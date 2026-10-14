"""Native material behavior from self-authored resources, without shader files."""
from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from game_data.model_gen.converter import from_native, model_glb
from game_data.model_gen.converter.native import load_native
from converter.test_native_adapter import fixture


class NativeHornTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        fixture(self.folder)
        rkm = self.folder / 'body.rkm'
        rkm.write_text(rkm.read_text().replace('Shader=rkshader', 'Shader=rkshader_horn').replace('BlendMode=none', 'BlendMode=alpha'))

    def tearDown(self):
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
