"""The public native path, independent GLBs and uploader-safe catalogue."""
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np

from game_data.model_gen.converter import from_native
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


    def test_animation_with_a_different_bone_layout_is_rejected(self):
        path = self.folder / 'flag.anim'
        raw = bytearray(path.read_bytes())
        struct.pack_into('<I', raw, 80, 1)
        # Keep the source syntactically valid but with one joint instead of two.
        path.write_bytes(raw[:92] + b''.join(raw[92 + frame * 64:92 + frame * 64 + 32] for frame in range(46)))
        with self.assertRaisesRegex(NativeInputError, 'bone counts differ'):
            self.load()


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
                                           else f"models/3d/anim/{after['sha256']}/{name}.glb"))
            self.assertEqual(named.files[after['path']], legacy.files[before['path']])
            for key in ('sha256', 'bytes', 'rigKey'):
                self.assertEqual(after[key], before[key])
        self.assertEqual(named.provenance, legacy.provenance)
        self.assertEqual(new['animations'][0]['id'], old['animations'][0]['id'])
        self.assertEqual(new['animations'][0]['duration'], old['animations'][0]['duration'])
        self.assertEqual(self.convert(), legacy, 'legacy remains the standalone API default')


if __name__ == '__main__':
    unittest.main()
