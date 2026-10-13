"""The association audit and selected-clip converter share one strict reader."""
import tempfile
from pathlib import Path
import unittest

from game_data.model_gen.converter.csv import CsvError, read_clips


class NativeCsvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'library.csv'

    def test_selection_preserves_unselected_invalid_row_diagnostics(self):
        self.path.write_text('idle,0,2,15\nbroken,NaN,2,15\n')
        clips, duplicates, diagnostics = read_clips(self.path, 3, {'idle'})
        self.assertEqual([c['name'] for c in clips], ['idle'])
        self.assertEqual(duplicates, 0)
        self.assertEqual([(d['line'], d['name']) for d in diagnostics], [(2, 'broken')])
        with self.assertRaisesRegex(CsvError, 'Invalid animation CSV row 2'):
            read_clips(self.path, 3)

    def test_selection_rejects_bad_selected_row(self):
        self.path.write_text('idle,0,2,0\n')
        with self.assertRaisesRegex(CsvError, 'Invalid selected animation CSV row 1'):
            read_clips(self.path, 3, {'idle'})

    def test_conflicting_unselected_duplicates_only_block_all_row_audit(self):
        self.path.write_text('idle,0,2,15\nother,0,1,15\nother,1,2,15\n')
        self.assertEqual(len(read_clips(self.path, 3, {'idle'})[0]), 1)
        with self.assertRaisesRegex(CsvError, 'Conflicting animation declarations: other'):
            read_clips(self.path, 3)

    def test_identical_duplicates_count_without_reordering(self):
        self.path.write_text('\ufeffidle,0,1,15\nwalk,1,2,15\nidle,0,1,15.0\n')
        for names in (None, {'walk', 'idle'}):
            clips, duplicates, diagnostics = read_clips(self.path, 3, names)
            self.assertEqual([c['name'] for c in clips], ['idle', 'walk'])
            self.assertEqual(duplicates, 1)
            self.assertEqual(diagnostics, [])

    def test_empty_input_retains_existing_mode_difference(self):
        self.path.write_text('\n')
        self.assertEqual(read_clips(self.path, 3, {'idle'}), ([], 0, []))
        with self.assertRaisesRegex(CsvError, 'Empty CSV'):
            read_clips(self.path, 3)

    def test_partial_discovery_preserves_valid_rows_and_diagnostics(self):
        self.path.write_text('idle,0,2,15\nbroken,0,99,15\nwalk,0,1,15\n')
        clips, duplicates, diagnostics = read_clips(self.path, 3, allow_partial=True)
        self.assertEqual([c['name'] for c in clips], ['idle', 'walk'])
        self.assertEqual(duplicates, 0)
        self.assertEqual([(d['line'], d['name']) for d in diagnostics], [(2, 'broken')])

    def test_partial_discovery_removes_every_ambiguous_name_in_any_order(self):
        for bad in ['other,NaN,1,15\n', 'other,1,2,15\n']:
            for rows in [bad + 'other,0,1,15\n', 'other,0,1,15\n' + bad,
                         'other,0,1,15\nother,0,1,15\n' + bad + 'other,0,1,15\n']:
                with self.subTest(rows=rows):
                    self.path.write_text('idle,0,2,15\n' + rows)
                    clips, duplicates, diagnostics = read_clips(self.path, 3, allow_partial=True)
                    self.assertEqual([c['name'] for c in clips], ['idle'])
                    self.assertEqual(duplicates, 0)
                    self.assertTrue(diagnostics)
                    self.assertEqual({d['name'] for d in diagnostics}, {'other'})
                    with self.assertRaises(CsvError):
                        read_clips(self.path, 3, {'other'})

    def test_partial_discovery_no_valid_clip_still_fails(self):
        for rows in ['idle,0,4,15\n', 'idle,0,1,15\nidle,1,2,15\n', '']:
            with self.subTest(rows=rows):
                self.path.write_text(rows)
                with self.assertRaises(CsvError):
                    read_clips(self.path, 3, allow_partial=True)

    def test_allow_partial_does_not_weaken_selected_mode(self):
        self.path.write_text('idle,0,2,15\nidle,0,1,15\n')
        with self.assertRaisesRegex(CsvError, 'Conflicting'):
            read_clips(self.path, 3, {'idle'}, allow_partial=True)

    def test_source_metadata_keeps_malformed_first_record_before_filtering(self):
        self.path.write_text('broken,0,99,15\nidle,0,2,15\n')
        metadata = {}
        clips, _, _ = read_clips(self.path, 3, allow_partial=True, source_metadata=metadata)
        self.assertEqual([clip['name'] for clip in clips], ['idle'])
        self.assertEqual(metadata, {'first_declared_name':'broken', 'first_declared_line':1,
                                    'raw_record_count':2, 'empty_record_count':0})

    def test_source_metadata_keeps_first_name_after_later_conflict_excludes_it(self):
        self.path.write_text('first,0,1,15\nidle,0,2,15\nfirst,1,2,15\n')
        metadata = {}
        clips, _, _ = read_clips(self.path, 3, allow_partial=True, source_metadata=metadata)
        self.assertEqual([clip['name'] for clip in clips], ['idle'])
        self.assertEqual(metadata, {'first_declared_name':'first', 'first_declared_line':1,
                                    'raw_record_count':3, 'empty_record_count':0})

    def test_source_metadata_never_skips_blank_or_empty_first_record(self):
        for first, name in [('\n', None), (',0,1,15\n', ''), (' \n', ' ')]:
            with self.subTest(first=first):
                self.path.write_text(first + 'idle,0,2,15\n')
                metadata = {}
                clips, _, _ = read_clips(self.path, 3, allow_partial=True, source_metadata=metadata)
                self.assertEqual([clip['name'] for clip in clips], ['idle'])
                self.assertEqual(metadata, {'first_declared_name':name, 'first_declared_line':1,
                                            'raw_record_count':2, 'empty_record_count':int(first == '\n')})

    def test_record_count_preserves_middle_blank_and_duplicate_indices(self):
        self.path.write_text('idle,0,2,15\n\nidle,0,2,15\n')
        metadata = {}
        clips, duplicates, _ = read_clips(self.path, 3, source_metadata=metadata)
        self.assertEqual(len(clips), 1)
        self.assertEqual(duplicates, 1)
        self.assertEqual(metadata['raw_record_count'], 3)
        self.assertEqual(metadata['empty_record_count'], 1)


if __name__ == '__main__':
    unittest.main()
