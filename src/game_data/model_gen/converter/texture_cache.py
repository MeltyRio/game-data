"""Bounded, disposable disk cache of encoded native texture conversions.

Only PNG bytes survive a decode. Keys hash source content (including alpha),
conversion settings and decoder versions; paths and timestamps are not identity.
Cache I/O failure is a miss and cannot invalidate a successful native conversion.
"""
from __future__ import annotations

from collections.abc import Callable, Collection
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import struct
import tempfile

from PIL import features

_HEADER = struct.Struct('>8s32sIIQ')
_MAGIC = b'ATPTEX02'
_MAX_ENTRY_BYTES = 64 * 1024 * 1024


def _decoder_fingerprint() -> str:
    versions = {name: metadata.version(name) for name in ('luna-kit', 'pillow', 'texture2ddecoder')}
    versions['luna-source'] = metadata.distribution('luna-kit').read_text('direct_url.json')
    versions['png-zlib'] = features.version_codec('zlib')
    return json.dumps(versions, sort_keys=True)


class TextureCache:
    """One generation's handle to a persistent encoded-PNG cache directory."""

    def __init__(self, directory: str | Path, max_bytes: int = 512 * 1024 * 1024) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError('Texture cache byte budget must be a nonnegative integer')
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self._decoder = _decoder_fingerprint()
        self.hits = self.misses = self.writes = 0
        self.invalid_entries = self.io_errors = 0

    def _key(self, sources: tuple[Path, ...], settings: str) -> bytes:
        digest = hashlib.sha256()
        digest.update(json.dumps([1, self._decoder, settings, len(sources)]).encode())
        for path in sources:
            with path.open('rb') as source:
                digest.update(hashlib.file_digest(source, 'sha256').digest())
        return digest.digest()

    def _read(self, path: Path, key: bytes) -> tuple[bytes, int, int] | None:
        try:
            with path.open('rb') as handle:
                if os.fstat(handle.fileno()).st_size > min(self.max_bytes, _MAX_ENTRY_BYTES):
                    raise ValueError('Cache entry exceeds its byte budget')
                magic, stored_key, width, height, length = _HEADER.unpack(handle.read(_HEADER.size))
                if magic != _MAGIC or stored_key != key or length > min(self.max_bytes, _MAX_ENTRY_BYTES):
                    raise ValueError('Invalid cache entry header')
                png = handle.read(length + 1)
            if len(png) != length:
                raise ValueError('Truncated texture cache entry')
        except FileNotFoundError:
            return None
        except OSError:
            self.io_errors += 1
            return None
        except (ValueError, struct.error):
            self.invalid_entries += 1
            return None
        try:
            path.touch()
        except OSError:
            self.io_errors += 1
        return png, width, height

    def _write(self, path: Path, key: bytes, result: tuple[bytes, int, int]) -> None:
        png, width, height = result
        size = _HEADER.size + len(png)
        if size > min(self.max_bytes, _MAX_ENTRY_BYTES):
            return
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(_HEADER.pack(_MAGIC, key, width, height, len(png)))
                handle.write(png)
            os.replace(temporary, path)
            temporary = None
            self.writes += 1
            # Sweep persisted entries too, including those from previous runs.
            entries = []
            for entry in self.directory.glob('*.pngcache'):
                try:
                    stat = entry.stat()
                except FileNotFoundError:
                    continue
                entries.append((stat.st_mtime_ns, entry, stat.st_size))
            total = sum(row[2] for row in entries)
            for _, entry, entry_size in sorted(entries):
                if total <= self.max_bytes:
                    break
                entry.unlink(missing_ok=True)
                total -= entry_size
        except OSError:
            self.io_errors += 1
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    self.io_errors += 1

    def get_or_decode(self, sources: Collection[Path], *, settings: str,
                      decode: Callable[[], tuple[bytes, int, int]]) -> tuple[bytes, int, int]:
        sources = tuple(Path(path) for path in sources)
        if not self.max_bytes:
            self.misses += 1
            return decode()
        try:
            key = self._key(sources, settings)
        except OSError:
            self.io_errors += 1
            self.misses += 1
            return decode()
        path = self.directory / (key.hex() + '.pngcache')
        cached = self._read(path, key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        result = decode()
        self._write(path, key, result)
        return result
