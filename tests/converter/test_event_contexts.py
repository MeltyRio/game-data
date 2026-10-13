"""Extra native event contexts must constrain the same model/rest/action bundle."""
import collections
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from game_data.model_gen.converter.export import convert_native
from game_data.model_gen.converter.native import NativeInputError, load_native
from native_fixture import write_fixture


class EventContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'native'
        write_fixture(self.folder)
        self.primary = self.folder / 'flag.xml'
        self.book = self.folder / 'book.xml'
        self.book.write_text('<MeshList/><AnimationList/>', encoding='utf-8')

    def call(self, entry, additional=(), *, events_absent=False):
        kwargs = dict(visible_meshes=['flag'], rest_name='rest',
                      additional_event_paths=additional)
        if events_absent:
            kwargs['events_absent'] = True
        else:
            kwargs['event_path'] = self.primary
        if entry is convert_native:
            kwargs['identity'] = 'Synthetic_Flag'
        return entry(self.folder / 'flag.rk', self.folder / 'flag.anim',
                     self.folder / 'flag.csv', ['wave'], **kwargs)

    def test_additional_selected_action_events_are_rejected(self):
        self.book.write_text('<MeshList/><AnimationList><Animation Name="wave">'
                             '<Frame Index="1"><EyeSet Open="Happy"/></Frame>'
                             '</Animation></AnimationList>', encoding='utf-8')
        for entry in (load_native, convert_native):
            with self.subTest(entry=entry.__name__):
                with self.assertRaisesRegex(NativeInputError, 'unsupported native events: wave'):
                    self.call(entry, [self.book])

    def test_additional_rest_events_are_rejected_even_when_rest_is_not_exported(self):
        self.book.write_text('<AnimationList><Animation Name="rest">'
                             '<Frame Index="0"><EyeSet Open="Closed"/></Frame>'
                             '</Animation></AnimationList>', encoding='utf-8')
        for entry in (load_native, convert_native):
            with self.subTest(entry=entry.__name__):
                with self.assertRaisesRegex(NativeInputError, 'unsupported native events: rest'):
                    self.call(entry, [self.book])

    def test_missing_additional_xml_is_rejected(self):
        self.book.unlink()
        for entry in (load_native, convert_native):
            with self.subTest(entry=entry.__name__):
                with self.assertRaisesRegex(NativeInputError, 'Missing native event XML: book.xml'):
                    self.call(entry, [self.book])

    def test_additional_dtd_or_entity_declarations_are_rejected(self):
        for declaration in ('<!DOCTYPE AnimationList>', '<!ENTITY example "value">'):
            self.book.write_text(declaration + '<AnimationList/>', encoding='utf-8')
            for entry in (load_native, convert_native):
                with self.subTest(entry=entry.__name__, declaration=declaration):
                    with self.assertRaisesRegex(NativeInputError, 'DTD/entity'):
                        self.call(entry, [self.book])

    def test_additional_selected_and_rest_attributes_are_rejected(self):
        for name in ('wave', 'rest'):
            self.book.write_text('<AnimationList><Animation Name="' + name
                                 + '" FutureController="1"/></AnimationList>', encoding='utf-8')
            for entry in (load_native, convert_native):
                with self.subTest(entry=entry.__name__, clip=name):
                    with self.assertRaisesRegex(NativeInputError, 'Unsupported selected native animation attributes'):
                        self.call(entry, [self.book])

    def test_additional_malformed_xml_and_unknown_sections_are_rejected(self):
        for content in ('<AnimationList>', '<FutureController/>'):
            self.book.write_text(content, encoding='utf-8')
            for entry in (load_native, convert_native):
                with self.subTest(entry=entry.__name__, content=content):
                    with self.assertRaisesRegex(NativeInputError, 'Invalid native event XML|Unsupported native event document structure'):
                        self.call(entry, [self.book])

    def test_each_distinct_additional_source_is_in_both_provenance_records(self):
        other = self.folder / 'other.xml'
        other.write_text('<AnimationList><Animation Name="unselected">'
                         '<Frame Index="7"><FutureEffect/></Frame>'
                         '</Animation></AnimationList>', encoding='utf-8')
        self.book.write_text('<MeshList/><AnimationList><Animation Name="wave"/>'
                             '<Animation Name="rest"/></AnimationList>', encoding='utf-8')
        expected = [{'status': 'parsed-source-xml', 'name': path.name,
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                     'checked_clips': ['rest', 'wave']} for path in (self.book, other)]
        bundle = self.call(convert_native, [self.book, other])
        provenance = bundle.provenance
        self.assertEqual(provenance['additional_event_sources'], expected)
        for source in expected:
            self.assertEqual(provenance['files'].count({k: source[k] for k in ('name', 'sha256')}), 1)
        self.assertEqual(provenance['event_source']['name'], 'flag.xml')
        # A source can contain unsupported events for a clip that is not exported.
        self.assertEqual([a['id'] for a in bundle.catalogue['models']['Synthetic_Flag']['animations']], ['wave'])

    def test_duplicate_resolved_sources_do_not_add_reads_or_provenance(self):
        original_read = Path.read_bytes
        watched = {self.primary.resolve(), self.book.resolve()}

        def measured(additional):
            reads = collections.Counter()

            def read(path):
                resolved = path.resolve()
                if resolved in watched:
                    reads[resolved] += 1
                return original_read(path)

            with mock.patch.object(Path, 'read_bytes', read):
                bundle = self.call(convert_native, additional)
            return reads, bundle

        once_reads, once = measured([self.book])
        duplicate_reads, duplicate = measured([
            self.primary, self.book, str(self.book),
            self.folder / '..' / 'native' / 'book.xml', self.primary,
        ])
        self.assertEqual(set(once_reads), watched)
        self.assertTrue(all(count > 0 for count in once_reads.values()))
        # Hash collection may read an audited source again; repeated arguments
        # must not add any reads beyond the single-source conversion baseline.
        self.assertEqual(duplicate_reads, once_reads)
        self.assertEqual(duplicate, once)
        self.assertEqual(len(duplicate.provenance['additional_event_sources']), 1)
        names = [item['name'] for item in duplicate.provenance['files']]
        self.assertEqual(names.count('flag.xml'), 1)
        self.assertEqual(names.count('book.xml'), 1)

    def test_valid_additional_context_changes_only_private_provenance(self):
        self.book.write_text('<MeshList/><AnimationList><Animation Name="wave"/>'
                             '<Animation Name="unselected"><Frame Index="1">'
                             '<EyeSet Open="Happy"/></Frame></Animation>'
                             '</AnimationList>', encoding='utf-8')
        before = self.call(convert_native)
        after = self.call(convert_native, [self.book])
        self.assertEqual(after.files, before.files)
        self.assertEqual(after.catalogue, before.catalogue)
        self.assertEqual(after.catalogue['models']['Synthetic_Flag']['rigKey'],
                         before.catalogue['models']['Synthetic_Flag']['rigKey'])
        self.assertNotIn('additional_event_sources', before.provenance)
        self.assertEqual(after.provenance['additional_event_sources'][0]['name'], 'book.xml')

    def test_confirmed_world_absence_still_checks_the_book_source(self):
        for content, error in (
            ('<AnimationList><Animation Name="wave"><Frame Index="1"/></Animation></AnimationList>',
             'unsupported native events: wave'),
            ('<AnimationList><Animation Name="rest"><Frame Index="0"/></Animation></AnimationList>',
             'unsupported native events: rest'),
        ):
            self.book.write_text(content, encoding='utf-8')
            for entry in (load_native, convert_native):
                with self.subTest(entry=entry.__name__, content=content):
                    with self.assertRaisesRegex(NativeInputError, error):
                        self.call(entry, [self.book], events_absent=True)
        self.book.unlink()
        with self.assertRaisesRegex(NativeInputError, 'Missing native event XML'):
            self.call(convert_native, [self.book], events_absent=True)
        self.book.write_text('<MeshList/><AnimationList/>', encoding='utf-8')
        bundle = self.call(convert_native, [self.book], events_absent=True)
        self.assertEqual(bundle.provenance['event_source']['status'], 'source-absence-confirmed-by-caller')
        self.assertEqual(bundle.provenance['additional_event_sources'][0]['name'], 'book.xml')
        self.assertIn('book.xml', [item['name'] for item in bundle.provenance['files']])
        self.assertNotIn('flag.xml', [item['name'] for item in bundle.provenance['files']])


if __name__ == '__main__':
    unittest.main()
