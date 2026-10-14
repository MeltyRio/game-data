"""Synthetic public appearance / town-action boundary checks."""
import copy
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

    def test_constructor_pose_and_later_idle_are_separate(self):
        result = self.select()
        self.assertEqual(result['rest_name'], 'gen_idle_00')
        self.assertEqual(result['clip_names'], ['town_idle', 'town_walk', 'bind', 'gen_idle_00'])
        self.assertEqual(result['provenance']['ponybook']['constructor']['requested_name'], 'default')
        self.assertEqual(result['provenance']['ponybook']['idle']['name'], 'gen_idle_00')
        self.assertEqual(result['visible_meshes'], ['body', 'eyes_open'])
        self.assertNotIn('foreign', result['clip_names'])
        self.assertEqual(result['additional_event_paths'], [self.folder/'book.xml'])

    def test_explicit_constructor_default_never_uses_first_row(self):
        self.clips.append({'name': 'default', 'start': 27, 'end': 27, 'fps': 15})
        result = self.select()
        self.assertEqual(result['rest_name'], 'gen_idle_00')
        self.assertEqual(result['provenance']['ponybook']['constructor']['name'], 'default')
        self.assertIn('default', result['clip_names'])

    def test_original_first_row_not_filtered_first_valid_clip(self):
        self.fails('ponybook_idle_unverified', csv_first_name='bad_original', csv_excluded_names=['bad_original'])

    def test_rejected_default_is_not_a_native_name_miss(self):
        self.fails('ponybook_idle_unverified', csv_excluded_names=['default'])

    def test_present_conversation_selects_exact_name_not_prefix(self):
        self.obj['Conversation'] = {'IdleAnimation': 'other_idle'}
        self.assertEqual(self.select()['provenance']['ponybook']['idle']['name'], 'other_idle')
        self.assertEqual(self.select()['rest_name'], 'other_idle')
        self.obj['Conversation']['IdleAnimation'] = 'other'
        self.assertEqual(self.select()['provenance']['ponybook']['idle']['name'], 'bind')
        self.assertEqual(self.select()['rest_name'], 'bind')
        self.assertNotIn('other_idle', self.select()['clip_names'])

    def test_empty_conversation_is_not_absent_conversation(self):
        self.obj['Conversation'] = {'IdleAnimation': ''}
        self.fails('ponybook_idle_unverified')
        result = self.select('ponybook-preferred')
        self.assertEqual(result['provenance']['display']['selected'], 'world-preview')
        self.assertEqual(result['rest_name'], 'town_idle')

    def test_eventful_book_idle_rejects_book_and_preserves_verified_world(self):
        self.xml('book', ['gen_idle_00'])
        self.fails('ponybook_idle_requires_events')
        world = self.select('world-preview')
        result = self.select('ponybook-preferred')
        for field in ['rest_name', 'visible_meshes', 'clip_names', 'event_path', 'events_absent']:
            self.assertEqual(result[field], world[field])
        self.assertEqual(result['provenance']['display']['selected'], 'world-preview')
        self.assertEqual(result['provenance']['display']['ponybook_unavailable']['code'],
                         'ponybook_idle_requires_events')

    def test_constructor_event_blocks_strict_profile(self):
        self.xml('book', ['bind'])
        self.fails('ponybook_idle_requires_events')

    def test_missing_book_source_keeps_verified_world_without_claiming_book(self):
        world = self.select('world-preview')
        (self.folder/'book.xml').unlink()
        self.fails('missing_event_source')
        result = self.select('ponybook-preferred')
        for field in ['rest_name', 'visible_meshes', 'clip_names', 'event_path', 'events_absent']:
            self.assertEqual(result[field], world[field])
        display = result['provenance']['display']
        self.assertEqual(display['selected'], 'world-preview')
        self.assertEqual(display['ponybook_unavailable']['code'], 'missing_event_source')
        self.assertNotIn('ponybook', result['provenance'])

    def test_missing_world_source_is_not_hidden_by_available_book(self):
        (self.folder/'world.xml').unlink()
        self.fails('missing_event_source', profile='ponybook-preferred')

    def test_bad_book_source_records_reason_not_successful_book(self):
        (self.folder/'book.xml').write_text('<!DOCTYPE x><AnimationList/>')
        self.fails('invalid_event_source')
        result = self.select('ponybook-preferred')
        self.assertEqual(result['provenance']['display']['ponybook_unavailable']['code'], 'invalid_event_source')

    def test_world_bypass_does_not_bypass_book(self):
        self.obj['AI']['Special_AI'] = 2
        self.obj['Animation']['AnimationEventsXML'] = ''
        result = self.select()
        self.assertTrue(result['events_absent'])
        self.assertIn('world_event', result['clip_names'])
        self.assertNotIn('book_event', result['clip_names'])
        (self.folder/'book.xml').unlink()
        self.fails('missing_event_source')

    def test_same_event_source_has_no_duplicate_converter_dependency(self):
        self.obj['Animation']['AnimationEventsXML'] = 'book'
        self.assertEqual(self.select()['additional_event_paths'], [])

    def test_native_first_item_case_and_lod_are_exact(self):
        self.obj['Eyes'] = {f'EyeSubObject_{state}': [first, later] for state, first, later in [
            ('Open', 'eye_lod0_open', 'eye_lod1_open'), ('Closed', 'eye_lod0_shut', 'eye_lod1_shut'),
            ('Happy', 'eye_lod0_happy', 'wrong'), ('Frown', 'eye_lod0_frown', 'eye_lod1_frown')]}
        self.meshes = ['body', 'eye_lod1_open', 'eye_lod1_shut', 'eye_lod1_happy', 'eye_lod1_frown']
        self.assertEqual(self.select()['visible_meshes'], ['body', 'eye_lod1_open'])
        self.obj['Eyes']['EyeSubObject_Open'][0] = 'EYE_lod0_open'
        self.fails('ponybook_eye_policy_unverified')

    def test_visibility_disagreement_is_explicit_and_preserves_existing_view(self):
        self.xml('book', defaults='<body><SubObject ID="body" Name="body" DefaultVisible="0"/></body>')
        self.fails('ponybook_visibility_unverified')
        result = self.select('ponybook-preferred')
        self.assertEqual(result['visible_meshes'], ['body', 'eyes_open'])
        self.assertEqual(result['provenance']['display']['selected'], 'world-preview')

    def test_unknown_profile_fails_before_selection(self):
        self.fails('invalid_display_profile', profile='guess')

    def test_pets_keep_existing_editor_policy(self):
        pet = {'Object': {'Model': 'body', 'AnimationIdle': 'town_idle'}}
        result = self.select('ponybook-preferred', category='PonyPet', obj=pet)
        self.assertEqual(result['rest_name'], 'town_idle')
        self.assertEqual(result['provenance']['display']['selected'], 'pet-editor-preview')
