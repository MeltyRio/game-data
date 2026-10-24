"""Explicit native sampler policy; source files and CSV timing are never repaired."""
from pathlib import Path
import builtins
import io
import struct
import tempfile
import unittest
from unittest.mock import patch

from game_data.model_gen.converter.native import NativeInputError, load_model, load_native
from game_data.model_gen.converter.export import convert_native
from native_fixture import write_fixture


class NativeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        write_fixture(self.folder)
        self.csv = self.folder/'flag.csv'

    def load(self, names, **kwargs):
        return load_native(self.folder/'flag.rk', self.folder/'flag.anim', self.csv, names,
                           visible_meshes=['flag'], rest_name='rest', events_absent=True, **kwargs)

    def write_animation(self, kind):
        records = []
        for frame in range(2):
            for bone in range(2):
                x = 10 * frame + bone
                if kind == 0:
                    row = struct.pack('<8f', 0, 0, 0, 1, x, 0, 0, 1)
                elif kind == 1:
                    row = struct.pack('<4f3h', x, 0, 0, 1, 0, 0, 0)
                else:
                    row = struct.pack('<4h3b', x, 0, 0, 32767, 0, 0, 0)
                records.append(row)
        (self.folder/'flag.anim').write_bytes(struct.pack('<8sII64sIII', b'RKFORMAT', 5, 2,
                                                        b'self_created', 2, 2, kind)+b''.join(records))


    def test_out_of_range_material_index_is_a_native_input_failure(self):
        path = self.folder/'flag.rk'
        raw = bytearray(path.read_bytes())
        submeshes = next(offset for tag, offset, _, _ in
                         struct.iter_unpack('<4I', raw[80:80 + 24 * 16]) if tag == 1)
        # The fixture declares one material, so index one is the first invalid ID.
        struct.pack_into('<I', raw, submeshes + 8, 1)
        path.write_bytes(raw)
        with self.assertRaisesRegex(NativeInputError, 'material index.*range'):
            self.load(['wave'])

    def test_rk_is_opened_once_for_parsing_and_validation(self):
        path = self.folder/'flag.rk'
        with patch('builtins.open', wraps=builtins.open) as opened, patch('io.open', wraps=io.open) as io_opened:
            model = load_model(path)
        reads = [call for call in [*opened.call_args_list, *io_opened.call_args_list]
                 if str(call.args[0]) == str(path)]
        self.assertEqual(len(reads), 1)
        self.assertEqual([mesh.name for mesh in model.meshes], ['flag'])

    def test_rk_uses_lunas_declared_face_offsets(self):
        path = self.folder/'flag.rk'
        raw = bytearray(path.read_bytes())
        offset = next(offset for tag, offset, _, _ in
                      struct.iter_unpack('<4I', raw[80:464]) if tag == 1)
        struct.pack_into('<2I', raw, offset, 1, 3)
        path.write_bytes(raw)
        native = self.load(['wave'])
        self.assertEqual(native['primitives'][0]['indices'].tolist(), [[1, 3, 2]])

    def test_converter_keeps_skin_and_uv_requirements(self):
        path = self.folder/'flag.rk'
        valid = path.read_bytes()
        cases = []
        for tag in (7, 13, 17):
            raw = bytearray(valid)
            header = next(80 + i * 16 for i in range(24)
                          if struct.unpack_from('<I', raw, 80 + i * 16)[0] == tag)
            struct.pack_into('<4I', raw, header, 0, 0, 0, 0)
            cases.append((raw, f'section layout: {tag}'))
        attributes = next(80 + i * 16 for i in range(24)
                          if struct.unpack_from('<I', valid, 80 + i * 16)[0] == 13)
        for entries in ([0], [1026, 1026]):
            raw = bytearray(valid)
            struct.pack_into('<4I', raw, attributes, 13, len(raw), len(entries), len(entries) * 4)
            raw.extend(b''.join(struct.pack('<HBB', code, 12, 0) for code in entries))
            cases.append((raw, 'UV layout'))
        for raw, reason in cases:
            with self.subTest(reason=reason):
                path.write_bytes(raw)
                with self.assertRaisesRegex(NativeInputError, reason):
                    load_model(path)

    def test_tail_export_keeps_original_declared_duration_and_input_bytes(self):
        self.csv.write_text('rest,0,0,15\ntail,46,126,15\n')
        before = {p.name:p.read_bytes() for p in self.folder.iterdir() if p.is_file()}
        bundle = convert_native(self.folder/'flag.rk',self.folder/'flag.anim',self.csv,['tail'],
                                identity='Synthetic_Flag',visible_meshes=['flag'],rest_name='rest',
                                events_absent=True,frame_boundary='native-clamp')
        clip = bundle.catalogue['models']['Synthetic_Flag']['animations'][0]
        self.assertEqual(clip['duration'],81/15)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.folder.iterdir() if p.is_file()})


if __name__ == '__main__':
    unittest.main()
