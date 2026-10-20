"""Reusable skeletal GLBs; model-specific event bindings are not cached here."""
from collections import OrderedDict
import hashlib
from importlib import metadata
import json
from pathlib import Path
import struct
import tempfile
from time import perf_counter
from typing import TYPE_CHECKING
import warnings

import numpy as np

from .core import Rig, clip_from_native
from .gltf import animation_glb
from .native_animation import NativeAnimation, read_animation
from ...model_catalogue import animation_assets, resolve_animation
from ...model_paths import asset_path, asset_path_matches

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

INDEX_KEY = 'model_cache/animations-v1.json'


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class AnimationCache:
    """One build's bounded memory cache and optional durable local/S3 backing.

    The remote index points at ordinary immutable GLBs published by sync. It is
    written only after sync succeeds; this class never uploads those resources.
    Compiler source, NumPy and Luna versions invalidate entries when encoding changes.
    """
    def __init__(self, directory: str | Path, *, version: str = 'skeletal-glb-v1',
                 memory_bytes: int = 64 * 1024**2, decoded_bytes: int = 128 * 1024**2) -> None:
        self.directory = Path(directory)
        self.memory_bytes, self.decoded_bytes = memory_bytes, decoded_bytes
        compiler = [hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                    for name in ('core.py', 'gltf.py', 'native_animation.py', 'animation_cache.py')]
        luna = metadata.distribution('luna-kit')
        self.version = [version, np.__version__, luna.version, luna.read_text('direct_url.json'), *compiler]
        self.memory: OrderedDict[str, tuple[dict, bytes]] = OrderedDict()
        self.decoded: OrderedDict[str, NativeAnimation] = OrderedDict()
        self.fingerprints: dict[Path, tuple[tuple, str]] = {}
        self.records: dict[str, dict] = {}
        self.invalid_remote_assets: set[str] = set()
        self.client: S3Client | None = None
        self.bucket: str | None = None
        self.stats = dict(memory_hits=0, disk_hits=0, remote_hits=0, misses=0,
                          compiled=0, decode_hits=0, decoded=0, corrupt=0, cache_errors=0,
                          compile_seconds=0., decode_seconds=0.)

    def digest(self, path: Path) -> str:
        """Hash each unchanged file once per build; stat changes invalidate it."""
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        previous = self.fingerprints.get(path)
        if previous is not None and previous[0] == signature:
            return previous[1]
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        self.fingerprints[path] = (signature, digest)
        return digest

    def read_animation(self, path: Path) -> NativeAnimation:
        key = self.digest(path)
        if key in self.decoded:
            self.stats['decode_hits'] += 1
            self.decoded.move_to_end(key)
            return self.decoded[key]
        start = perf_counter()
        result = read_animation(path)
        if self.digest(path) != key:
            raise ValueError('Native animation changed during decoding')
        self.stats['decode_seconds'] += perf_counter() - start
        self.stats['decoded'] += 1
        size = result.translations.nbytes + result.rotations_xyzw.nbytes
        if size <= self.decoded_bytes:
            self.decoded[key] = result
            while sum(a.translations.nbytes + a.rotations_xyzw.nbytes for a in self.decoded.values()) > self.decoded_bytes:
                self.decoded.popitem(last=False)
        return result

    def load_remote(self, client: S3Client, bucket: str) -> None:
        self.client, self.bucket = client, bucket
        try:
            response = client.get_object(Bucket=bucket, Key=INDEX_KEY)
            with response['Body'] as body:
                index = json.loads(body.read())
            if index.get('version') != 1 or not isinstance(index.get('entries'), dict):
                raise ValueError('Unsupported animation cache index')
            self.records = index['entries']
        except Exception as error:
            # An optional optimization must not prevent native conversion.
            self._error('Animation cache unavailable; converting locally', error)

    def publish_remote(self, catalogue_path: Path) -> None:
        """Call only after normal sync has uploaded the referenced GLBs."""
        if self.client is not None and self.bucket:
            try:
                catalogue = json.loads(catalogue_path.read_text())
                actors = list(catalogue['models'].values())
                actors += [actor['additionalActor']['model'] for actor in list(actors) if 'additionalActor' in actor]
                shared = animation_assets(catalogue)
                paths = {resolve_animation(clip, shared)['path'] for actor in actors for clip in actor['animations']}
                entries = {key: record for key, record in self.records.items()
                           if isinstance(record, dict) and isinstance(record.get('asset'), dict)
                           and record['asset'].get('path') in paths}
                self.client.put_object(Bucket=self.bucket, Key=INDEX_KEY,
                    Body=_json({'version': 1, 'entries': entries}),
                    ContentType='application/json', CacheControl='no-cache')
            except Exception as error:
                self._error('Could not save the optional animation cache index', error)

    def _error(self, message: str, error: Exception) -> None:
        self.stats['cache_errors'] += 1
        warnings.warn(f'{message}: {type(error).__name__}: {error}', RuntimeWarning, stacklevel=2)

    @staticmethod
    def _validate(ref: dict, data: bytes, rig: Rig, row: dict, named: bool) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256', 'bytes', 'rigKey', 'id', 'name', 'duration'}
                or ref.get('sha256') != digest or ref.get('bytes') != len(data)
                or ref.get('rigKey') != rig.key or ref.get('id') != row['name']
                or ref.get('name') != row['name']
                or ref.get('duration') != len(row['translations']) / row['fps']
                or not asset_path_matches(ref.get('path'), digest, row['name'] if named else None)
                or len(data) < 20 or struct.unpack_from('<4sII', data) != (b'glTF', 2, len(data))):
            raise ValueError('Invalid animation cache entry')
        length, kind = struct.unpack_from('<II', data, 12)
        if kind != 0x4e4f534a or length > len(data) - 20:
            raise ValueError('Invalid cached GLB JSON')
        document = json.loads(data[20:20 + length])
        if (document['extras']['atpRig']['key'] != rig.key
                or document['extras']['atpRig']['parents'] != rig.parents
                or 'events' in document['extras']['atpClip']):
            raise ValueError('Cached animation contains incompatible rig or model events')
        return {key: ref[key] for key in ('path', 'sha256', 'bytes', 'rigKey', 'id', 'name', 'duration')}

    def _remember(self, key: str, ref: dict, data: bytes) -> None:
        self.records[key] = {'key': key, 'asset': ref}
        if len(data) <= self.memory_bytes:
            self.memory[key] = (ref, data)
            self.memory.move_to_end(key)
            while sum(len(data) for _, data in self.memory.values()) > self.memory_bytes:
                self.memory.popitem(last=False)

    def _write(self, key: str, ref: dict, data: bytes) -> None:
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            header = _json({'key': key, 'asset': ref})
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(struct.pack('<I', len(header)))
                handle.write(header)
                handle.write(data)
            temporary.replace(self.directory/(key + '.entry'))
        except OSError as error:
            self._error('Could not save animation cache entry', error)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    self._error('Could not remove partial animation cache entry', error)

    def compile(self, native: dict, row: dict, rig: Rig, *, named: bool) -> tuple[dict, bytes]:
        # Rig includes the exact ANIM digest and parent layout. Bone names and
        # bind matrices are model-only; the GLB uses ordinal joint names.
        # XML events/mesh masks are validated per model and emitted separately.
        key = hashlib.sha256(_json([self.version, rig.key, rig.parents,
            native['provenance']['animation_sha256'], row['name'], row['start'], row['end'], row['fps'],
            native['provenance'].get('native_frame_boundary', {}).get('policy', 'reject'), named])).hexdigest()
        if key in self.memory:
            self.stats['memory_hits'] += 1
            self.memory.move_to_end(key)
            ref, data = self.memory[key]
            return dict(ref), data
        path = self.directory/(key + '.entry')
        try:
            with path.open('rb') as handle:
                length, = struct.unpack('<I', handle.read(4))
                if not 0 < length <= 16384:
                    raise ValueError('Invalid animation cache header')
                entry, data = json.loads(handle.read(length)), handle.read()
                if not isinstance(entry, dict) or entry.get('key') != key:
                    raise ValueError('Animation cache compilation key mismatch')
                ref = entry['asset']
            ref = self._validate(ref, data, rig, row, named)
            self.stats['disk_hits'] += 1
            self._remember(key, ref, data)
            return dict(ref), data
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, IndexError, struct.error):
            self.stats['corrupt'] += 1
        if self.client is not None and key in self.records:
            try:
                entry = self.records[key]
                if not isinstance(entry, dict) or entry.get('key') != key:
                    raise ValueError('Remote animation cache compilation key mismatch')
                ref = entry['asset']
                if not asset_path_matches(ref.get('path'), ref.get('sha256'), row['name'] if named else None):
                    raise ValueError('Invalid remote animation cache path')
                response = self.client.get_object(Bucket=self.bucket, Key=ref['path'])
                with response['Body'] as body:
                    data = body.read()
                try:
                    ref = self._validate(ref, data, rig, row, named)
                except ValueError:
                    # Bad index metadata alone does not prove the GLB is corrupt.
                    # Sync must not trust stale hash metadata for a known bad body.
                    if hashlib.sha256(data).hexdigest() != ref['sha256']:
                        self.invalid_remote_assets.add(ref['path'])
                    raise
                self.stats['remote_hits'] += 1
                self._write(key, ref, data)
                self._remember(key, ref, data)
                return dict(ref), data
            except Exception as error:
                self._error('Animation cache asset unavailable; rebuilding', error)
        self.stats['misses'] += 1
        start = perf_counter()
        clip = clip_from_native(row, rig)
        clip.events = None
        data = animation_glb(clip)
        self.stats['compile_seconds'] += perf_counter() - start
        self.stats['compiled'] += 1
        digest = hashlib.sha256(data).hexdigest()
        ref = dict(path=asset_path(digest, row['name'] if named else None), sha256=digest,
                   bytes=len(data), rigKey=rig.key, id=row['name'], name=row['name'],
                   duration=len(row['translations']) / row['fps'])
        # Until sync repairs this path, a disk hit in the next build would hide
        # the remote corruption if the upload failed. Memory reuse is still safe.
        if ref['path'] not in self.invalid_remote_assets:
            self._write(key, ref, data)
        self._remember(key, ref, data)
        return dict(ref), data
