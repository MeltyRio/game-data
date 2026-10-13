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
from unittest.mock import Mock

from bootstrap import ROOT
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
    context = {'safe_asset_name': safe_asset_name, 'is_asset_path': is_asset_path, 'model_path': model_path, 'asset_path_matches': asset_path_matches, 'hashlib': hashlib, 'json': json, 'math': math, 'mimetypes': mimetypes,
        'Path': Path, 're': re, 'Literal': Literal, 'ThreadPoolExecutor': ThreadPoolExecutor,
        'as_completed': as_completed, 'Progress': Progress, 'MAX_WORKERS': 3,
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

    def test_shared_assets_upload_once_and_unused_old_hash_stays_local(self):
        self.catalogue['models']['Pony_Other'] = copy.deepcopy(self.model)
        stale = self.write_asset(glb('retained old generation'))
        self.save_catalogue()
        self.run_sync()
        self.assertEqual(self.writes().count(self.animation['path']), 1)
        self.assertNotIn(stale['path'], self.writes())
        self.assertTrue((self.dist/stale['path']).exists())

    def test_missing_asset_stops_before_any_cloud_call(self):
        (self.dist/self.animation['path']).unlink()
        with self.assertRaisesRegex(ValueError, 'Missing'):
            self.run_sync()
        self.code['get_s3_client'].assert_not_called()
        self.code['generate_diff'].assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_bad_hash_stops_before_any_cloud_call(self):
        (self.dist/self.model['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            self.run_sync()
        self.assertEqual(self.client.calls, [])

    def test_unsafe_path_rig_size_animation_and_shared_conflicts(self):
        cases = [lambda: self.model.update(path='../outside.glb'),
                 lambda: self.model.update(bytes=True),
                 lambda: self.animation.update(rigKey='2'*64),
                 lambda: self.model['animations'].append(copy.deepcopy(self.animation)),
                 lambda: self.animation.update(duration=float('nan'))]
        original = copy.deepcopy(self.catalogue)
        for mutate in cases:
            self.catalogue = copy.deepcopy(original)
            self.model = self.catalogue['models']['Pony_Test']
            self.animation = self.model['animations'][0]
            mutate(); self.save_catalogue()
            with self.assertRaises(ValueError):
                self.run_sync()
            self.assertEqual(self.client.calls, [])

    def test_asset_symlink_outside_namespace_is_refused(self):
        path = self.dist/self.model['path']
        outside = Path(self.temp.name)/'outside.glb'
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'external'):
            self.run_sync()
        self.assertEqual(self.client.calls, [])

    def test_unknown_owned_namespace_file_refused(self):
        (self.dist/'models/3d/private-audit.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            self.run_sync()
        self.assertEqual(self.client.calls, [])

    def test_binary_put_failure_cannot_publish_catalogue_or_manifest(self):
        self.client.fail_put.add(self.model['path'])
        with self.assertRaisesRegex(RuntimeError, 'put failure'):
            self.run_sync()
        self.assertNotIn('models_3d.json', self.writes())
        self.assert_old_manifest()

    def test_copy_failure_cannot_publish_catalogue_or_manifest(self):
        self.client.objects[self.bucket, self.model['path']] = {
            'Body': (self.dist/self.model['path']).read_bytes(),
            'Metadata': {'hash': self.model['sha256']}, 'ContentType': 'wrong/type'}
        self.client.fail_copy.add(self.model['path'])
        with self.assertRaisesRegex(RuntimeError, 'copy failure'):
            self.run_sync()
        self.assertNotIn('models_3d.json', self.writes())
        self.assert_old_manifest()

    def test_remote_override_with_different_content_blocks_publication(self):
        self.client.objects[self.bucket, self.model['path']] = {
            'Body': b'override', 'Metadata': {'hash': 'f'*64, 'override': 'true'}}
        with self.assertRaisesRegex(ValueError, 'remote override'):
            self.run_sync()
        self.assertNotIn('models_3d.json', self.writes())
        self.assert_old_manifest()

    def test_matching_remote_override_is_preserved(self):
        body = (self.dist/self.model['path']).read_bytes()
        self.client.objects[self.bucket, self.model['path']] = {
            'Body': body, 'Metadata': {'hash': self.model['sha256'], 'override': 'true'},
            'ContentType': 'model/gltf-binary', 'CacheControl': 'max-age=2592000'}
        self.run_sync()
        self.assertNotIn(self.model['path'], self.writes())
        self.assertEqual(self.client.objects[self.bucket, self.model['path']]['Body'], body)

    def test_catalogue_failure_keeps_previous_manifest(self):
        self.client.fail_put.add('models_3d.json')
        with self.assertRaisesRegex(RuntimeError, 'put failure'):
            self.run_sync()
        self.assert_old_manifest()
        self.assertNotIn('manifest.json', self.writes())

    def test_catalogue_override_mismatch_keeps_previous_manifest(self):
        self.client.objects[self.bucket, 'models_3d.json'] = {
            'Body': b'old catalogue', 'Metadata': {'hash': 'f'*64, 'override': 'true'}}
        with self.assertRaisesRegex(ValueError, 'remote override'):
            self.run_sync()
        self.assert_old_manifest()

    def test_catalogue_copy_failure_keeps_previous_manifest(self):
        body = json.dumps(self.catalogue, separators=(',', ':'), ensure_ascii=False).encode()
        self.client.objects[self.bucket, 'models_3d.json'] = {
            'Body': body, 'Metadata': {'hash': hashlib.sha256(body).hexdigest()},
            'ContentType': 'wrong/type'}
        self.client.fail_copy.add('models_3d.json')
        with self.assertRaisesRegex(RuntimeError, 'copy failure'):
            self.run_sync()
        self.assert_old_manifest()
        self.assertNotIn('manifest.json', self.writes())

    def test_root_manifest_failure_is_propagated(self):
        self.client.fail_put.add('manifest.json')
        with self.assertRaisesRegex(RuntimeError, 'put failure'):
            self.run_sync()
        self.assert_old_manifest()
        self.assertIn('models_3d.json', self.writes())
        self.assertTrue(all((self.bucket, ref['path']) in self.client.objects
                            for ref in [self.model, self.animation]))

    def test_no_catalogue_but_glbs_is_incomplete(self):
        (self.dist/'models_3d.json').unlink()
        with self.assertRaisesRegex(ValueError, 'without models_3d'):
            self.run_sync()
        self.assertEqual(self.client.calls, [])

    def test_ordinary_upload_keeps_existing_error_behavior(self):
        self.client.fail_put.add('ordinary.json')
        result = self.code['upload_file'](self.client, self.bucket, b'{}', 'ordinary.json', 'application/json')
        self.assertEqual(result, hashlib.sha256(b'{}').hexdigest())
        self.code['console'].print_exception.assert_called_once()

    def test_ordinary_override_keeps_existing_behavior(self):
        self.client.objects[self.bucket, 'ordinary.json'] = {
            'Body': b'old', 'Metadata': {'hash': 'f'*64, 'override': 'true'},
            'ContentType': 'application/json'}
        self.code['upload_file'](self.client, self.bucket, b'{}', 'ordinary.json', 'application/json')
        self.assertEqual(self.client.objects[self.bucket, 'ordinary.json']['Body'], b'old')

    def test_without_3d_resources_existing_sync_still_publishes_regular_json(self):
        (self.dist/'models_3d.json').unlink()
        for ref in [self.model, self.animation]:
            (self.dist/ref['path']).unlink()
        self.run_sync()
        self.assertEqual(self.writes(), ['game_objects.json', 'manifest.json'])
        manifest = json.loads(self.client.objects[self.bucket, 'manifest.json']['Body'])
        self.assertNotIn('models_3d.json', manifest)

    def test_empty_catalogue_is_a_closed_generation_not_an_error(self):
        self.catalogue['models'] = {}
        self.save_catalogue()
        self.run_sync()
        self.assertNotIn(self.model['path'], self.writes())
        self.assertIn('models_3d.json', self.writes())

    def test_shared_asset_cannot_declare_different_rig(self):
        self.catalogue['models']['Pony_Other'] = copy.deepcopy(self.model)
        self.catalogue['models']['Pony_Other']['rigKey'] = '2'*64
        self.save_catalogue()
        with self.assertRaisesRegex(ValueError, 'Conflicting shared'):
            self.run_sync()
        self.assertEqual(self.client.calls, [])

    def test_mutation_between_preflight_and_upload_is_detected(self):
        _, refs = self.code['prepare_model_upload'](self.dist)
        (self.dist/self.model['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed after'):
            self.code['upload_model_asset'](self.client,self.bucket,self.dist,self.model['path'],refs[self.model['path']])
        self.assertEqual(self.client.calls, [])

    def test_symlink_replacement_after_preflight_is_not_read(self):
        _, refs = self.code['prepare_model_upload'](self.dist)
        path = self.dist/self.model['path']
        outside = Path(self.temp.name)/'outside.glb'
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'left the build'):
            self.code['upload_model_asset'](self.client,self.bucket,self.dist,self.model['path'],refs[self.model['path']])
        self.assertEqual(self.client.calls, [])

    def test_local_manifest_not_queued_before_final_publication(self):
        (self.dist/'manifest.json').write_text('{"old": "stale"}')
        self.run_sync()
        self.assertEqual(self.writes().count('manifest.json'), 1)
        self.assertEqual(self.writes()[-1], 'manifest.json')

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

    def test_schema_two_accepts_ordinary_parent_without_child(self):
        self.catalogue['schemaVersion'] = 2
        self.save_catalogue()
        self.run_sync()
        self.assertIn('models_3d.json', self.writes())

    def test_schema_one_cannot_silently_ignore_additional_actor(self):
        child = self.add_additional_actor()
        self.catalogue['schemaVersion'] = 1
        for value in [child, None]:
            with self.subTest(value=value):
                self.model['additionalActor'] = value
                self.save_catalogue()
                with self.assertRaisesRegex(ValueError, 'schema'):
                    self.run_sync()
                self.assertEqual(self.client.calls, [])

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

    def test_child_missing_or_corrupt_dependencies_prevent_cloud_calls(self):
        child = self.add_additional_actor()
        for ref in [child['model'], child['model']['animations'][0]]:
            path = self.dist/ref['path']; original = path.read_bytes()
            for failure in ['missing', 'corrupt']:
                with self.subTest(ref=ref['path'], failure=failure):
                    if failure == 'missing':
                        path.unlink()
                    else:
                        path.write_bytes(b'corrupt child dependency')
                    with self.assertRaises(ValueError):
                        self.run_sync()
                    self.assertEqual(self.client.calls, [])
                    self.code['generate_diff'].assert_not_called()
                    self.code['get_s3_client'].assert_not_called()
                    path.write_bytes(original)

    def test_child_animation_contract_and_own_rig_are_required(self):
        self.add_additional_actor()
        original = copy.deepcopy(self.model['additionalActor'])
        cases = [lambda c: c['model'].pop('animations'),
                 lambda c: c['model'].update(animations=[]),
                 lambda c: c['model'].update(animations={}),
                 lambda c: c['model']['animations'].append(copy.deepcopy(c['model']['animations'][0])),
                 lambda c: c['model']['animations'][0].update(duration=0),
                 lambda c: c['model']['animations'][0].update(duration=-1),
                 lambda c: c['model']['animations'][0].update(duration=True),
                 lambda c: c['model']['animations'][0].update(duration=float('inf')),
                 lambda c: c['model']['animations'][0].update(rigKey=self.rig),
                 lambda c: c['model']['animations'][0].update(id=''),
                 lambda c: c['model']['animations'][0].update(name='')]
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                child = copy.deepcopy(original); mutate(child)
                self.model['additionalActor'] = child; self.save_catalogue()
                with self.assertRaises(ValueError):
                    self.run_sync()
                self.assertEqual(self.client.calls, [])

    def test_child_wrapper_controller_and_parent_joint_contract(self):
        self.add_additional_actor()
        original = copy.deepcopy(self.model['additionalActor'])
        cases = [None, {}, *[dict(copy.deepcopy(original), parentRoot=value)
                 for value in [-1, True, 1.5, 9007199254740992]],
                 dict(copy.deepcopy(original), controller='unknown'),
                 dict(copy.deepcopy(original), additionalActor={})]
        nested = copy.deepcopy(original); nested['model']['additionalActor'] = None
        cases.append(nested)
        for index, child in enumerate(cases):
            with self.subTest(case=index):
                self.model['additionalActor'] = child; self.save_catalogue()
                with self.assertRaises(ValueError):
                    self.run_sync()
                self.assertEqual(self.client.calls, [])

    def test_child_transform_rejects_non_affine_nonfinite_or_non_uniform_rotation(self):
        child = self.add_additional_actor(); valid = child['transform']
        cases = [None, [], valid[:-1]]
        for index, value in [(0, True), (0, float('nan')), (0, 1e308), (3, .1),
                             (7, .1), (11, .1), (15, 0), (9, .5), (5, 1)]:
            bad = list(valid); bad[index] = value; cases.append(bad)
        zero = list(valid); zero[1] = zero[4] = zero[10] = 0; cases.append(zero)
        uneven = list(valid); uneven[10] = 3; cases.append(uneven)
        reflected = list(valid); reflected[10] = -2; cases.append(reflected)
        for index, matrix in enumerate(cases):
            with self.subTest(case=index):
                child['transform'] = matrix; self.save_catalogue()
                with self.assertRaises(ValueError):
                    self.run_sync()
                self.assertEqual(self.client.calls, [])

    def test_child_binary_failure_keeps_previous_generation_advertised(self):
        child = self.add_additional_actor()
        self.client.fail_put.add(child['model']['animations'][0]['path'])
        with self.assertRaisesRegex(RuntimeError, 'put failure'):
            self.run_sync()
        self.assertNotIn('models_3d.json', self.writes())
        self.assert_old_manifest()

    def test_child_mutation_after_preflight_is_detected(self):
        child = self.add_additional_actor(); ref = child['model']
        _, assets = self.code['prepare_model_upload'](self.dist)
        (self.dist/ref['path']).write_bytes(b'changed child')
        with self.assertRaisesRegex(ValueError, 'changed after'):
            self.code['upload_model_asset'](self.client, self.bucket, self.dist,
                                            ref['path'], assets[ref['path']])
        self.assertEqual(self.client.calls, [])

    def test_actual_generate_diff_reads_all_three_objects_from_passed_bucket(self):
        tree = ast.parse(SOURCE.read_text())
        function = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                        and item.name == 'generate_diff')
        self.code.update(ClientError=ClientError, CATEGORY_NAMES=[],
            GameVersion=SimpleNamespace(model_validate_json=lambda value: SimpleNamespace(**json.loads(value))),
            GameObjects=SimpleNamespace(model_validate_json=lambda value: SimpleNamespace()))
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), 'exec'), self.code)
        (self.dist/'game_version.json').write_text('{"game_version":"new","content_version":"new"}')
        for key, body in {'game_version.json': b'{"game_version":"old","content_version":"old"}',
                          'game_objects.json': b'{}', 'changelog.json': b'{}'}.items():
            self.client.objects[self.bucket, key] = {'Body': body, 'Metadata': {}}
        self.code['generate_diff'](self.dist, self.bucket)
        self.assertEqual(self.client.calls, [('get', self.bucket, 'game_version.json'),
            ('get', self.bucket, 'changelog.json'), ('get', self.bucket, 'game_objects.json')])
        self.assertEqual(json.loads((self.dist/'changelog.json').read_text()),
                         {'new': {'new': {'game_objects': {}}}})


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

    def test_fixed_hex_identity_cache_is_chosen_by_role_not_filename_shape(self):
        identity = 'a'*64
        self.make_fixed(self.model, identity)
        self.catalogue['models'] = {identity: self.model}
        self.save_catalogue(); self.run_sync()
        self.assertEqual(self.client.objects[self.bucket,self.model['path']]['CacheControl'], 'no-cache')

    def test_legacy_model_identity_does_not_need_to_be_a_filename(self):
        self.catalogue['models'] = {'Legacy Model/Identity': self.model}
        self.save_catalogue(); self.run_sync()
        self.assertEqual(self.client.objects[self.bucket,self.model['path']]['CacheControl'], 'max-age=2592000')

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

    def test_fixed_path_is_not_allowed_for_animation_or_anonymous_child(self):
        child = self.add_additional_actor()
        for ref, name in [(self.animation,self.animation['id']), (child['model'],'Invented_Child')]:
            original = ref['path']; self.make_fixed(ref,name); self.save_catalogue()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'path'):
                self.run_sync()
            ref['path'] = original
        self.assertEqual(self.client.calls, [])

    def make_named(self, ref, name):
        old = self.dist/ref['path']
        ref['path'] = f"models/3d/{ref['sha256']}/{name}.glb"
        target = self.dist/ref['path'];target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old.read_bytes())

    def test_named_catalogue_keeps_cold_legacy_references_valid(self):
        self.run_sync()
        old_catalogue = json.loads(self.client.objects[self.bucket, 'models_3d.json']['Body'])
        old_refs = [old_catalogue['models']['Pony_Test'], *old_catalogue['models']['Pony_Test']['animations']]
        old_bytes = {ref['path']: self.client.objects[self.bucket, ref['path']]['Body'] for ref in old_refs}
        self.make_named(self.model, 'Pony_Test');self.make_named(self.animation, self.animation['id'])
        self.save_catalogue();self.run_sync()
        for ref in old_refs:
            self.assertEqual(self.client.objects[self.bucket, ref['path']]['Body'], old_bytes[ref['path']])
        for old, new in zip(old_refs, [self.model,self.animation]):
            self.assertEqual(self.client.objects[self.bucket,new['path']]['Body'],old_bytes[old['path']])
        self.assertEqual(json.loads(self.client.objects[self.bucket, 'models_3d.json']['Body']), self.catalogue)

    def test_named_reference_requires_exact_identity_clip_and_directory_digest(self):
        original = copy.deepcopy(self.catalogue)
        for target, name in [('model','Pony_Other'),('animation','wrong_clip')]:
            self.catalogue = copy.deepcopy(original);self.model = self.catalogue['models']['Pony_Test'];self.animation = self.model['animations'][0]
            self.make_named(self.model if target=='model' else self.animation, name)
            self.save_catalogue()
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'path'):
                self.run_sync()
            self.assertEqual(self.client.calls, [])
        self.catalogue=copy.deepcopy(original);self.model=self.catalogue['models']['Pony_Test'];self.animation=self.model['animations'][0]
        self.model['path']='models/3d/'+'f'*64+'/Pony_Test.glb';self.save_catalogue()
        with self.assertRaisesRegex(ValueError, 'path'):
            self.run_sync()
        self.assertEqual(self.client.calls, [])

    def test_named_additional_animation_keeps_anonymous_model_flat(self):
        child = self.add_additional_actor()
        self.make_named(self.model, 'Pony_Test')
        self.make_named(child['model']['animations'][0], 'idle')
        self.save_catalogue()
        self.run_sync()
        self.assertIn(child['model']['path'], self.writes())
        self.assertIn(child['model']['animations'][0]['path'], self.writes())

    def test_named_additional_model_cannot_invent_an_object_identity(self):
        child = self.add_additional_actor()
        self.make_named(child['model'], 'Invented_Object')
        self.save_catalogue()
        with self.assertRaisesRegex(ValueError, 'identity, size or path'):
            self.run_sync()
        self.assertEqual(self.client.calls, [])

    def test_named_put_failure_preserves_previous_catalogue_and_manifest(self):
        self.run_sync()
        old_catalogue=self.client.objects[self.bucket,'models_3d.json']['Body']
        old_manifest=self.client.objects[self.bucket,'manifest.json']['Body']
        self.make_named(self.model,'Pony_Test');self.make_named(self.animation,self.animation['id']);self.save_catalogue()
        self.client.fail_put.add(self.model['path'])
        with self.assertRaisesRegex(RuntimeError,'put failure'):
            self.run_sync()
        self.assertEqual(self.client.objects[self.bucket,'models_3d.json']['Body'],old_catalogue)
        self.assertEqual(self.client.objects[self.bucket,'manifest.json']['Body'],old_manifest)


if __name__ == '__main__':
    unittest.main()
