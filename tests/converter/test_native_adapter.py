"""Tiny synthetic RK/ANIM dependency closure; no proprietary fixture needed."""
from __future__ import annotations

import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from game_data.model_gen.converter import from_native, model_glb
from game_data.model_gen.converter.native import NativeInputError, load_native


def fixture(folder):
    def text(value):
        return value.encode().ljust(64, b'\0')
    parts = [
        (1, 1, struct.pack('<4I', 1, 0, 0, 0)),
        (2, 1, text('body')),
        (3, 3, b''.join(struct.pack('<5f', *vertex) for vertex in [(0, 0, 0, 0, 0), (1, 0, 0, 1, 0), (0, 1, 0, 0, 1)])),
        (4, 3, struct.pack('<3H', 0, 1, 2)),
        (7, 1, struct.pack('<3i', -1, 0, 0) + np.eye(4, dtype='<f4').tobytes() + text('root')),
        (13, 1, struct.pack('<H2B', 1026, 12, 0)),
        (16, 1, text('body')),
        (17, 3, struct.pack('<4B4H', 0, 0, 0, 0, 65535, 0, 0, 0) * 3),
    ]
    offset = 80 + 24 * 16
    entries = []
    body = bytearray()
    for tag, count, data in parts:
        entries.append(struct.pack('<4I', tag, offset, count, len(data)))
        body.extend(data)
        offset += len(data)
    rk = struct.pack('<8sII64s', b'RKFORMAT', 5, 2, text('tiny')) + b''.join(entries).ljust(24 * 16, b'\0') + body
    (folder / 'tiny.rk').write_bytes(rk)
    anim = struct.pack('<8sII64sIII', b'RKFORMAT', 5, 2, text('tiny'), 1, 3, 4)
    anim += b''.join(struct.pack('<3hh3b', x, 0, 0, 32767, 0, 0, 0) for x in (0, 32, 64))
    (folder / 'tiny.anim').write_bytes(anim)
    (folder / 'tiny.xml').write_text('<?xml version="1.0"?><MeshList/><AnimationList/>')
    (folder / 'tiny.csv').write_text('rest,0,0,15\nmove,0,2,15\nmove,0,2,15\n')
    (folder / 'body.rkm').write_text('DiffuseTexture=body\nShader=rkshader\nNoCompress=true\nClampMode=RK_REPEAT\nBlendMode=none\nCull=1\n')
    Image.new('RGBA', (2, 2), (255, 0, 0, 255)).save(folder / 'body.png')


class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        fixture(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def load(self):
        return load_native(self.folder / 'tiny.rk', self.folder / 'tiny.anim', self.folder / 'tiny.csv', ['move'],
                           visible_meshes=['body'], rest_name='rest', event_path=self.folder / 'tiny.xml')


    def test_native_component_weights_survive_conversion_and_standard_transport(self):
        from .test_converter import unpack
        from .test_export import accessor

        # A real Coiffed weight row, with the fixture's one valid joint. This
        # also exercises glTF's requirement to merge duplicate influences.
        path = self.folder / 'tiny.rk'
        raw = bytearray(path.read_bytes())
        for i in range(24):
            tag, offset, _, _ = struct.unpack_from('<4I', raw, 80 + 16 * i)
            if tag == 17:
                struct.pack_into('<4B4H', raw, offset, 0, 0, 0, 0, 16048, 8845, 13012, 4287)
                break
        path.write_bytes(raw)
        native = self.load()
        model, _ = from_native(native, identity='NativeComponents')
        np.testing.assert_array_equal(model.weights, native['weights'])
        self.assertAlmostEqual(model.weights[0].sum(), 42192 / 65535)
        document, binary = unpack(model_glb(model))
        attrs = document['meshes'][0]['primitives'][0]['attributes']
        np.testing.assert_array_equal(accessor(document, binary, attrs['_NATIVE_WEIGHTS']), model.weights.astype('float32'))
        np.testing.assert_array_equal(accessor(document, binary, attrs['_NATIVE_JOINTS']), model.joints)
        np.testing.assert_array_equal(accessor(document, binary, attrs['WEIGHTS_0']), [[1, 0, 0, 0]] * 3)

    def test_absent_rkm_reuses_luna_and_native_default_material_state(self):
        from luna_kit.model.rk import parse_rkm
        for alpha in (255, 64):
            with self.subTest(alpha=alpha):
                fixture(self.folder)
                _, mask = self.pvr_fixture(embedded_alpha=alpha)
                mask.unlink()
                (self.folder / 'body.rkm').unlink()
                with patch('game_data.model_gen.converter.native.parse_rkm', wraps=parse_rkm) as parser:
                    native = self.load()
                parser.assert_called_once_with(str((self.folder / 'body.rkm').resolve()))
                self.assertEqual(native['materials'][0]['rkm_fields'], {})
                self.assertEqual(native['materials'][0]['resolved_shader'], 'rkshader')
                self.assertTrue(native['materials'][0]['native_default_shader'])
                with Image.open(io.BytesIO(native['materials'][0]['png_bytes'])) as image:
                    self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, alpha))
                source = native['provenance']['material_sources'][0]
                self.assertEqual(source['name'], 'body.rkm')
                self.assertEqual(source['status'], 'native-default-without-rkm')
                self.assertEqual(source['client_version'], '11.4.3c')
                names = {row['name'] for row in native['provenance']['files']}
                self.assertIn('body.pvr', names)
                self.assertNotIn('body.rkm', names)
                self.assertFalse((self.folder / 'body.rkm').exists())
                model, _ = from_native(native, identity='SyntheticDefaultMaterial')
                glb = model_glb(model)
                document = json.loads(glb[20:20 + struct.unpack_from('<I', glb, 12)[0]])
                material = document['materials'][0]
                self.assertEqual(material['alphaMode'], 'OPAQUE')
                self.assertFalse(material['doubleSided'])
                self.assertEqual(material['extras']['atpNativeDepth'], {'write': True, 'test': True})
                self.assertEqual(document['samplers'][0]['wrapS'], 10497)
                self.assertEqual(document['samplers'][0]['wrapT'], 10497)


    def test_present_invalid_rkm_never_uses_default_texture(self):
        self.pvr_fixture()
        rkm = self.folder / 'body.rkm'
        for data in ('not a property', 'DiffuseTexture=body\nDiffuseTexture=body\n',
                     'DiffuseTexture=body\nUnknownNativeField=1\n'):
            with self.subTest(data=data):
                rkm.write_text(data)
                with self.assertRaisesRegex(NativeInputError, 'material propert'):
                    self.load()


    def pvr_fixture(self, embedded_alpha=255):
        header = struct.pack('<4sI4s4s9I', b'PVR\x03', 0, b'rgba', bytes([8] * 4),
                             0, 0, 2, 2, 1, 1, 1, 1, 0)
        primary = self.folder / 'body.pvr'
        primary.write_bytes(header + bytes([255, 0, 0, embedded_alpha]) * 4)
        mask = self.folder / 'body.alpha.pvr'
        mask.write_bytes(header + bytes([128, 128, 128, 255]) * 4)
        rkm = self.folder / 'body.rkm'
        rkm.write_text(rkm.read_text().replace('NoCompress=true', 'NoCompress=false'))
        return primary, mask


if __name__ == '__main__':
    unittest.main()
