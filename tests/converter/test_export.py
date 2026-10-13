"""The public native path, independent GLBs and uploader-safe catalogue."""
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np
from PIL import Image

from game_data.model_gen.converter import from_native, model_glb
from game_data.model_gen.converter.export import convert_native
from game_data.model_gen.converter.native import NativeInputError, load_native
from .test_converter import unpack

from native_fixture import write_fixture


def accessor(document, binary, index):
    item = document['accessors'][index]
    view = document['bufferViews'][item['bufferView']]
    dtype = {5126: '<f4', 5123: '<u2', 5125: '<u4'}[item['componentType']]
    width = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}[item['type']]
    value = np.frombuffer(binary, dtype=dtype, count=item['count'] * width, offset=view['byteOffset'])
    value = value.reshape(item['count'], width)
    if item['type'] == 'MAT4':
        value = value.reshape(-1, 4, 4).transpose(0, 2, 1)
    return value


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / 'native'
        write_fixture(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def load(self):
        return load_native(self.folder / 'flag.rk', self.folder / 'flag.anim', self.folder / 'flag.csv', ['wave'],
                           visible_meshes=['flag'], rest_name='rest', event_path=self.folder / 'flag.xml')

    def convert(self, **options):
        return convert_native(self.folder / 'flag.rk', self.folder / 'flag.anim', self.folder / 'flag.csv', ['wave'],
                              identity='Synthetic_Flag', visible_meshes=['flag'], rest_name='rest',
                              event_path=self.folder / 'flag.xml', **options)

    def test_native_two_joint_geometry_bind_uv_and_standalone_animation(self):
        native = self.load()
        model, clips = from_native(native, identity='Synthetic_Flag')
        bundle = self.convert()
        row = bundle.catalogue['models']['Synthetic_Flag']
        document, binary = unpack(bundle.files[row['path']])
        animation, _ = unpack(bundle.files[row['animations'][0]['path']])
        self.assertNotIn('animations', document)
        self.assertNotIn('meshes', animation)
        self.assertNotIn('images', animation)
        self.assertEqual(document['extras']['atpRig'], animation['extras']['atpRig'])
        self.assertEqual(row['rigKey'], document['extras']['atpRig']['key'])
        self.assertEqual(row['animations'][0]['duration'], 46 / 15)
        np.testing.assert_array_equal(native['bones'][1]['bind_global'], model.bind_global[1])
        np.testing.assert_array_equal(model.bind_global[1][:3, 3], [0, -1, 0])
        inv = accessor(document, binary, document['skins'][0]['inverseBindMatrices'])
        np.testing.assert_allclose(model.bind_global @ inv, np.broadcast_to(np.eye(4), (2, 4, 4)))
        attrs = document['meshes'][0]['primitives'][0]['attributes']
        np.testing.assert_array_equal(accessor(document, binary, attrs['TEXCOORD_0']), model.texcoords)
        triangles = accessor(document, binary, document['meshes'][0]['primitives'][0]['indices']).reshape(-1, 3)
        np.testing.assert_array_equal(triangles, native['primitives'][0]['indices'][:, [0, 2, 1]])
        # Matrix skinning for the upper flag tip must actually move while root remains fixed.
        point = np.r_[model.positions[-1], 1]
        posed = clips[0].global_frames[11, 1] @ inv[1] @ point
        self.assertGreater(abs(posed[0] - point[0]), .1)
        np.testing.assert_allclose(clips[0].global_frames[:, 0], np.broadcast_to(np.eye(4), (46, 4, 4)))

    def test_digest_assets_and_json_reserialization_are_independent(self):
        bundle = self.convert()
        for path, data in bundle.files.items():
            self.assertEqual(path, f'models/3d/{hashlib.sha256(data).hexdigest()}.glb')
        # Mirrors ATP data sync's semantic JSON load/dump; no JSON byte hash is referenced.
        rewritten = json.dumps(json.loads(json.dumps(bundle.catalogue, indent=2)), ensure_ascii=False, separators=(',', ':'))
        self.assertEqual(json.loads(rewritten), bundle.catalogue)
        self.assertNotIn(str(self.folder), rewritten)
        self.assertNotIn('provenance', rewritten)
        self.assertNotIn('http', rewritten)
        self.assertEqual(self.convert(), bundle)

    def test_conversion_leaves_native_inputs_and_filesystem_unchanged(self):
        before = {str(path.relative_to(self.temp.name)): path.read_bytes()
                  for path in Path(self.temp.name).rglob('*') if path.is_file()}
        bundle = self.convert()
        self.assertEqual(len(bundle.files), 2)
        after = {str(path.relative_to(self.temp.name)): path.read_bytes()
                 for path in Path(self.temp.name).rglob('*') if path.is_file()}
        self.assertEqual(after, before)

    def test_invalid_identity_is_rejected_before_native_files_are_read(self):
        for identity in ('', None, 5, 'bad\nname'):
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, 'identity'):
                convert_native(self.folder / 'missing.rk', self.folder / 'missing.anim',
                               self.folder / 'missing.csv', ['wave'], identity=identity,
                               visible_meshes=['flag'], rest_name='rest', events_absent=True)

    def test_face_section_overrun_rejected_before_luna(self):
        path = self.folder / 'flag.rk'
        raw = bytearray(path.read_bytes())
        submesh_offset = struct.unpack_from('<I', raw, 84)[0]
        struct.pack_into('<I', raw, submesh_offset, 5)
        path.write_bytes(raw)
        with self.assertRaisesRegex(NativeInputError, 'face range'):
            self.load()

    def test_corrupt_pvr_has_an_explicit_native_diagnostic(self):
        rkm = self.folder / 'flag.rkm'
        rkm.write_text(rkm.read_text().replace('NoCompress=true', 'NoCompress=false'))
        for payload in (b'NOPE' + bytes(48), b'PVR\x03'):
            (self.folder / 'flag.pvr').write_bytes(payload)
            with self.subTest(payload=payload[:4]), self.assertRaisesRegex(NativeInputError, 'Native PVR parse failed'):
                self.load()

    def test_animation_with_a_different_bone_layout_is_rejected(self):
        path = self.folder / 'flag.anim'
        raw = bytearray(path.read_bytes())
        struct.pack_into('<I', raw, 80, 1)
        # Keep the source syntactically valid but with one joint instead of two.
        path.write_bytes(raw[:92] + b''.join(raw[92 + frame * 64:92 + frame * 64 + 32] for frame in range(46)))
        with self.assertRaisesRegex(NativeInputError, 'bone counts differ'):
            self.load()

    def test_material_and_event_extensions_are_not_silently_ignored(self):
        rkm = self.folder / 'flag.rkm'
        rkm.write_text(rkm.read_text().replace('rkshader', 'rkponyhair0'))
        with self.assertRaisesRegex(NativeInputError, 'Missing or unsupported native UserVector declaration'):
            self.load()
        write_fixture(self.folder)
        for xml in ('<AnimationList><Animation Name="wave"><Frame Index="1"><FutureEffect/></Frame></Animation></AnimationList>',
                    '<AnimationList><FutureAnimation Name="wave"/></AnimationList>',
                    '<AnimationList><Animation Name="wave" FutureController="1"/></AnimationList>'):
            (self.folder / 'flag.xml').write_text(xml)
            with self.subTest(xml=xml), self.assertRaisesRegex(NativeInputError, 'unsupported|Unsupported'):
                self.load()

    def test_animation_source_digest_does_not_depend_on_filename_case(self):
        original = self.convert()
        path = self.folder / 'FLAG.ANIM'
        (self.folder / 'flag.anim').rename(path)
        renamed = convert_native(self.folder / 'flag.rk', path, self.folder / 'flag.csv', ['wave'],
                                 identity='Synthetic_Flag', visible_meshes=['flag'], rest_name='rest',
                                 event_path=self.folder / 'flag.xml')
        self.assertEqual(original.catalogue, renamed.catalogue)
        self.assertEqual(original.files, renamed.files)
        before = {row['name']: row['sha256'] for row in original.provenance['files']}
        after = {row['name']: row['sha256'] for row in renamed.provenance['files']}
        self.assertIn('flag.anim', before)
        self.assertNotIn('FLAG.ANIM', before)
        self.assertIn('FLAG.ANIM', after)
        self.assertNotIn('flag.anim', after)
        self.assertEqual(before.pop('flag.anim'), after.pop('FLAG.ANIM'))
        self.assertEqual(before, after)

    def test_missing_explicit_source_digest_has_a_conversion_diagnostic(self):
        from game_data.model_gen.converter import ConversionError
        native = self.load()
        del native['provenance']['animation_sha256']
        with self.assertRaisesRegex(ConversionError, 'native animation digest'):
            from_native(native, identity='Synthetic_Flag')

    def test_source_hash_not_bone_count_is_the_binding_key(self):
        first = self.convert().catalogue['models']['Synthetic_Flag']['rigKey']
        path = self.folder / 'flag.anim'
        raw = bytearray(path.read_bytes())
        struct.pack_into('<f', raw, 92 + 64 + 16, .2)
        path.write_bytes(raw)
        second = self.convert().catalogue['models']['Synthetic_Flag']['rigKey']
        self.assertNotEqual(first, second)


    def test_named_assets_preserve_every_glb_byte_and_reference_field(self):
        legacy = self.convert()
        named = self.convert(model_filename='Synthetic_Flag', animation_filenames=True)
        old = legacy.catalogue['models']['Synthetic_Flag']
        new = named.catalogue['models']['Synthetic_Flag']
        for before, after, name in [(old, new, 'Synthetic_Flag'),
                                   (old['animations'][0], new['animations'][0], 'wave')]:
            self.assertEqual(after['path'], (f'models/3d/{name}.glb' if name == 'Synthetic_Flag'
                                           else f"models/3d/{after['sha256']}/{name}.glb"))
            self.assertEqual(named.files[after['path']], legacy.files[before['path']])
            for key in ('sha256', 'bytes', 'rigKey'):
                self.assertEqual(after[key], before[key])
        self.assertEqual(named.provenance, legacy.provenance)
        self.assertEqual(new['animations'][0]['id'], old['animations'][0]['id'])
        self.assertEqual(new['animations'][0]['duration'], old['animations'][0]['duration'])
        self.assertEqual(self.convert(), legacy, 'legacy remains the standalone API default')

    def test_named_model_rejects_wrong_identity_and_unsafe_names_before_reading(self):
        for name in ['', '../x', 'bad name', 'x%2Fy', 'x#y', 'x?y', 'Other_ID']:
            with self.subTest(name=name), self.assertRaisesRegex(NativeInputError, 'filename'):
                convert_native(self.folder/'absent.rk', self.folder/'absent.anim',
                    self.folder/'absent.csv', ['wave'], identity='Synthetic_Flag',
                    model_filename=name, visible_meshes=['flag'], rest_name='rest', events_absent=True)

    def test_named_animation_rejects_unsafe_native_name_without_rewriting(self):
        (self.folder/'flag.csv').write_text((self.folder/'flag.csv').read_text().replace('wave,', 'wave/bad,'))
        args = [self.folder/'flag.rk', self.folder/'flag.anim', self.folder/'flag.csv', ['wave/bad']]
        options = dict(identity='Synthetic_Flag', visible_meshes=['flag'], rest_name='rest', event_path=self.folder/'flag.xml')
        legacy = convert_native(*args, **options)
        self.assertEqual(legacy.catalogue['models']['Synthetic_Flag']['animations'][0]['id'], 'wave/bad')
        with self.assertRaisesRegex(NativeInputError, 'filename'):
            convert_native(*args, **options, animation_filenames=True)


if __name__ == '__main__':
    unittest.main()
