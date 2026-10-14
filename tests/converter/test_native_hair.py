"""Native UV clocks, using self-authored assets and mock reviewed digests."""
from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_data.model_gen.converter import from_native, model_glb
from game_data.model_gen.converter.native import load_native
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
