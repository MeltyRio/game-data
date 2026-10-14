"""Synthetic public appearance / town-action boundary checks."""
from pathlib import Path
import tempfile
import unittest

from bootstrap import ROOT
from game_data.model_gen.selection import SelectionError, select_native_view


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
        self.assertIsNone(book['event_sha256'])
        self.assertFalse(book['world_visibility_equivalent'])

    def test_present_invalid_book_xml_is_not_an_empty_controller(self):
        (self.folder/'book.xml').write_text('<broken')
        self.fails('invalid_event_source')
        result = self.select('ponybook-preferred')
        self.assertEqual(result['provenance']['display']['selected'], 'world-preview')
        self.assertEqual(result['provenance']['display']['ponybook_unavailable']['code'],
                         'invalid_event_source')


    def test_present_conversation_selects_exact_name_not_prefix(self):
        self.obj['Conversation'] = {'IdleAnimation': 'other_idle'}
        self.assertEqual(self.select()['provenance']['ponybook']['idle']['name'], 'other_idle')
        self.assertEqual(self.select()['rest_name'], 'other_idle')
        self.obj['Conversation']['IdleAnimation'] = 'other'
        self.assertEqual(self.select()['provenance']['ponybook']['idle']['name'], 'bind')
        self.assertEqual(self.select()['rest_name'], 'bind')
        self.assertNotIn('other_idle', self.select()['clip_names'])
