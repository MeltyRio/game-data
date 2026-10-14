"""Explicit native sampler policy; source files and CSV timing are never repaired."""
from pathlib import Path
import struct
import tempfile
import unittest

from game_data.model_gen.converter.native import load_native
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
