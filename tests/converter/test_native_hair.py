"""Native UV clocks, using self-authored assets and mock reviewed digests."""
from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_data.model_gen.converter import ConversionError, from_native, model_glb
from game_data.model_gen.converter.export import convert_native
from game_data.model_gen.converter.native import NativeInputError, load_native
from converter.test_native_adapter import fixture
from converter.test_zero_weight_triangles import add_triangles


class NativeHairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        fixture(self.folder)
        add_triangles(self.folder, separate=True, weight=65535)
        path = self.folder / 'degenerate.rkm'
        path.write_text(path.read_text().replace('rkshader', 'rkponyhair0') + 'UserVector0_0=-0.528\n')
        self.digests = {}
        for name in ('rkponyhair0.rkvs', 'rkponyhair0.rkps', 'rkshadercore.rkvs', 'rkshader.rkvs', 'rkshader.rkps'):
            data = ('self-authored fixture ' + name).encode()
            (self.folder / name).write_bytes(data)
            self.digests[name] = hashlib.sha256(data).hexdigest()
        self.mock = patch('game_data.model_gen.converter.native._HAIR_SHADER_SOURCES', self.digests, create=True)
        self.mock.start()
        self.controller = {'UserVectorSpeed': .15, 'RangeMin': -2, 'RangeMax': 2}

    def tearDown(self):
        self.mock.stop()
        self.temp.cleanup()

    def load(self, visible=('body', 'degenerate'), **kwargs):
        return load_native(self.folder / 'tiny.rk', self.folder / 'tiny.anim', self.folder / 'tiny.csv', ['move'],
                           visible_meshes=visible, rest_name='rest', event_path=self.folder / 'tiny.xml',
                           user_vector=kwargs.pop('user_vector', self.controller), **kwargs)

    def document(self, native):
        model, _ = from_native(native, identity='SyntheticHair')
        raw = model_glb(model)
        return json.loads(raw[20:20 + struct.unpack_from('<I', raw, 12)[0]])

    def test_authored_gpu_offset_and_first_ordinary_cpu_clock_are_distinct(self):
        native = self.load()
        document = self.document(native)
        ordinary, hair = document['materials']
        self.assertNotIn('atpNativeUVClock', ordinary['extras'])
        self.assertNotIn('extensions', ordinary['pbrMetallicRoughness']['baseColorTexture'])
        self.assertEqual(hair['pbrMetallicRoughness']['baseColorTexture']['extensions']['KHR_texture_transform']['offset'], [0, .528])
        self.assertEqual(hair['extras']['atpNativeUVClock'], {'version': 1, 'initial': 1, 'speed': .15, 'min': -2, 'max': 2})
        self.assertIn('KHR_texture_transform', document['extensionsUsed'])
        self.assertTrue(set(self.digests).issubset({p['name'] for p in native['provenance']['files']}))

    def test_hidden_first_material_remains_clock_source_and_dependency(self):
        path = self.folder / 'body.rkm'
        path.write_text(path.read_text() + 'UserVector0_0=.75\n')
        native = self.load(('degenerate',))
        document = self.document(native)
        self.assertEqual(len(document['materials']), 1)
        self.assertEqual(document['materials'][0]['extras']['atpNativeUVClock']['initial'], .75)
        self.assertIn('body.rkm', {p['name'] for p in native['provenance']['files']})

    def test_absent_gpu_vector_is_zero_but_cpu_clock_remains_one(self):
        for name in ('body.rkm', 'degenerate.rkm'):
            path = self.folder / name
            path.write_text(path.read_text().replace('Shader=rkshader', 'Shader=rkponyhair0').replace('UserVector0_0=-0.528\n', ''))
        document = self.document(self.load())
        for material in document['materials']:
            self.assertEqual(material['pbrMetallicRoughness']['baseColorTexture']['extensions']['KHR_texture_transform']['offset'], [0, 0])
            self.assertEqual(material['extras']['atpNativeUVClock']['initial'], 1)

    def test_missing_or_invalid_controller_is_rejected(self):
        for controller in (None, {}, {'UserVectorSpeed': True, 'RangeMin': 0, 'RangeMax': 1},
                           {**self.controller, 'Future': 1}, {**self.controller, 'RangeMin': 3},
                           {**self.controller, 'UserVectorSpeed': -1}, {**self.controller, 'RangeMax': float('nan')}):
            with self.subTest(controller=controller), self.assertRaisesRegex(NativeInputError, 'native UserVector'):
                self.load(user_vector=controller)

    def test_explicit_disabled_clock_retains_authored_static_offset(self):
        document = self.document(self.load(user_vector={**self.controller, 'UserVectorSpeed': 0}))
        self.assertNotIn('atpNativeUVClock', document['materials'][1]['extras'])
        self.assertEqual(document['materials'][1]['pbrMetallicRoughness']['baseColorTexture']['extensions']['KHR_texture_transform']['offset'], [0, .528])

    def test_unsupported_vector_encoding_is_not_guessed(self):
        path = self.folder / 'body.rkm'
        path.write_text(path.read_text() + 'UserVector0_1=4\n')
        with self.assertRaisesRegex(NativeInputError, 'Unsupported material property'):
            self.load()

    def test_missing_and_changed_sources_fail_closed(self):
        for name in self.digests:
            with self.subTest(name=name):
                path = self.folder / name
                raw = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(NativeInputError, 'Missing native dependency'):
                    self.load()
                path.write_bytes(raw + b' changed')
                with self.assertRaisesRegex(NativeInputError, 'Unreviewed native hair shader'):
                    self.load()
                path.write_bytes(raw)

    def test_public_export_forwards_controller(self):
        bundle = convert_native(self.folder / 'tiny.rk', self.folder / 'tiny.anim', self.folder / 'tiny.csv', ['move'],
                                identity='SyntheticHair', visible_meshes=['body', 'degenerate'], rest_name='rest',
                                event_path=self.folder / 'tiny.xml', user_vector=self.controller)
        self.assertEqual(len(bundle.catalogue['models']['SyntheticHair']['animations']), 1)
        self.assertEqual(bundle.catalogue['schemaVersion'], 2)
        self.assertTrue(bundle.provenance['material_uv_clock'])

    def test_serializer_rejects_unverified_hair_and_invalid_clock(self):
        native = self.load()
        native['materials'][1].pop('native_hair_uv')
        with self.assertRaisesRegex(ConversionError, 'Unverified native hair'):
            self.document(native)
        native = self.load()
        native['materials'][1]['native_hair_uv']['clock']['speed'] = float('nan')
        with self.assertRaisesRegex(ConversionError, 'Invalid native UV clock'):
            self.document(native)
