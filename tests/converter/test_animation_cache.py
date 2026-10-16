"""Shared compilation and persistent reuse, using only authored native inputs."""
from pathlib import Path
import struct
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from game_data.model_gen.converter.export import convert_native
from game_data.model_gen.converter.animation_cache import AnimationCache
from native_fixture import write_fixture


class AnimationCaching(unittest.TestCase):
    def test_cold_warm_source_change_and_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'native'
            write_fixture(source)
            def convert(cache, identity='Synthetic_A'):
                return convert_native(source/'flag.rk', source/'flag.anim', source/'flag.csv', ['wave'],
                    identity=identity, visible_meshes=['flag'], rest_name='rest', event_path=source/'flag.xml',
                    animation_filenames=True, animation_cache=cache)
            cache = AnimationCache(root/'cache')
            first = convert(cache)
            second = convert(cache, 'Synthetic_B')
            a = first.catalogue['models']['Synthetic_A']['animations'][0]
            b = second.catalogue['models']['Synthetic_B']['animations'][0]
            self.assertEqual(a, b)
            self.assertEqual(cache.stats['compiled'], 1)
            self.assertEqual(cache.stats['memory_hits'], 1)
            fresh = AnimationCache(root/'cache')
            warm_bundle = convert(fresh)
            self.assertEqual(warm_bundle, first)
            self.assertEqual(json.dumps(warm_bundle.catalogue), json.dumps(first.catalogue))
            self.assertEqual(fresh.stats['compiled'], 0)
            self.assertEqual(fresh.stats['disk_hits'], 1)
            entry = next((root/'cache').glob('*.entry'))
            raw = entry.read_bytes()
            length, = struct.unpack_from('<I', raw)
            header = json.loads(raw[4:4+length])
            header['asset']['eventBinding'] = {'version':1, 'events':[{'frame':0,'type':'EyeSet','state':'Closed','enableBlink':False}]}
            modified = json.dumps(header).encode()
            entry.write_bytes(struct.pack('<I', len(modified)) + modified + raw[4+length:])
            clean = AnimationCache(root/'cache')
            self.assertEqual(convert(clean), first, 'model events cannot be injected through a shared cache reference')
            self.assertEqual(clean.stats['compiled'], 1)

            # A damaged cache entry is a miss, never a referenced invalid asset.
            next((root/'cache').glob('*.entry')).write_bytes(struct.pack('<I', 2) + b'[]')
            repaired = AnimationCache(root/'cache')
            self.assertEqual(convert(repaired), first)
            self.assertEqual(repaired.stats['compiled'], 1)
            path = source/'flag.anim'
            raw = bytearray(path.read_bytes())
            struct.pack_into('<f', raw, 92 + 64 + 16, .2)
            path.write_bytes(raw)
            changed = convert(cache).catalogue['models']['Synthetic_A']['animations'][0]
            self.assertNotEqual(a['sha256'], changed['sha256'])
            self.assertEqual(cache.stats['compiled'], 2)
            version = AnimationCache(root/'cache', version='test-new-serializer')
            convert(version)
            self.assertEqual(version.stats['compiled'], 1)

    def test_source_change_after_decode_cannot_populate_new_source_key(self):
        from game_data.model_gen.converter.native import NativeInputError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'native'
            write_fixture(source)
            cache = AnimationCache(root/'cache')
            def convert(current_cache):
                return convert_native(source/'flag.rk', source/'flag.anim', source/'flag.csv', ['wave'],
                    identity='Synthetic', visible_meshes=['flag'], rest_name='rest', event_path=source/'flag.xml',
                    animation_filenames=True, animation_cache=current_cache)
            original = cache.read_animation
            def read_then_change(path):
                decoded = original(path)
                data = bytearray(path.read_bytes())
                struct.pack_into('<f', data, 92 + 64 + 16, .2)
                path.write_bytes(data)
                return decoded
            with patch.object(cache, 'read_animation', side_effect=read_then_change):
                with self.assertRaisesRegex(NativeInputError, 'animation changed'):
                    convert(cache)
            self.assertEqual(list((root/'cache').glob('*.entry')), [])
            # The next process must compile the current source, never hit stale frames.
            warm = AnimationCache(root/'cache')
            self.assertEqual(convert(warm), convert(AnimationCache(root/'fresh-cache')))
            self.assertEqual(warm.stats['disk_hits'], 0)
            self.assertEqual(warm.stats['compiled'], 1)

    def test_injected_cache_cannot_write_into_public_output(self):
        from game_data.model_gen import generate_models
        from native_fixture import write_native_tables
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, dist = root/'native', root/'dist'
            write_fixture(source)
            objects = write_native_tables(source)
            with self.assertRaisesRegex(ValueError, 'cache must stay outside public dist'):
                generate_models(objects, source, dist, root/'report.json', campaign={},
                                animation_cache=AnimationCache(dist/'cache'))
            self.assertFalse(dist.exists())

    def test_csv_interval_and_incompatible_rig_do_not_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture(root/'native')
            cache = AnimationCache(root/'cache')
            from game_data.model_gen.converter.native import load_native
            from game_data.model_gen.converter.core import rig_from_native
            native = load_native(root/'native/flag.rk', root/'native/flag.anim', root/'native/flag.csv', ['wave'],
                visible_meshes=['flag'], rest_name='rest', event_path=root/'native/flag.xml')
            rig = rig_from_native(native)
            row = native['clips'][0]
            first, data = cache.compile(native, row, rig, named=True)
            cut = {**row, 'end': row['end']-1, 'translations':row['translations'][:-1],
                   'rotations_xyzw':row['rotations_xyzw'][:-1]}
            second, _ = cache.compile(native, cut, rig, named=True)
            self.assertNotEqual(first['path'], second['path'])
            from dataclasses import replace
            other, _ = cache.compile(native, row, replace(rig, key='b'*64, parents=[-1,-1]), named=True)
            self.assertNotEqual(first['rigKey'], other['rigKey'])
            self.assertEqual(cache.stats['compiled'], 3)
            offset = {**row, 'start': row['start']+1, 'translations':row['translations'][1:],
                      'rotations_xyzw':row['rotations_xyzw'][1:]}
            shifted, _ = cache.compile(native, offset, rig, named=True)
            entries = {}
            for path in (root/'cache').glob('*.entry'):
                data = path.read_bytes()
                length, = struct.unpack_from('<I', data)
                header = json.loads(data[4:4+length])
                entries[header['asset']['sha256']] = path
            entries[second['sha256']].write_bytes(entries[shifted['sha256']].read_bytes())
            recovered = AnimationCache(root/'cache')
            repaired, _ = recovered.compile(native, cut, rig, named=True)
            self.assertEqual(repaired, second)
            self.assertEqual(recovered.stats['compiled'], 1)


    def test_remote_index_uses_published_assets_and_missing_asset_rebuilds(self):
        from game_data.model_gen.converter.animation_cache import INDEX_KEY
        from game_data.model_gen.converter.native import load_native
        from game_data.model_gen.converter.core import rig_from_native
        class Bucket:
            def __init__(self): self.objects = {}; self.writes = []
            def get_object(self, *, Bucket, Key):
                return {'Body': io.BytesIO(self.objects[Key])}
            def put_object(self, *, Bucket, Key, Body, **kwargs):
                self.objects[Key] = Body; self.writes.append(Key)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture(root/'native')
            native = load_native(root/'native/flag.rk', root/'native/flag.anim', root/'native/flag.csv', ['wave'],
                visible_meshes=['flag'], rest_name='rest', event_path=root/'native/flag.xml')
            rig, row = rig_from_native(native), native['clips'][0]
            bucket = Bucket()
            first = AnimationCache(root/'first')
            with self.assertWarns(RuntimeWarning): first.load_remote(bucket, 'local-test')
            ref, data = first.compile(native, row, rig, named=True)
            self.assertEqual(bucket.writes, [], 'conversion never uploads assets or publishes its index')
            bucket.objects[ref['path']] = data  # successful ordinary sync
            catalogue_path = root/'catalogue.json'
            catalogue_path.write_text(json.dumps({'models': {'Synthetic': {'animations': [ref]}}}))
            first.records['not-published'] = {'key': 'not-published', 'asset': {**ref, 'path': 'models/3d/missing.glb'}}
            first.publish_remote(catalogue_path)
            self.assertNotIn('not-published', json.loads(bucket.objects[INDEX_KEY])['entries'])
            self.assertEqual(bucket.writes, [INDEX_KEY])
            warm = AnimationCache(root/'fresh')
            warm.load_remote(bucket, 'local-test')
            self.assertEqual(warm.compile(native, row, rig, named=True), (ref, data))
            self.assertEqual(warm.stats['remote_hits'], 1)
            self.assertEqual(warm.stats['compiled'], 0)
            self.assertEqual(warm.invalid_remote_assets, set())
            index = bucket.objects[INDEX_KEY]
            # A wrong index size is not evidence that the actual content is bad.
            altered = json.loads(index)
            next(iter(altered['entries'].values()))['asset']['bytes'] += 1
            bucket.objects[INDEX_KEY] = json.dumps(altered).encode()
            bad_index = AnimationCache(root/'bad-index')
            bad_index.load_remote(bucket, 'local-test')
            with self.assertWarns(RuntimeWarning):
                self.assertEqual(bad_index.compile(native, row, rig, named=True), (ref, data))
            self.assertEqual(bad_index.invalid_remote_assets, set())
            bucket.objects[INDEX_KEY] = index
            bucket.objects[ref['path']] = data[:-1] + bytes([data[-1] ^ 1])
            corrupt = AnimationCache(root/'corrupt-remote')
            corrupt.load_remote(bucket, 'local-test')
            with self.assertWarns(RuntimeWarning):
                self.assertEqual(corrupt.compile(native, row, rig, named=True), (ref, data))
            self.assertEqual(corrupt.invalid_remote_assets, {ref['path']})
            self.assertEqual(corrupt.stats['compiled'], 1)
            del bucket.objects[ref['path']]
            missing = AnimationCache(root/'missing')
            missing.load_remote(bucket, 'local-test')
            with self.assertWarns(RuntimeWarning):
                rebuilt, actual = missing.compile(native, row, rig, named=True)
            self.assertEqual((rebuilt, actual), (ref, data))
            self.assertEqual(missing.stats['compiled'], 1)
            self.assertEqual(missing.invalid_remote_assets, set())
            self.assertEqual(bucket.writes, [INDEX_KEY])

    def test_one_unsupported_clip_does_not_discard_valid_library_clips(self):
        from game_data.model_gen.converter import ConversionError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture(root/'native')
            cache = AnimationCache(root/'cache')
            original = cache.compile
            def compile(native, row, rig, *, named):
                if row['name'] == 'wave': raise ConversionError('injected unsupported clip')
                return original(native, row, rig, named=named)
            with patch.object(cache, 'compile', side_effect=compile):
                bundle = convert_native(root/'native/flag.rk', root/'native/flag.anim', root/'native/flag.csv', ['wave','rest'],
                    identity='Pony_Test', visible_meshes=['flag'], rest_name='rest', event_path=root/'native/flag.xml',
                    animation_filenames=True, animation_cache=cache)
            self.assertEqual([clip['id'] for clip in bundle.catalogue['models']['Pony_Test']['animations']], ['rest'])
            self.assertEqual(bundle.provenance['animation_exclusions'],
                             [{'name':'wave','reason':'injected unsupported clip'}])
