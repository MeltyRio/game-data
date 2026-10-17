"""Offline unit tests of actual sync functions; no SDK, cloud or build calls."""
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import io
import json
import math
import mimetypes
from pathlib import Path
import re
import struct
import tempfile
import threading
from types import SimpleNamespace
from typing import Literal
import unittest
from unittest.mock import Mock, patch

from rich.console import Console
from rich.progress import Progress as RichProgress

from bootstrap import ROOT
from game_data.console import track
from game_data.model_catalogue import compact_animations, animation_assets, resolve_animation
from game_data.model_paths import is_asset_path, asset_path_matches, model_path, safe_asset_name


SOURCE = ROOT/'src/game_data/sync.py'


class ClientError(Exception):
    def __init__(self, code):
        self.response = {'Error': {'Code': code}}


class Progress:
    def __init__(self, *args, **kwargs):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def add_task(self, *args, **kwargs):
        return 1
    def advance(self, *args):
        pass


class MemoryClient:
    """Only the used S3 surface; deliberately no delete/download implementation."""
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_put = set()
        self.fail_copy = set()
        self.lock = threading.Lock()

    def head_object(self, Bucket, Key):
        with self.lock:
            self.calls.append(('head', Bucket, Key))
            if (Bucket, Key) not in self.objects:
                raise ClientError('404')
            return copy.deepcopy(self.objects[Bucket, Key])

    def put_object(self, Bucket, Key, Body, **kwargs):
        with self.lock:
            self.calls.append(('put', Bucket, Key))
            if Key in self.fail_put:
                raise RuntimeError('injected put failure: ' + Key)
            self.objects[Bucket, Key] = {'Body': Body, 'Metadata': kwargs.pop('Metadata', {}), **kwargs}

    def get_object(self, Bucket, Key):
        with self.lock:
            self.calls.append(('get', Bucket, Key))
            if (Bucket, Key) not in self.objects:
                raise ClientError('404')
            return {'Body': io.BytesIO(self.objects[Bucket, Key]['Body'])}

    def copy_object(self, Bucket, Key, CopySource, **kwargs):
        with self.lock:
            self.calls.append(('copy', Bucket, Key))
            if Key in self.fail_copy:
                raise RuntimeError('injected copy failure: ' + Key)
            body = self.objects[CopySource['Bucket'], CopySource['Key']]['Body']
            self.objects[Bucket, Key] = {'Body': body, **kwargs}


def load_sync(client):
    source = ast.parse(SOURCE.read_text())
    names = {'upload_file', 'upload_single_file', 'prepare_model_upload', 'upload_model_asset', 'sync'}
    functions = [item for item in source.body if isinstance(item, ast.FunctionDef) and item.name in names]
    assert len(functions) == len(names)
    context = {'animation_assets': animation_assets, 'resolve_animation': resolve_animation, 'safe_asset_name': safe_asset_name, 'is_asset_path': is_asset_path, 'model_path': model_path, 'asset_path_matches': asset_path_matches, 'hashlib': hashlib, 'json': json, 'math': math, 'mimetypes': mimetypes,
        'Path': Path, 're': re, 'Literal': Literal, 'ThreadPoolExecutor': ThreadPoolExecutor,
        'as_completed': as_completed, 'Progress': Progress, 'track': track, 'MAX_WORKERS': 3,
        'HEADERS_TO_CHECK': ['CacheControl', 'ContentType'], 'COLUMNS': [],
        'botocore': SimpleNamespace(exceptions=SimpleNamespace(ClientError=ClientError)),
        'console': Mock(), 'DEFAULT_BUCKET': 'must-not-be-used',
        'get_s3_client': Mock(return_value=client), 'generate_diff': Mock(),
        'json_dumps_compact': lambda value: json.dumps(value, separators=(',', ':'), ensure_ascii=False),
        'get_image_metadata': Mock(side_effect=AssertionError('image handling is outside this test'))}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), 'exec'), context)
    return context


def glb(label):
    document = json.dumps({'asset': {'version': '2.0', 'generator': label}}).encode()
    document += b' ' * (-len(document) % 4)
    return struct.pack('<III', 0x46546C67, 2, 20+len(document)) + struct.pack('<II', len(document), 0x4E4F534A) + document


class ModelSync(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dist = Path(self.temp.name)/'dist'
        self.dist.mkdir()
        self.client = MemoryClient()
        self.code = load_sync(self.client)
        self.bucket = 'test-isolated-bucket'
        self.rig = '1'*64
        self.model = self.write_asset(glb('self-authored model'))
        self.animation = self.write_asset(glb('self-authored animation'))
        self.animation.update(id='idle', name='Idle', duration=1)
        self.model['animations'] = [self.animation]
        self.catalogue = {'schemaVersion': 1, 'models': {'Pony_Test': self.model}}
        self.save_catalogue()
        (self.dist/'game_objects.json').write_text('{ "example": true }')
        self.client.objects[self.bucket, 'manifest.json'] = {'Body': b'old manifest', 'Metadata': {}}

    def test_schema_three_native_interaction_closure(self):
        self.catalogue['schemaVersion'] = 3
        self.model['interactions'] = {'version': 1, 'clickAnimations': ['idle'], 'dizzyAnimation': 'idle'}
        self.save_catalogue()
        self.code['prepare_model_upload'](self.dist)
        for profile in [{'version': 1, 'clickAnimations': ['missing']},
                        {'version': 1, 'clickAnimations': ['idle', 'idle']},
                        {'version': True, 'clickAnimations': []},
                        {'version': 1, 'clickAnimations': [], 'dizzyAnimation': 'missing'},
                        {'version': 1, 'clickAnimations': [], 'unknown': []}]:
            self.model['interactions'] = profile; self.save_catalogue()
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                self.code['prepare_model_upload'](self.dist)
        self.model['interactions'] = {'version': 1, 'clickAnimations': []}
        self.catalogue['schemaVersion'] = 2; self.save_catalogue()
        with self.assertRaises(ValueError): self.code['prepare_model_upload'](self.dist)

    def test_compact_animation_table_preserves_model_events_and_child_order(self):
        child = self.add_additional_actor()
        original = copy.deepcopy(self.catalogue)
        binding = {'version': 1, 'events': [
            {'frame': 0, 'type': 'SubObject', 'meshes': [2], 'visible': True}]}
        self.catalogue['models']['Pony_Other'] = copy.deepcopy(self.model)
        self.catalogue['models']['Pony_Other']['animations'][0]['eventBinding'] = binding
        self.catalogue = compact_animations(self.catalogue)
        table = animation_assets(self.catalogue)
        self.assertEqual(len(table), 3)
        self.model = self.catalogue['models']['Pony_Test']
        self.assertNotIn('eventBinding', table[0])
        self.assertEqual(table[2]['eventBinding'], binding)
        self.assertEqual(self.model['animations'], [0])
        self.assertEqual(self.model['additionalActor']['model']['animations'], [1])
        self.assertEqual(self.catalogue['models']['Pony_Other']['animations'],
                         [2])
        self.assertEqual(resolve_animation(0, table), original['models']['Pony_Test']['animations'][0])
        self.assertNotIn('eventBinding', resolve_animation(0, table))
        self.save_catalogue(); self.run_sync()
        writes = self.writes()
        for ref in table:
            self.assertEqual(writes.count(ref['path']), 1)
            self.assertLess(writes.index(ref['path']), writes.index('models_3d.json'))

    def test_invalid_compact_animation_references_do_not_upload(self):
        self.catalogue = compact_animations(self.catalogue)
        self.model = self.catalogue['models']['Pony_Test']
        for bad in [True, -1, 1, {'asset': 0}, {'asset': 0, 'eventBinding': {}, 'unexpected': 1}]:
            self.model['animations'] = [bad]
            self.save_catalogue()
            with self.subTest(value=bad), self.assertRaises(ValueError):
                self.run_sync()
        self.model['animations'] = [0]
        self.catalogue['schemaVersion'] = 3
        self.save_catalogue()
        with self.assertRaises(ValueError): self.run_sync()
        self.assertEqual(self.client.calls, [])

    def write_asset(self, data):
        digest = hashlib.sha256(data).hexdigest()
        key = f'models/3d/{digest}.glb'
        path = self.dist/key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {'path': key, 'sha256': digest, 'bytes': len(data), 'rigKey': self.rig}

    def save_catalogue(self):
        (self.dist/'models_3d.json').write_text(json.dumps(self.catalogue, indent=2))

    def run_sync(self):
        return self.code['sync'](self.dist, self.bucket)

    def writes(self):
        return [key for operation, bucket, key in self.client.calls if operation in {'put', 'copy'}]

    def assert_old_manifest(self):
        self.assertEqual(self.client.objects[self.bucket, 'manifest.json']['Body'], b'old manifest')

    def test_binary_catalogue_manifest_order_and_actual_bucket(self):
        self.run_sync()
        writes = self.writes()
        self.assertLess(writes.index(self.model['path']), writes.index('models_3d.json'))
        self.assertLess(writes.index(self.animation['path']), writes.index('models_3d.json'))
        self.assertLess(writes.index('models_3d.json'), writes.index('manifest.json'))
        self.assertEqual(writes[-1], 'manifest.json')
        self.assertTrue(all(bucket == self.bucket for _, bucket, _ in self.client.calls))
        remote_catalogue = self.client.objects[self.bucket, 'models_3d.json']['Body']
        manifest = json.loads(self.client.objects[self.bucket, 'manifest.json']['Body'])
        self.assertEqual(manifest['models_3d.json'], hashlib.sha256(remote_catalogue).hexdigest())
        self.assertNotIn(b'\n', remote_catalogue)

    def test_model_upload_progress_counts_completed_assets(self):
        displays = []
        def progress(*args, **kwargs):
            display = RichProgress(*args, console=Console(file=io.StringIO()), auto_refresh=False)
            displays.append(display)
            return display

        with patch('game_data.console.Progress', side_effect=progress):
            self.run_sync()

        tasks = [task for display in displays for task in display.tasks]
        self.assertEqual([(task.completed, task.total) for task in tasks], [(2, 2)])
        self.assertIn((self.bucket, self.model['path']), self.client.objects)
        self.assertIn((self.bucket, self.animation['path']), self.client.objects)


    def test_missing_asset_stops_before_any_cloud_call(self):
        (self.dist/self.animation['path']).unlink()
        with self.assertRaisesRegex(ValueError, 'Missing'):
            self.run_sync()
        self.code['get_s3_client'].assert_not_called()
        self.code['generate_diff'].assert_not_called()
        self.assertEqual(self.client.calls, [])


    def test_binary_put_failure_cannot_publish_catalogue_or_manifest(self):
        self.client.fail_put.add(self.model['path'])
        with self.assertRaisesRegex(RuntimeError, 'put failure'):
            self.run_sync()
        self.assertNotIn('models_3d.json', self.writes())
        self.assert_old_manifest()


    def test_catalogue_failure_keeps_previous_manifest(self):
        self.client.fail_put.add('models_3d.json')
        with self.assertRaisesRegex(RuntimeError, 'put failure'):
            self.run_sync()
        self.assert_old_manifest()
        self.assertNotIn('manifest.json', self.writes())


    def add_additional_actor(self):
        child_model = self.write_asset(glb('self-authored additional model'))
        child_animation = self.write_asset(glb('self-authored additional animation'))
        child_model['rigKey'] = child_animation['rigKey'] = '2'*64
        child_animation.update(id='idle', name='Idle', duration=2)
        child_model['animations'] = [child_animation]
        child = {'model': child_model, 'parentRoot': 0,
                 'controller': 'native-first-then-random-v1',
                 'transform': [0, 2, 0, 0, -2, 0, 0, 0, 0, 0, 2, 0, 10, -4, 3, 1]}
        self.model['additionalActor'] = child
        self.catalogue['schemaVersion'] = 2
        self.save_catalogue()
        return child


    def test_child_has_independent_rig_and_shared_assets_upload_once_before_catalogue(self):
        child = self.add_additional_actor()
        self.catalogue['models']['Pony_Other'] = copy.deepcopy(self.model)
        self.save_catalogue()
        self.run_sync()
        writes = self.writes()
        for ref in [self.model, self.animation, child['model'], child['model']['animations'][0]]:
            self.assertEqual(writes.count(ref['path']), 1)
            self.assertLess(writes.index(ref['path']), writes.index('models_3d.json'))
        remote = json.loads(self.client.objects[self.bucket, 'models_3d.json']['Body'])
        self.assertEqual(remote['models']['Pony_Test']['additionalActor'], child)
        self.assertEqual(writes[-1], 'manifest.json')


    def make_fixed(self, ref, identity):
        data = (self.dist/ref['path']).read_bytes()
        ref['path'] = f'models/3d/{identity}.glb'
        (self.dist/ref['path']).write_bytes(data)

    def change_fixed_model(self, label):
        data = glb(label)
        (self.dist/self.model['path']).write_bytes(data)
        self.model.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        self.save_catalogue()
        return data

    def test_fixed_model_overwrite_and_cache_headers_without_duplicate_upload(self):
        self.make_fixed(self.model, 'Pony_Test')
        self.make_named(self.animation, self.animation['id'])
        child = self.add_additional_actor()
        self.make_named(child['model']['animations'][0], 'idle')
        self.save_catalogue(); self.run_sync()
        self.assertEqual(self.client.objects[self.bucket,self.model['path']]['CacheControl'], 'no-cache')
        for ref in [self.animation, child['model'], child['model']['animations'][0]]:
            self.assertEqual(self.client.objects[self.bucket,ref['path']]['CacheControl'], 'max-age=2592000')
        self.client.calls.clear(); self.run_sync()
        self.assertFalse([key for key in self.writes() if key.startswith('models/3d/')],
                         'identical 3D bytes and headers require no put or copy')
        old_catalogue = self.client.objects[self.bucket,'models_3d.json']['Body']
        updated = self.change_fixed_model('replacement current Object ID model')
        self.client.calls.clear(); self.run_sync()
        self.assertEqual(self.client.objects[self.bucket,self.model['path']]['Body'], updated)
        self.assertNotEqual(self.client.objects[self.bucket,'models_3d.json']['Body'], old_catalogue)
        self.assertNotIn(self.animation['path'], self.writes())


    def test_fixed_remote_failure_can_leave_old_catalogue_and_replaced_model(self):
        self.make_fixed(self.model, 'Pony_Test'); self.save_catalogue(); self.run_sync()
        old_catalogue = self.client.objects[self.bucket,'models_3d.json']['Body']
        old_manifest = self.client.objects[self.bucket,'manifest.json']['Body']
        old_digest = self.model['sha256']
        updated = self.change_fixed_model('replacement before another asset upload fails')
        self.animation.update(self.write_asset(glb('new failing animation')))
        self.save_catalogue(); self.client.fail_put.add(self.animation['path'])
        with self.assertRaisesRegex(RuntimeError, 'put failure'):
            self.run_sync()
        self.assertEqual(self.client.objects[self.bucket,self.model['path']]['Body'], updated)
        self.assertNotEqual(hashlib.sha256(updated).hexdigest(), old_digest)
        self.assertEqual(self.client.objects[self.bucket,'models_3d.json']['Body'], old_catalogue)
        self.assertEqual(self.client.objects[self.bucket,'manifest.json']['Body'], old_manifest)

    def test_fixed_stale_digest_and_wrong_object_paths_fail_before_cloud(self):
        self.make_fixed(self.model, 'Pony_Test')
        for path in ['models/3d/Other.glb', 'models/3d/../Pony_Test.glb', 'models/3d/Pony_Test%2F.glb']:
            self.model['path'] = path; self.save_catalogue()
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'path'):
                self.run_sync()
        self.model['path'] = 'models/3d/Pony_Test.glb'
        (self.dist/self.model['path']).write_bytes(glb('changed but old declared digest'))
        self.save_catalogue()
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            self.run_sync()
        self.assertEqual(self.client.calls, [])


    def make_named(self, ref, name):
        old = self.dist/ref['path']
        ref['path'] = f"models/3d/{ref['sha256']}/{name}.glb"
        target = self.dist/ref['path'];target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old.read_bytes())




    def test_cached_files_are_uploaded_by_normal_sync_and_missing_remote_asset_is_restored(self):
        from game_data.model_gen.converter.animation_cache import AnimationCache
        from game_data.model_gen.converter.export import convert_native
        from native_fixture import write_fixture
        root = Path(self.temp.name)
        write_fixture(root/'native')
        def generate(cache):
            return convert_native(root/'native/flag.rk', root/'native/flag.anim', root/'native/flag.csv', ['wave'],
                identity='Pony_Test', model_filename='Pony_Test', animation_filenames=True,
                visible_meshes=['flag'], rest_name='rest', event_path=root/'native/flag.xml', animation_cache=cache)
        first = generate(AnimationCache(root/'cache'))
        warm = AnimationCache(root/'cache')
        bundle = generate(warm)
        self.assertEqual(first, bundle)
        self.assertEqual(warm.stats['compiled'], 0)
        self.catalogue = bundle.catalogue
        for key, data in bundle.files.items():
            path = self.dist/key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.save_catalogue()
        self.run_sync()
        animation = bundle.catalogue['models']['Pony_Test']['animations'][0]
        del self.client.objects[self.bucket, animation['path']]
        self.client.calls.clear()
        self.run_sync()
        self.assertEqual(self.client.objects[self.bucket, animation['path']]['Body'], bundle.files[animation['path']])
        self.assertIn(('put', self.bucket, animation['path']), self.client.calls)


    def test_confirmed_corrupt_asset_is_repaired_despite_hash_metadata(self):
        self.run_sync()
        key = self.animation['path']
        expected = (self.dist/key).read_bytes()
        for metadata_hash in (self.animation['sha256'], 'incorrect'):
            with self.subTest(metadata_hash=metadata_hash):
                self.client.objects[self.bucket, key]['Body'] = b'corrupted'
                self.client.objects[self.bucket, key]['Metadata']['hash'] = metadata_hash
                self.client.calls.clear()
                self.code['sync'](self.dist, self.bucket, repair_model_assets={key, 'unreferenced'})
                self.assertEqual(self.client.objects[self.bucket, key]['Body'], expected)
                self.assertEqual(self.client.objects[self.bucket, key]['Metadata'],
                                 {'hash': self.animation['sha256']})
                self.assertEqual([k for k in self.writes() if k.startswith('models/3d/')], [key])
                self.assertLess(self.writes().index(key), self.writes().index('manifest.json'))
        self.client.calls.clear()
        self.run_sync()
        self.assertFalse([k for k in self.writes() if k.startswith('models/3d/')])

    def test_repair_failure_and_override_preserve_published_catalogue(self):
        self.run_sync()
        key = self.animation['path']
        old_catalogue = self.client.objects[self.bucket, 'models_3d.json']['Body']
        old_manifest = self.client.objects[self.bucket, 'manifest.json']['Body']
        for failure in ('put', 'override-same-hash', 'override-different-hash'):
            with self.subTest(failure=failure):
                remote = self.client.objects[self.bucket, key]
                remote['Body'] = b'corrupted'
                remote['Metadata'] = {'hash': self.animation['sha256']}
                self.client.fail_put.clear()
                if failure == 'put':
                    self.client.fail_put.add(key)
                else:
                    remote['Metadata']['override'] = 'true'
                    if failure.endswith('different-hash'):
                        remote['Metadata']['hash'] = 'different'
                self.client.calls.clear()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.code['sync'](self.dist, self.bucket, repair_model_assets={key})
                self.assertEqual(self.client.objects[self.bucket, key]['Body'], b'corrupted')
                self.assertEqual(self.client.objects[self.bucket, 'models_3d.json']['Body'], old_catalogue)
                self.assertEqual(self.client.objects[self.bucket, 'manifest.json']['Body'], old_manifest)
                self.assertNotIn('models_3d.json', self.writes())
                self.assertNotIn('manifest.json', self.writes())


    def test_changed_asset_after_preflight_is_rejected_before_its_cloud_calls(self):
        _, assets = self.code['prepare_model_upload'](self.dist)
        key = self.animation['path']
        path = self.dist/key
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, 'changed after validation'):
            self.code['upload_model_asset'](self.client, self.bucket, self.dist, key, assets[key])
        self.assertEqual(self.client.calls, [])
