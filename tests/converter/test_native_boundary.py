"""Explicit native sampler policy; source files and CSV timing are never repaired."""
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np
from game_data.model_gen.converter.csv import CsvError, read_clips
from game_data.model_gen.converter.native import NativeInputError, load_native
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

    def test_strict_default_still_rejects_native_tail(self):
        self.csv.write_text('rest,0,0,15\ntail,46,60,15\n')
        with self.assertRaisesRegex(CsvError, 'out-of-range'):
            read_clips(self.csv, 47)
        with self.assertRaisesRegex(NativeInputError, 'out-of-range'):
            self.load(['tail'])

    def test_explicit_policy_preserves_native_order_range_and_duration(self):
        self.csv.write_text('rest,0,0,15\nz_last,260,340,15\na_first,0,2,15\n')
        clips, duplicates, diagnostics = read_clips(self.csv, 261, frame_boundary='native-clamp')
        self.assertEqual([c['name'] for c in clips], ['rest','z_last','a_first'])
        self.assertEqual(clips[1], {'name':'z_last','start':260,'end':340,'fps':15.0})
        self.assertEqual((duplicates,diagnostics), (0,[]))

    def test_partial_and_entire_tail_sample_last_real_frame_for_all_supported_layouts(self):
        for kind in (0, 1, 4):
            with self.subTest(kind=kind):
                self.write_animation(kind)
                self.csv.write_text('rest,0,0,15\npartial,0,3,15\ntail,100,102,15\n')
                native = self.load(['partial','tail'], frame_boundary='native-clamp')
                first, tail = native['clips']
                self.assertEqual(len(first['translations']), 4)
                self.assertEqual(len(tail['translations']), 3)
                np.testing.assert_array_equal(first['translations'][1:], np.repeat(first['translations'][1:2],3,axis=0))
                np.testing.assert_array_equal(tail['translations'], np.repeat(first['translations'][1:2],3,axis=0))
                np.testing.assert_array_equal(tail['rotations_xyzw'], np.repeat(first['rotations_xyzw'][1:2],3,axis=0))
                proof = native['provenance']['native_frame_boundary']
                self.assertEqual(proof['source_frame_count'], 2)
                self.assertEqual([(x['name'],x['declared_start'],x['declared_end'],x['clamped_samples'])
                                  for x in proof['clips']], [('partial',0,3,2),('tail',100,102,3)])

    def test_rest_uses_same_boundary_but_records_original_declaration(self):
        self.write_animation(0)
        self.csv.write_text('rest,9,10,15\nend,1,1,15\n')
        result = self.load(['end'], frame_boundary='native-clamp')
        self.assertEqual(result['rest']['frame'], 1)
        np.testing.assert_array_equal(result['rest']['translations'],result['clips'][0]['translations'])
        self.assertEqual(result['provenance']['native_frame_boundary']['rest'],
                         {'name':'rest','declared_frame':9,'sampled_frame':1})

    def test_policy_rejects_invalid_rows_duplicates_unknown_policy_and_empty_anim(self):
        for row in ['x,-1,3,15','x,4,3,15','x,1.5,3,15','x,0,3,0','x,0,3,NaN',
                    'x,0,3,15\nx,0,4,15','x,2147483647,2147483647,15']:
            self.csv.write_text(row+'\n')
            with self.subTest(row=row), self.assertRaises(CsvError):
                read_clips(self.csv,2,frame_boundary='native-clamp')
        self.csv.write_text('x,0,0,15\n')
        for kwargs in [{'frame_boundary':'anything'}, {'frame_boundary':'native-clamp','frame_count':0}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(CsvError):
                read_clips(self.csv, **{'frame_count':2,**kwargs})

    def test_clip_budget_is_checked_before_expansion(self):
        self.csv.write_text('rest,0,0,15\ntail,0,10000,15\n')
        with self.assertRaisesRegex(NativeInputError, '10000'):
            self.load(['tail'],frame_boundary='native-clamp')
        self.csv.write_text('tail,0,9999,15\n')
        self.assertEqual(read_clips(self.csv,47,frame_boundary='native-clamp')[0][0]['end'],9999)

    def test_conversion_joint_frame_budget_counts_all_selected_clips_and_rest(self):
        names = [f'clip_{i}' for i in range(50)]
        self.csv.write_text('rest,0,0,15\n'+''.join(f'{name},0,9999,15\n' for name in names))
        with self.assertRaisesRegex(NativeInputError, '1000000'):
            self.load(names,frame_boundary='native-clamp')

    def test_in_range_explicit_policy_does_not_change_glb_bytes_or_catalogue(self):
        args = (self.folder/'flag.rk',self.folder/'flag.anim',self.csv,['wave'])
        kwargs = dict(identity='Synthetic_Flag',visible_meshes=['flag'],rest_name='rest',events_absent=True)
        before = convert_native(*args,**kwargs)
        after = convert_native(*args,**kwargs,frame_boundary='native-clamp')
        self.assertEqual(before.files,after.files)
        self.assertEqual(before.catalogue,after.catalogue)

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
