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
from game_data.model_gen.converter.native import NativeInputError, load_native, NATIVE_POSITION_SCALE


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

    def test_public_parsers_geometry_skin_animation_and_material(self):
        model = self.load()
        np.testing.assert_array_equal(model['positions'], [[0, 0, 0], [1, 0, 0], [0, 1, 0]])
        np.testing.assert_array_equal(model['texcoords'], [[0, 0], [1, 0], [0, 1]])
        np.testing.assert_array_equal(model['weights'][:, 0], [1, 1, 1])
        np.testing.assert_array_equal(model['bones'][0]['bind_global'], np.eye(4))
        np.testing.assert_allclose(model['clips'][0]['translations'][:, 0, 0], [0, 32 * NATIVE_POSITION_SCALE, 64 * NATIVE_POSITION_SCALE], rtol=0, atol=0)
        np.testing.assert_array_equal(model['clips'][0]['rotations_xyzw'][:, 0], [[0, 0, 0, 1]] * 3)
        self.assertEqual(model['provenance']['duplicate_csv_rows_merged'], 1)
        self.assertTrue(model['materials'][0]['png_bytes'].startswith(b'\x89PNG'))
        self.assertEqual(model['rest']['frame'], 0)
        converted, _ = from_native(model, identity='SyntheticNative')
        self.assertTrue(model_glb(converted).startswith(b'glTF'))

    def test_material_name_records_use_declared_variable_stride(self):
        path = self.folder / 'tiny.rk'
        raw = bytearray(path.read_bytes())
        records = [struct.unpack_from('<4I', raw, 80 + i * 16) for i in range(24)]
        material = next(row for row in records if row[0] == 2)
        end = material[1] + material[3]
        padding = bytes(400 - material[3])
        raw[end:end] = padding
        for i, (tag, offset, count, size) in enumerate(records):
            if tag == 2:
                struct.pack_into('<I', raw, 80 + i * 16 + 12, 400)
            elif tag and offset >= end:
                struct.pack_into('<I', raw, 80 + i * 16 + 4, offset + len(padding))
        path.write_bytes(raw)
        self.assertEqual(self.load()['materials'][0]['name'], 'body')

    def test_native_pvr_alpha_is_decoded(self):
        pixels = bytes([255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 0, 255, 255, 255, 255])
        header = struct.pack('<4sI4s4s9I', b'PVR\x03', 0, b'rgba', bytes([8] * 4), 0, 0, 2, 2, 1, 1, 1, 1, 0)
        (self.folder / 'body.pvr').write_bytes(header + pixels)
        path = self.folder / 'body.rkm'
        path.write_text(path.read_text().replace('NoCompress=true', 'NoCompress=false'))
        model = self.load()
        import io
        with Image.open(io.BytesIO(model['materials'][0]['png_bytes'])) as image:
            self.assertEqual(image.tobytes(), pixels)

    def test_selected_event_cannot_be_silently_dropped(self):
        (self.folder / 'tiny.xml').write_text('<MeshList/><AnimationList><Animation Name="move"><Frame Index="1"><EyeSet Open="Happy"/></Frame></Animation></AnimationList>')
        with self.assertRaisesRegex(NativeInputError, 'requires unsupported native events'):
            self.load()

    def test_unselected_event_is_not_overrestricted(self):
        (self.folder / 'tiny.xml').write_text('<MeshList/><AnimationList><Animation Name="unused"><Frame Index="1"><EyeSet/></Frame></Animation></AnimationList>')
        self.assertEqual(self.load()['provenance']['event_source']['status'], 'parsed-source-xml')

    def test_event_source_must_be_explicit(self):
        with self.assertRaisesRegex(NativeInputError, 'Supply an event XML'):
            load_native(self.folder / 'tiny.rk', self.folder / 'tiny.anim', self.folder / 'tiny.csv', ['move'], visible_meshes=['body'], rest_name='rest')

    def test_confirmed_absence_is_recorded_separately(self):
        model = load_native(self.folder / 'tiny.rk', self.folder / 'tiny.anim', self.folder / 'tiny.csv', ['move'], visible_meshes=['body'], rest_name='rest', events_absent=True)
        self.assertEqual(model['provenance']['event_source']['status'], 'source-absence-confirmed-by-caller')

    def test_event_dtd_is_rejected(self):
        (self.folder / 'tiny.xml').write_text('<!DOCTYPE foo [<!ENTITY ext SYSTEM "file:///etc/passwd">]><AnimationList/>')
        with self.assertRaisesRegex(NativeInputError, 'DTD/entity'):
            self.load()

    def test_conflicting_csv_is_rejected(self):
        (self.folder / 'tiny.csv').write_text('rest,0,0,15\nmove,0,2,15\nmove,0,1,15\n')
        with self.assertRaisesRegex(NativeInputError, 'Conflicting'):
            self.load()

    def test_unselected_invalid_csv_does_not_block_valid_clip(self):
        path = self.folder / 'tiny.csv'
        path.write_text(path.read_text() + 'unselected,9999,10000,15\n')
        model = self.load()
        self.assertEqual(model['provenance']['unselected_csv_diagnostics'][0]['name'], 'unselected')

    def test_selected_invalid_csv_is_rejected(self):
        (self.folder / 'tiny.csv').write_text('rest,0,0,15\nmove,0,9999,15\n')
        with self.assertRaisesRegex(NativeInputError, 'Invalid selected'):
            self.load()

    def test_unsupported_anim_layout_is_rejected_before_decode(self):
        path = self.folder / 'tiny.anim'
        raw = bytearray(path.read_bytes())
        struct.pack_into('<I', raw, 88, 99)
        path.write_bytes(raw)
        with self.assertRaisesRegex(NativeInputError, 'Unsupported animation'):
            self.load()

    def test_unsupported_shader_is_explicit(self):
        path = self.folder / 'body.rkm'
        path.write_text(path.read_text().replace('rkshader', 'unknown-shader'))
        with self.assertRaisesRegex(NativeInputError, 'Unsupported native shader'):
            self.load()

    def test_no_guessed_visible_mesh(self):
        with self.assertRaisesRegex(NativeInputError, 'visible submesh'):
            load_native(self.folder / 'tiny.rk', self.folder / 'tiny.anim', self.folder / 'tiny.csv', ['move'], visible_meshes=[], rest_name='rest', event_path=self.folder / 'tiny.xml')

    def test_material_source_art_suffix_uses_same_stem_only(self):
        path = self.folder / 'tiny.rk'
        raw = path.read_bytes().replace(b'body' + b'\0' * 60, b'body.psd' + b'\0' * 56)
        path.write_bytes(raw)
        model = load_native(path, self.folder / 'tiny.anim', self.folder / 'tiny.csv', ['move'], visible_meshes=['body.psd'], rest_name='rest', event_path=self.folder / 'tiny.xml')
        self.assertEqual(model['materials'][0]['name'], 'body.psd')
        self.assertIn('body.rkm', [row['name'] for row in model['provenance']['files']])

    def test_unknown_material_suffix_has_no_guess(self):
        from game_data.model_gen.converter.native import material_dependency_names
        self.assertEqual(material_dependency_names('body.unconfirmed'), ('body.unconfirmed.rkm', ))

    def test_legacy_model_versions_use_verified_section_layout(self):
        path = self.folder / 'tiny.rk'
        original = path.read_bytes()
        for version in (0, 1):
            raw = bytearray(original)
            struct.pack_into('<I', raw, 12, version)
            path.write_bytes(raw)
            self.assertEqual(len(self.load()['positions']), 3)

    def test_invalid_model_layout_still_rejected_on_known_version(self):
        path = self.folder / 'tiny.rk'
        raw = bytearray(path.read_bytes())
        # A valid version cannot authorize a different skin record layout.
        for index in range(24):
            at = 80 + index * 16
            if struct.unpack_from('<I', raw, at)[0] == 17:
                struct.pack_into('<I', raw, at + 12, 33)
        path.write_bytes(raw)
        with self.assertRaisesRegex(NativeInputError, 'section layout: 17'):
            self.load()

    def test_zero_weights_are_not_assigned_to_an_arbitrary_bone(self):
        path = self.folder / 'tiny.rk'
        raw = bytearray(path.read_bytes())
        for index in range(24):
            at = 80 + index * 16
            if struct.unpack_from('<I', raw, at)[0] == 17:
                offset = struct.unpack_from('<I', raw, at + 4)[0]
                struct.pack_into('<4B4H', raw, offset, *([0] * 8))
        path.write_bytes(raw)
        with self.assertRaisesRegex(NativeInputError, 'Zero-weight vertices'):
            self.load()

    def test_missing_material_is_not_invented(self):
        (self.folder / 'body.rkm').unlink()
        with self.assertRaisesRegex(NativeInputError, 'Missing native dependency'):
            self.load()

    def test_absent_rkm_reuses_luna_and_native_default_material_state(self):
        from luna_kit.model.rk import parse_rkm
        for alpha in (255, 64):
            with self.subTest(alpha=alpha):
                fixture(self.folder)
                _, mask = self.pvr_fixture(embedded_alpha=alpha)
                mask.unlink()
                (self.folder / 'body.rkm').unlink()
                with patch('luna_kit.model.rk.parse_rkm', wraps=parse_rkm) as parser:
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

    def test_absent_rkm_requires_exact_pvr_not_png_or_other_texture(self):
        primary, mask = self.pvr_fixture()
        mask.unlink()
        primary.rename(self.folder / 'other.pvr')
        (self.folder / 'body.rkm').unlink()
        # The original body.png still exists; neither available file substitutes.
        with self.assertRaisesRegex(NativeInputError, 'Missing native dependency: body.pvr'):
            self.load()

    def test_absent_source_art_rkm_keeps_original_texture_name(self):
        primary, mask = self.pvr_fixture()
        mask.unlink()
        (self.folder / 'body.rkm').unlink()
        path = self.folder / 'tiny.rk'
        path.write_bytes(path.read_bytes().replace(b'body' + b'\0' * 60, b'body.psd' + b'\0' * 56))
        def load():
            return load_native(path, self.folder / 'tiny.anim', self.folder / 'tiny.csv', ['move'],
                               visible_meshes=['body.psd'], rest_name='rest', event_path=self.folder / 'tiny.xml')
        with self.assertRaisesRegex(NativeInputError, 'Missing native dependency: body.psd.pvr'):
            load()
        primary.rename(self.folder / 'body.psd.pvr')
        native = load()
        self.assertIn('body.psd.pvr', {row['name'] for row in native['provenance']['files']})
        self.assertEqual(native['provenance']['material_sources'][0]['name'], 'body.psd.rkm')

    def test_existing_rkm_wins_over_same_name_default_texture(self):
        before = self.load()
        model, _ = from_native(before, identity='SyntheticExplicitMaterial')
        original = model_glb(model)
        header = struct.pack('<4sI4s4s9I', b'PVR\x03', 0, b'rgba', bytes([8] * 4),
                             0, 0, 2, 2, 1, 1, 1, 1, 0)
        (self.folder / 'body.pvr').write_bytes(header + bytes([0, 255, 0, 255]) * 4)
        after = self.load()
        model, _ = from_native(after, identity='SyntheticExplicitMaterial')
        self.assertEqual(model_glb(model), original)
        self.assertEqual(after['provenance'], before['provenance'])
        self.assertEqual(after['provenance']['material_sources'],
                         [{'name': 'body.rkm', 'status': 'parsed-source-rkm'}])

    def test_present_invalid_rkm_never_uses_default_texture(self):
        self.pvr_fixture()
        rkm = self.folder / 'body.rkm'
        for data in ('not a property', 'DiffuseTexture=body\nDiffuseTexture=body\n',
                     'DiffuseTexture=body\nUnknownNativeField=1\n'):
            with self.subTest(data=data):
                rkm.write_text(data)
                with self.assertRaisesRegex(NativeInputError, 'material propert'):
                    self.load()

    def test_non_file_or_escaped_rkm_is_not_absence(self):
        self.pvr_fixture()
        rkm = self.folder / 'body.rkm'
        rkm.unlink()
        rkm.mkdir()
        with self.assertRaisesRegex(NativeInputError, 'Native dependency is not a file'):
            self.load()
        rkm.rmdir()
        with tempfile.TemporaryDirectory() as outside:
            rkm.symlink_to(Path(outside) / 'missing.rkm')
            with self.assertRaisesRegex(NativeInputError, 'leaves its directory'):
                self.load()
        rkm.unlink()
        rkm.symlink_to(self.folder / 'missing.rkm')
        with self.assertRaisesRegex(NativeInputError, 'Native dependency is not a file'):
            self.load()

    def test_first_material_lookup_for_hair_remains_strict(self):
        from game_data.model_gen.converter.native import _material_sibling
        self.pvr_fixture()
        (self.folder / 'body.rkm').unlink()
        with self.assertRaisesRegex(NativeInputError, 'Missing native dependency: body.rkm'):
            _material_sibling(self.folder, 'body')

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

    def test_pvr_alpha_sidecar_matches_luna_pixels(self):
        import io
        from luna_kit.pvr import PVR
        primary, _ = self.pvr_fixture()
        expected = PVR(str(primary)).image.convert('RGBA').tobytes()
        model = self.load()
        with Image.open(io.BytesIO(model['materials'][0]['png_bytes'])) as image:
            self.assertEqual(image.tobytes(), expected)
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 128))

    def test_pvr_embedded_alpha_takes_precedence_over_sidecar(self):
        import io
        from luna_kit.pvr import PVR
        primary, _ = self.pvr_fixture(embedded_alpha=64)
        expected = PVR(str(primary)).image.convert('RGBA').tobytes()
        model = self.load()
        with Image.open(io.BytesIO(model['materials'][0]['png_bytes'])) as image:
            self.assertEqual(image.tobytes(), expected)
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 64))

    def test_pvr_different_size_required_sidecar_is_rejected(self):
        _, mask = self.pvr_fixture()
        header = struct.pack('<4sI4s4s9I', b'PVR\x03', 0, b'rgba', bytes([8] * 4),
                             0, 0, 1, 1, 1, 1, 1, 1, 0)
        mask.write_bytes(header + bytes([128, 128, 128, 255]))
        with self.assertRaisesRegex(NativeInputError, 'alpha dimensions differ'):
            self.load()

    def test_pvr_alpha_sidecar_cannot_leave_extracted_directory(self):
        for embedded_alpha in (255, 64):
            with self.subTest(embedded_alpha=embedded_alpha), tempfile.TemporaryDirectory() as outside:
                _, mask = self.pvr_fixture(embedded_alpha)
                target = Path(outside) / 'mask.pvr'
                target.write_bytes(mask.read_bytes())
                mask.unlink()
                mask.symlink_to(target)
                try:
                    with self.assertRaisesRegex(NativeInputError, 'leaves its directory'):
                        self.load()
                finally:
                    mask.unlink()

    def test_pvr_does_not_read_unreferenced_alpha_of_alpha(self):
        import io
        _, mask = self.pvr_fixture()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'not-a-mask.pvr'
            target.write_bytes(b'not a PVR')
            mask.with_name('body.alpha.alpha.pvr').symlink_to(target)
            model = self.load()
        with Image.open(io.BytesIO(model['materials'][0]['png_bytes'])) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 128))



if __name__ == '__main__':
    unittest.main()
