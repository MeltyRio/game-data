"""Self-authored shader fixtures: only reviewed hash constants are mocked."""
from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from game_data.model_gen.converter import ConversionError, from_native, model_glb
from game_data.model_gen.converter.native import NativeInputError, load_native
from converter.test_native_adapter import fixture
from converter.test_zero_weight_triangles import add_triangles


class NativeHornTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        fixture(self.folder)
        self.digests = {}
        for name in ('rkshader_horn.rkvs', 'rkshader_horn.rkps', 'rkshadercore.rkvs'):
            payload = ('self-authored fixture: ' + name).encode()
            (self.folder / name).write_bytes(payload)
            self.digests[name] = hashlib.sha256(payload).hexdigest()
        self.mock = patch('game_data.model_gen.converter.native._HORN_SHADER_SOURCES', self.digests, create=True)
        self.mock.start()
        rkm = self.folder / 'body.rkm'
        rkm.write_text(rkm.read_text().replace('Shader=rkshader', 'Shader=rkshader_horn').replace('BlendMode=none', 'BlendMode=alpha'))

    def tearDown(self):
        self.mock.stop()
        self.temp.cleanup()

    def load(self, visible=('body',)):
        return load_native(self.folder / 'tiny.rk', self.folder / 'tiny.anim',
                           self.folder / 'tiny.csv', ['move'], visible_meshes=visible,
                           rest_name='rest', event_path=self.folder / 'tiny.xml')

    def document(self, native):
        model, clips = from_native(native, identity='SyntheticHorn')
        raw = model_glb(model)
        return model, clips, json.loads(raw[20:20 + struct.unpack_from('<I', raw, 12)[0]])

    def test_default_gpu_zero_is_a_discard_without_removing_geometry(self):
        native = self.load()
        model, clips, document = self.document(native)
        material = document['materials'][0]
        self.assertEqual(material['alphaMode'], 'MASK')
        self.assertEqual(material['alphaCutoff'], .001)
        self.assertEqual(material['pbrMetallicRoughness']['baseColorFactor'], [1, 1, 1, 0])
        self.assertEqual(len(model.positions), 3)
        np.testing.assert_array_equal(model.primitives[0]['indices'], [[0, 1, 2]])
        np.testing.assert_array_equal(model.weights[:, 0], [1, 1, 1])
        self.assertEqual(len(clips), 1)
        self.assertEqual(len(document['images']), 1)
        self.assertEqual(material['extras']['atpNativeDepth'], {'write': True, 'test': True})
        self.assertEqual(native['materials'][0]['native_horn_default_discard'], True)
        self.assertTrue(set(self.digests).issubset({p['name'] for p in native['provenance']['files']}))

    def test_regular_and_horn_materials_keep_separate_states(self):
        # Second material inherits the horn source, first becomes ordinary.
        add_triangles(self.folder, separate=True, weight=65535)
        rkm = self.folder / 'body.rkm'
        rkm.write_text(rkm.read_text().replace('rkshader_horn', 'rkshader'))
        native = self.load(('body', 'degenerate'))
        model, _, document = self.document(native)
        self.assertEqual(len(model.positions), 7)
        self.assertEqual(document['materials'][0]['alphaMode'], 'BLEND')
        self.assertNotIn('baseColorFactor', document['materials'][0]['pbrMetallicRoughness'])
        self.assertEqual(document['materials'][1]['alphaMode'], 'MASK')
        self.assertEqual(len(document['meshes'][0]['primitives']), 2)

    def test_missing_or_changed_shader_sources_are_rejected(self):
        for name in self.digests:
            with self.subTest(name=name):
                path = self.folder / name
                original = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(NativeInputError, 'Missing native dependency'):
                    self.load()
                path.write_bytes(original + b' changed')
                with self.assertRaisesRegex(NativeInputError, 'Unreviewed native horn shader'):
                    self.load()
                path.write_bytes(original)

    def test_explicit_vector_is_not_silently_replaced_by_default(self):
        path = self.folder / 'body.rkm'
        original = path.read_text()
        for field in ('UserVector0_0=0', 'UserVector0_0=1', 'UserVector1_0=0', 'UserVectorFuture=0'):
            with self.subTest(field=field):
                path.write_text(original + field + '\n')
                with self.assertRaisesRegex(NativeInputError, 'Explicit native horn UserVector|Unsupported material property'):
                    self.load()
        path.write_text(original)

    def test_special_shader_does_not_inherit_ordinary_zero_weight_rule(self):
        add_triangles(self.folder)
        with self.assertRaisesRegex(NativeInputError, 'require the reviewed ordinary shader'):
            self.load()

    def test_serialization_rechecks_explicit_vector(self):
        native = self.load()
        native['materials'][0]['rkm_fields']['UserVector0_0'] = '1'
        with self.assertRaisesRegex(ConversionError, 'Unverified native horn default'):
            self.document(native)

    def test_horn_animation_event_is_still_rejected(self):
        (self.folder / 'tiny.xml').write_text('<MeshList/><AnimationList><Animation Name="move"><Frame Index="1"><UserVector X="1"/></Frame></Animation></AnimationList>')
        with self.assertRaisesRegex(NativeInputError, 'requires unsupported native events'):
            self.load()

    def test_unverified_serialization_cannot_opt_in_by_shader_name(self):
        path = self.folder / 'body.rkm'
        path.write_text(path.read_text().replace('rkshader_horn', 'rkshader'))
        native = self.load()
        native['materials'][0]['resolved_shader'] = 'rkshader_horn'
        with self.assertRaisesRegex(ConversionError, 'Unverified native horn default'):
            self.document(native)

    def test_horn_shader_symlink_cannot_escape_dependency_closure(self):
        path = self.folder / 'rkshader_horn.rkps'
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / path.name
            target.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(target)
            with self.assertRaisesRegex(NativeInputError, 'leaves its directory'):
                self.load()
