"""Synthetic public appearance / town-action boundary checks."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bootstrap import ROOT
from game_data.model_gen.selection import SelectionError, select_native_view, index_event_files, _event_file


class Objects(dict):
    CATEGORY_DATA = {'Pony': {'Eyes': {'attributes': {
        'EyeSubObject_' + state: {'default': mesh}
        for state, mesh in [('Open', 'eyes_open'), ('Closed', 'eyes_shut'),
                            ('Happy', 'eyes_happy'), ('Frown', 'eyes_frown')]
    }}}}


class PonyBookProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.obj = {'Model': {'MediumLOD': 'body'}, 'AI': {'Special_AI': 0},
                    'Animation': {'Rig_MediumLOD': 'book', 'AnimationEventsXML': 'world',
                                  'LeftIdleAnimationList': ['town_idle'],
                                  'RightIdleAnimationList': ['town_walk', 'world_event', 'book_event']}}
        self.meshes = ['body', 'eyes_open', 'eyes_shut', 'eyes_happy', 'eyes_frown']
        self.clips = [{'name': name, 'start': i * 3, 'end': i * 3 + 2, 'fps': 15}
                      for i, name in enumerate(['bind', 'town_idle', 'town_walk', 'world_event',
                                               'book_event', 'gen_idle_00', 'other_idle', 'foreign'])]
        self.xml('world', ['world_event'])
        self.xml('book', ['book_event'])

    def xml(self, stem, events=(), defaults=''):
        (self.folder/(stem + '.xml')).write_text('<MeshList>' + defaults + '</MeshList><AnimationList>'
            + ''.join('<Animation Name="' + name + '"><Frame Index="1"><EyeSet/></Frame></Animation>'
                      for name in events) + '</AnimationList>')

    def select(self, profile='ponybook-default', **overrides):
        args = dict(category='Pony', obj=self.obj, game_objects=Objects(), model_stem='body',
                    mesh_names=self.meshes, clips=self.clips, extracted_dir=self.folder,
                    csv_first_name='bind', csv_excluded_names=[], display_profile=profile)
        args.update(overrides)
        return select_native_view(**args)

    def fails(self, code, **kwargs):
        with self.assertRaises(SelectionError) as error:
            self.select(**kwargs)
        self.assertEqual(error.exception.code, code)

    def test_display_rest_is_resolved_idle_and_constructor_stays_available(self):
        result = self.select()
        self.assertEqual(result['rest_name'], 'gen_idle_00')
        self.assertEqual(result['provenance']['rest']['requested_name'], 'gen_idle_00')
        self.assertEqual(result['provenance']['rest']['name'], 'gen_idle_00')
        constructor = result['provenance']['ponybook']['constructor']
        self.assertEqual(constructor['requested_name'], 'default')
        self.assertEqual(constructor['name'], 'bind')
        self.assertEqual(constructor['rule'], 'native-name-miss-to-original-CSV-row-zero')
        self.assertIn('bind', result['clip_names'])

    def test_world_idle_event_blocks_book_and_preserves_verified_world(self):
        self.xml('world', ['world_event', 'gen_idle_00'])
        self.fails('ponybook_idle_requires_events')
        world = self.select('world-preview')
        result = self.select('ponybook-preferred')
        for field in ['rest_name', 'visible_meshes', 'clip_names', 'event_path', 'events_absent']:
            self.assertEqual(result[field], world[field])
        self.assertEqual(result['rest_name'], 'town_idle')
        self.assertEqual(result['provenance']['display']['ponybook_unavailable']['code'],
                         'ponybook_idle_requires_events')
        self.assertNotIn('ponybook', result['provenance'])

        (self.folder/'book.xml').unlink()
        self.fails('ponybook_idle_requires_events')

    def test_missing_book_xml_keeps_native_eyes_and_idle_without_world_mesh_defaults(self):
        self.meshes.append('world_hidden')
        self.xml('world', ['world_event'],
                 '<body><SubObject ID="1" Name="world_hidden" DefaultVisible="0"/></body>')
        (self.folder/'book.xml').unlink()

        result = self.select()

        self.assertEqual(set(result['visible_meshes']), {'body', 'eyes_open', 'world_hidden'})
        self.assertEqual(result['rest_name'], 'gen_idle_00')
        self.assertNotIn('world_event', result['clip_names'])
        self.assertEqual(result['event_path'], self.folder/'world.xml')
        self.assertFalse(result['events_absent'])
        self.assertEqual(result['additional_event_paths'], [])
        book = result['provenance']['ponybook']
        self.assertEqual(book['event_file'], 'book.xml')
        self.assertEqual(book['event_source'], 'missing-input-native-empty-controller')
        self.assertFalse(book['world_visibility_equivalent'])

    def test_present_invalid_book_xml_is_not_an_empty_controller(self):
        (self.folder/'book.xml').write_text('<broken')
        self.fails('invalid_event_source')
        result = self.select('ponybook-preferred')
        self.assertEqual(result['provenance']['display']['selected'], 'world-preview')
        self.assertEqual(result['provenance']['display']['ponybook_unavailable']['code'],
                         'invalid_event_source')

    def test_missing_world_xml_keeps_book_event_checks(self):
        (self.folder/'world.xml').unlink()
        result = self.select()
        self.assertIsNone(result['event_path'])
        self.assertTrue(result['events_absent'])
        self.assertEqual(result['additional_event_paths'], [self.folder/'book.xml'])
        self.assertIn('world_event', result['clip_names'])
        self.assertNotIn('book_event', result['clip_names'])
        self.assertEqual(result['rest_name'], 'gen_idle_00')

    def test_present_invalid_world_xml_is_not_an_empty_controller(self):
        (self.folder/'world.xml').write_text('<broken')
        self.fails('invalid_event_source', profile='ponybook-preferred')

    def test_unmatched_native_eye_names_preserve_all_meshes(self):
        self.obj['Eyes'] = {'EyeSubObject_' + state: ['missing_' + state]
                           for state in ('Open', 'Closed', 'Happy', 'Frown')}
        self.obj['Eyes']['EyeSubObject_Closed'].append('eyes_shut')
        result = self.select('world-preview')
        self.assertEqual(result['visible_meshes'], self.meshes)
        from game_data.model_gen.interactions import prepare_interactions
        extended = prepare_interactions(result, 'Pony', self.obj,
            model_stem='body', mesh_names=self.meshes, clips=self.clips)
        self.assertNotIn('eyes', extended.get('model_interactions', {}))
        self.assertEqual(extended['visible_meshes'], self.meshes)

    def test_missing_open_with_partially_matching_eye_controls_remains_rejected(self):
        self.obj['Eyes'] = {'EyeSubObject_Open': ['missing'], 'EyeSubObject_Closed': ['eyes_shut']}
        self.fails('missing_open_eye_mesh', profile='world-preview')


    def test_present_conversation_selects_exact_name_not_prefix(self):
        self.obj['Conversation'] = {'IdleAnimation': 'other_idle'}
        self.assertEqual(self.select()['provenance']['ponybook']['idle']['name'], 'other_idle')
        self.assertEqual(self.select()['rest_name'], 'other_idle')
        self.obj['Conversation']['IdleAnimation'] = 'other'
        self.assertEqual(self.select()['provenance']['ponybook']['idle']['name'], 'bind')
        self.assertEqual(self.select()['rest_name'], 'bind')
        self.assertNotIn('other_idle', self.select()['clip_names'])


    def test_filename_index_reuses_exact_world_and_book_lookups(self):
        expected = self.select()
        original = Path.iterdir
        with patch.object(Path, 'iterdir', autospec=True, side_effect=original) as scan:
            names = index_event_files(self.folder)
            for _ in range(3):
                self.assertEqual(self.select(event_files=names), expected)
            self.assertEqual(scan.call_count, 1)

    def test_filename_index_missing_book_and_new_build(self):
        (self.folder/'book.xml').unlink()
        names = index_event_files(self.folder)
        result = self.select(event_files=names)
        self.assertEqual(result['provenance']['ponybook']['event_source'],
                         'missing-input-native-empty-controller')
        self.xml('book', ['book_event'])
        current = self.select(event_files=index_event_files(self.folder))
        self.assertEqual(current['provenance']['ponybook']['event_source'], 'xml')
        (self.folder/'world.xml').unlink()
        current = self.select(event_files=index_event_files(self.folder))
        self.assertIsNone(current['event_path'])
        self.assertTrue(current['events_absent'])

    def test_filename_index_rejects_nonexact_names(self):
        # Use the actual filesystem's result: case-insensitive lookup must
        # still reject a spelling mismatch, while case-sensitive lookup misses.
        (self.folder/'book.xml').rename(self.folder/'Book.xml')
        expected = ('ambiguous_event_source' if (self.folder/'book.xml').is_file()
                    else 'missing_event_source')
        self.fails(expected, event_files=index_event_files(self.folder))
        names = {'world.xml': ['world.xml', 'World.xml']}
        with self.assertRaises(SelectionError) as error:
            _event_file(self.folder, 'world', event_files=names)
        self.assertEqual(error.exception.code, 'ambiguous_event_source')

    def test_filename_index_rejects_real_case_collision(self):
        try:
            with (self.folder/'World.xml').open('x') as target:
                target.write('<AnimationList/>')
        except FileExistsError:
            self.skipTest('Filesystem cannot represent case-distinct filenames')
        self.fails('ambiguous_event_source', event_files=index_event_files(self.folder))
