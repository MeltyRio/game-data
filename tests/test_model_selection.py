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


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.obj = {'Model': {'MediumLOD': 'body'}, 'AI': {'Special_AI': 0},
                    'Animation': {'Rig_MediumLOD': 'pony_type01', 'AnimationEventsXML': '',
                                  'LeftIdleAnimationList': ['idle', 'blink'],
                                  'RightIdleAnimationList': ['idle_right']},
                    'AdditionalObject': {'Model': '', 'Animation': '', 'Scale': 0}}
        self.meshes = ['body', 'eyes_open', 'eyes_shut', 'eyes_happy', 'eyes_frown', 'prop']
        self.clips = [{'name': name, 'start': i * 3, 'end': i * 3 + 2, 'fps': 15}
                      for i, name in enumerate(['idle', 'blink', 'idle_right', 'foreign'])]
        self.xml = ('<MeshList><body><SubObject ID="prop" Name="prop" DefaultVisible="0" />'
                    '</body></MeshList><AnimationList><Animation Name="blink">'
                    '<Frame Index="1"><EyeSet Open="Closed" /></Frame></Animation></AnimationList>')
        self.write_events()

    def write_events(self, text=None, stem='pony_type01'):
        (self.folder / (stem + '.xml')).write_text(self.xml if text is None else text)

    def select(self, **kwargs):
        args = {'category': 'Pony', 'obj': self.obj, 'game_objects': Objects(),
                'model_stem': 'body', 'mesh_names': self.meshes,
                'clips': self.clips, 'extracted_dir': self.folder}
        args.update(kwargs)
        return select_native_view(**args)

    def fails(self, code, **kwargs):
        with self.assertRaises(SelectionError) as result:
            self.select(**kwargs)
        self.assertEqual(result.exception.code, code)

    def test_ordinary_native_type01_selection(self):
        result = self.select()
        self.assertEqual(result['visible_meshes'], ['body', 'eyes_open'])
        self.assertEqual(result['clip_names'], ['idle', 'idle_right'])
        self.assertEqual(result['rest_name'], 'idle')
        self.assertFalse(result['events_absent'])
        self.assertEqual(result['excluded_clips'][0]['reason'], 'native_events_not_supported')
        self.assertNotIn('foreign', result['clip_names'])
        self.assertEqual(result['provenance']['eyes']['field'], 'CATEGORY_DATA.Pony.Eyes')

    def test_explicit_event_source_with_different_library(self):
        self.obj['Animation'].update(Rig_MediumLOD='other_rig', AnimationEventsXML='other_events')
        self.write_events(stem='other_events')
        self.assertEqual(self.select()['event_path'].name, 'other_events.xml')

    def test_special_ai_two_bypasses_xml_controller_and_mesh_defaults(self):
        self.obj['AI']['Special_AI'] = 2
        result = self.select()
        self.assertIsNone(result['event_path'])
        self.assertTrue(result['events_absent'])
        self.assertEqual(result['visible_meshes'], ['body', 'eyes_open', 'prop'])
        # This XML action does not apply in the native AI=2 world branch.
        self.assertIn('blink', result['clip_names'])
        self.assertEqual(result['provenance']['mesh_defaults'], {})
        self.assertEqual(result['provenance']['events']['special_ai'], 2)

    def test_special_ai_two_bypass_does_not_read_a_missing_xml(self):
        self.obj['AI']['Special_AI'] = 2
        self.obj['Animation']['Rig_MediumLOD'] = 'other_rig'
        (self.folder / 'pony_type01.xml').unlink()
        self.assertTrue(self.select()['events_absent'])

    def test_special_ai_two_keeps_explicit_event_source_priority(self):
        self.obj['AI']['Special_AI'] = 2
        self.obj['Animation']['AnimationEventsXML'] = 'explicit'
        self.write_events(stem='explicit')
        result = self.select()
        self.assertEqual(result['event_path'].name, 'explicit.xml')
        self.assertFalse(result['events_absent'])
        self.assertNotIn('prop', result['visible_meshes'])
        self.assertNotIn('blink', result['clip_names'])

    def test_unknown_or_missing_ai_does_not_select_a_world_controller(self):
        for value in [None, -1, 6, '2', 2.0, True]:
            with self.subTest(value=value):
                self.obj['AI']['Special_AI'] = value
                self.fails('unknown_special_ai')
        self.obj.pop('AI')
        self.fails('unknown_special_ai')

    def test_explicit_event_source_does_not_require_the_unused_ai_branch(self):
        self.obj.pop('AI')
        self.obj['Animation']['AnimationEventsXML'] = 'explicit'
        self.write_events(stem='explicit')
        self.assertEqual(self.select()['event_path'].name, 'explicit.xml')

    def test_shared_library_retains_each_objects_declared_actions(self):
        first = self.select()
        other = copy.deepcopy(self.obj)
        other['Animation']['LeftIdleAnimationList'] = ['foreign']
        other['Animation']['RightIdleAnimationList'] = []
        second = self.select(obj=other)
        self.assertEqual(first['clip_names'], ['idle', 'idle_right'])
        self.assertEqual(second['clip_names'], ['foreign'])

    def test_explicit_lod_lists_and_shared_eye_alias(self):
        self.obj['Eyes'] = {'EyeSubObject_Open': ['absent_lod0', 'eyes_open'],
                            'EyeSubObject_Closed': ['eyes_shut'],
                            'EyeSubObject_Happy': ['eyes_open'], 'EyeSubObject_Frown': ['eyes_frown']}
        self.assertIn('eyes_open', self.select()['visible_meshes'])
        self.assertNotIn('eyes_shut', self.select()['visible_meshes'])

    def complete_native_eyes(self, wrong_state='Happy'):
        stems = {'Open': 'open', 'Closed': 'shut', 'Happy': 'happy', 'Frown': 'frown'}
        self.obj['Eyes'] = {'EyeSubObject_' + state: ['a_lod0_' + stem, 'a_lod1_' + stem]
                            for state, stem in stems.items()}
        self.obj['Eyes']['EyeSubObject_' + wrong_state][1] = 'wrong_later_item'
        self.meshes = ['body', 'a_lod1_open', 'a_lod1_shut', 'a_lod1_happy', 'a_lod1_frown', 'prop']

    def test_complete_native_controller_hides_real_state_missed_by_later_item(self):
        for state in ['Happy', 'Frown']:
            with self.subTest(state=state):
                self.complete_native_eyes(state)
                result = self.select()
                self.assertEqual(result['visible_meshes'], ['body', 'a_lod1_open'])
                self.assertEqual(result['clip_names'], ['idle', 'idle_right'])
                self.assertEqual(result['rest_name'], 'idle')
                evidence = result['provenance']['eyes']
                self.assertEqual(evidence['added_hidden_meshes'], ['a_lod1_' + state.lower()])
                self.assertEqual(evidence['resolved_native_controller'][state], 'a_lod1_' + state.lower())

    def test_incomplete_native_controller_keeps_explicit_preview_policy(self):
        self.complete_native_eyes()
        self.obj['Eyes']['EyeSubObject_Closed'][0] = 'absent_first_item'
        result = self.select()
        self.assertEqual(result['visible_meshes'], ['body', 'a_lod1_open', 'a_lod1_happy'])
        self.assertNotIn('resolved_native_controller', result['provenance']['eyes'])

    def test_native_controller_with_shared_states_does_not_add_hides(self):
        self.complete_native_eyes()
        self.obj['Eyes']['EyeSubObject_Frown'][0] = 'a_lod0_shut'
        result = self.select()
        self.assertIn('a_lod1_happy', result['visible_meshes'])
        self.assertNotIn('resolved_native_controller', result['provenance']['eyes'])

    def test_native_controller_requires_same_unique_explicit_open(self):
        for ambiguous in [False, True]:
            with self.subTest(ambiguous=ambiguous):
                self.complete_native_eyes()
                self.obj['Eyes']['EyeSubObject_Open'] = ['a_lod0_open', 'other_open']
                self.meshes.append('other_open')
                if ambiguous:
                    self.meshes.append('a_lod0_open')
                result = self.select()
                self.assertIn('a_lod1_happy', result['visible_meshes'])
                self.assertNotIn('resolved_native_controller', result['provenance']['eyes'])

    def test_blank_first_item_is_not_replaced_by_a_later_nonempty_item(self):
        for state in ['Open', 'Closed']:
            with self.subTest(state=state):
                self.complete_native_eyes()
                self.obj['Eyes']['EyeSubObject_' + state].insert(0, '')
                result = self.select()
                self.assertIn('a_lod1_happy', result['visible_meshes'])
                self.assertNotIn('resolved_native_controller', result['provenance']['eyes'])

    def test_native_lod_replacement_changes_only_first_literal_occurrence(self):
        self.complete_native_eyes()
        self.obj['Eyes']['EyeSubObject_Happy'][0] = 'a_lod0_lod0_happy'
        self.meshes[self.meshes.index('a_lod1_happy')] = 'a_lod1_lod0_happy'
        self.meshes.append('a_lod1_lod1_happy')
        result = self.select()
        self.assertNotIn('a_lod1_lod0_happy', result['visible_meshes'])
        self.assertIn('a_lod1_lod1_happy', result['visible_meshes'])
        self.assertEqual(result['provenance']['eyes']['added_hidden_meshes'], ['a_lod1_lod0_happy'])

    def test_newly_resolved_eye_still_rejects_unverified_meshlist_order(self):
        self.complete_native_eyes()
        self.write_events(self.xml.replace('Name="prop"', 'Name="a_lod1_happy"'))
        self.fails('eye_subobject_order_unverified')

    def test_single_mesh_absent_native_eyes_are_an_exact_lookup_no_op(self):
        self.meshes = ['body']
        result = self.select()
        self.assertEqual(result['visible_meshes'], ['body'])
        self.assertEqual(result['provenance']['eyes']['rule'], 'reviewed-single-mesh-eye-lookups-no-op')
        self.assertIn('eyes_shut', result['provenance']['eyes']['lookup_candidates'])
        self.assertNotIn('blink', result['clip_names'])

    def test_single_mesh_no_op_records_declared_and_native_lod_candidates(self):
        self.meshes = ['body']
        self.obj['Eyes'] = {'EyeSubObject_Open': ['custom_lod0_open', 'later_open'],
                            'EyeSubObject_Closed': ['custom_lod00_shut']}
        result = self.select()
        candidates = result['provenance']['eyes']['lookup_candidates']
        for name in ['custom_lod0_open', 'custom_lod1_open', 'later_open',
                     'custom_lod00_shut', 'custom_lod10_shut', 'eyes_shut']:
            self.assertIn(name, candidates)
        self.assertEqual(result['visible_meshes'], ['body'])

    def test_single_mesh_any_declared_eye_match_prevents_no_op(self):
        self.meshes = ['body']
        for state in ['Closed', 'Happy', 'Frown']:
            with self.subTest(state=state):
                self.obj['Eyes'] = {'EyeSubObject_Open': ['absent'], 'EyeSubObject_' + state: ['body']}
                self.fails('missing_open_eye_mesh')

    def test_single_mesh_lod_transformed_match_prevents_no_op(self):
        self.meshes = ['custom_lod1_eye']
        for state in ['Open', 'Closed', 'Happy', 'Frown']:
            with self.subTest(state=state):
                self.obj['Eyes'] = {'EyeSubObject_Open': ['absent'], 'EyeSubObject_' + state: ['custom_lod0_eye']}
                self.fails('missing_open_eye_mesh')

    def test_single_mesh_native_default_match_prevents_no_op_after_schema_change(self):
        objects = Objects()
        objects.CATEGORY_DATA = copy.deepcopy(Objects.CATEGORY_DATA)
        for field in objects.CATEGORY_DATA['Pony']['Eyes']['attributes'].values():
            field['default'] = 'future_default'
        self.meshes = ['eyes_shut']
        self.fails('missing_open_eye_mesh', game_objects=objects)

    def test_multiple_meshes_including_duplicate_names_do_not_take_single_mesh_path(self):
        for meshes in [['body', 'other'], ['body', 'body']]:
            with self.subTest(meshes=meshes):
                self.fails('missing_open_eye_mesh', mesh_names=meshes)

    def test_single_mesh_no_op_is_not_applied_to_wardrobe(self):
        objects = self.wardrobe()
        self.meshes = ['body0']
        self.fails('missing_open_eye_mesh', game_objects=objects)

    def test_single_mesh_no_op_keeps_other_visibility_controllers(self):
        self.meshes = ['body']
        self.write_events(self.xml.replace('Name="prop"', 'Name="body"'))
        self.fails('no_visible_meshes')

    def test_new_explicit_eye_state_is_not_left_visible(self):
        self.obj['Eyes'] = {'EyeSubObject_Open': ['eyes_open'],
                            'EyeSubObject_Surprised': ['eyes_surprised']}
        self.meshes.append('eyes_surprised')
        self.fails('unsupported_eye_state')

    def test_new_schema_eye_state_is_not_ignored(self):
        objects = Objects()
        objects.CATEGORY_DATA = copy.deepcopy(Objects.CATEGORY_DATA)
        objects.CATEGORY_DATA['Pony']['Eyes']['attributes']['EyeSubObject_Surprised'] = {'default': 'eyes_surprised'}
        self.meshes.append('eyes_surprised')
        self.fails('unsupported_eye_state', game_objects=objects)

    def test_unrelated_eye_metadata_is_not_a_new_state(self):
        self.obj['Eyes'] = {'EyeSubObject_Open': ['eyes_open'], 'BlinkInterval': 5}
        self.select()

    def test_subobject_eye_overlap_requires_controller_order_evidence(self):
        self.write_events(self.xml.replace('Name="prop"', 'Name="eyes_open"'))
        self.fails('eye_subobject_order_unverified')

    def test_nested_subobject_controller_is_not_silently_discarded(self):
        self.write_events(self.xml.replace('DefaultVisible="0" />',
                                          'DefaultVisible="0"><FutureVisibility /></SubObject>'))
        self.fails('unsupported_mesh_defaults')

    def test_explicit_empty_open_disables_eye_controls_but_keeps_mesh_defaults(self):
        self.obj['Eyes'] = {'EyeSubObject_Open': [], 'EyeSubObject_Happy': ['eyes_open']}
        result = self.select()
        self.assertEqual(result['visible_meshes'], self.meshes[:-1])
        self.assertEqual(result['provenance']['eyes']['rule'], 'reviewed-empty-Open-disables-eye-controller')
        self.assertFalse(result['provenance']['eyes']['enabled'])
        self.assertNotIn('blink', result['clip_names'])

    def test_explicit_blank_open_items_do_not_invent_eye_names(self):
        self.obj['Eyes'] = {'EyeSubObject_Open': ['', ''], 'EyeSubObject_Closed': ['eyes_shut']}
        self.assertIn('eyes_shut', self.select()['visible_meshes'])

    def test_empty_open_retains_other_static_controllers(self):
        objects = self.wardrobe()
        self.obj['Eyes'] = {'EyeSubObject_Open': []}
        result = self.select(game_objects=objects)
        self.assertEqual(result['visible_meshes'], ['body0', 'mane0', 'tail0', 'eyes_open', 'eyes_shut'])

    def test_empty_open_does_not_allow_unknown_eye_state(self):
        self.obj['Eyes'] = {'EyeSubObject_Open': [], 'EyeSubObject_Future': ['prop']}
        self.fails('unsupported_eye_state')

    def test_empty_additional_object_is_not_a_controller(self):
        self.select()
        self.obj['AdditionalObject']['Model'] = 'child_model'
        self.fails('additional_object_not_supported')

    def test_missing_wardrobe_metadata_is_rejected(self):
        self.obj['Sets'] = {'Sets': ['default_set', 'other_set']}
        self.fails('missing_wardrobe_set')

    def wardrobe(self):
        from test_model_wardrobe import fixture
        owner, data = fixture()
        self.obj['Sets'] = owner['Sets']
        self.meshes = ['body0', 'mane0', 'tail0', 'body1', 'mane1', 'tail1',
                       'eyes_open', 'eyes_shut', 'prop']
        data['PonyPart']['body1']['PonyPart']['LinkedPart'] = 'tail1'
        return Objects(data)

    def test_default_wardrobe_and_complete_outfit_use_owner_eyes_and_events(self):
        objects = self.wardrobe()
        default = self.select(game_objects=objects)
        self.assertEqual(default['visible_meshes'], ['body0', 'mane0', 'tail0', 'eyes_open'])
        outfit = self.select(game_objects=objects, outfit_id='dress')
        self.assertEqual(outfit['visible_meshes'], ['body1', 'mane1', 'tail1', 'eyes_open'])
        self.assertEqual(outfit['clip_names'], default['clip_names'])
        self.assertEqual(outfit['event_path'], default['event_path'])

    def test_absent_valid_part_mesh_hides_other_tails_without_substitution(self):
        objects = self.wardrobe()
        objects['PonyPart']['tail1']['PonyPart']['ModelName'] = 'no_mesh'
        # Remove geometry corresponding to the old fixture declaration.
        self.meshes.remove('tail1')
        result = self.select(game_objects=objects, outfit_id='dress')
        self.assertNotIn('tail0', result['visible_meshes'])
        self.assertEqual(result['provenance']['wardrobe']['absent_declared_meshes'], ['no_mesh'])

    def test_wardrobe_event_controller_overlap_is_not_guessed(self):
        objects = self.wardrobe()
        self.write_events(self.xml.replace('Name="prop"', 'Name="body1"'))
        self.fails('wardrobe_subobject_order_unverified', game_objects=objects)

    def test_wardrobe_eye_controller_overlap_is_not_guessed(self):
        objects = self.wardrobe()
        objects['PonyPart']['body1']['PonyPart']['ModelName'] = 'eyes_open'
        self.fails('eye_wardrobe_order_unverified', game_objects=objects)

    def test_missing_events_never_means_events_absent(self):
        (self.folder / 'pony_type01.xml').unlink()
        self.fails('missing_event_source')

    def test_known_ai_world_fallback_is_literal_and_not_derived_from_rig(self):
        self.obj['Animation']['Rig_MediumLOD'] = 'other_rig'
        self.write_events('<AnimationList/>', stem='other_rig')
        for value in [0, 1, 3, 4, 5]:
            with self.subTest(value=value):
                self.obj['AI']['Special_AI'] = value
                result = self.select()
                self.assertEqual(result['event_path'].name, 'pony_type01.xml')
                self.assertNotIn('prop', result['visible_meshes'])
                self.assertNotIn('blink', result['clip_names'])
                self.assertFalse(result['events_absent'])

    def test_first_idle_eventful_does_not_scan_next_working_idle(self):
        self.obj['Animation']['LeftIdleAnimationList'] = ['blink', 'idle']
        self.fails('initial_pose_requires_events')

    def test_first_idle_missing_does_not_scan_next_working_idle(self):
        self.obj['Animation']['LeftIdleAnimationList'] = ['missing', 'idle']
        self.fails('declared_idle_not_in_csv')

    def test_absent_pony_idle_uses_original_csv_first_only_with_complete_audit(self):
        self.obj['Animation']['LeftIdleAnimationList'] = ['missing', 'idle']
        result = self.select(csv_first_name='foreign', csv_excluded_names=[])
        self.assertEqual(result['rest_name'], 'foreign')
        self.assertEqual(result['clip_names'], ['idle', 'idle_right', 'foreign'])
        self.assertEqual(result['provenance']['rest']['requested_name'], 'missing')
        self.assertEqual(result['provenance']['rest']['rule'], 'reviewed-native-name-lookup-miss-to-original-CSV-row-0')
        self.assertEqual(result['excluded_clips'][0]['name'], 'missing')

    def test_absent_idle_requires_exclusion_audit_and_raw_first_name(self):
        self.obj['Animation']['LeftIdleAnimationList'] = ['missing']
        for excluded in [None, 'missing', [None]]:
            with self.subTest(excluded=excluded):
                self.fails('declared_idle_not_in_csv', csv_first_name='foreign', csv_excluded_names=excluded)
        for first in [None, '', 'bad_first']:
            with self.subTest(first=first):
                self.fails('declared_idle_not_in_csv', csv_first_name=first, csv_excluded_names=[])

    def test_invalid_or_conflicting_named_idle_never_falls_back(self):
        from game_data.model_gen.converter.csv import read_clips
        self.obj['Animation']['LeftIdleAnimationList'] = ['missing']
        for rows in ['foreign,0,2,15\nmissing,broken,5,15\n',
                     'foreign,0,2,15\nmissing,3,5,15\nmissing,6,8,15\n']:
            with self.subTest(rows=rows):
                path = self.folder / 'strict.csv'
                path.write_text(rows)
                metadata = {}
                clips, _, diagnostics = read_clips(path, 20, allow_partial=True, source_metadata=metadata)
                excluded = sorted({row['name'] for row in diagnostics if row['name']})
                self.fails('declared_idle_not_in_csv', clips=clips,
                           csv_first_name=metadata['first_declared_name'], csv_excluded_names=excluded)

    def test_bad_or_conflicting_original_first_row_never_scans_later(self):
        from game_data.model_gen.converter.csv import read_clips
        self.obj['Animation']['LeftIdleAnimationList'] = ['missing']
        for rows in ['bad_first,broken,2,15\nforeign,3,5,15\n',
                     'bad_first,0,2,15\nforeign,3,5,15\nbad_first,6,8,15\n']:
            with self.subTest(rows=rows):
                path = self.folder / 'strict.csv'
                path.write_text(rows)
                metadata = {}
                clips, _, diagnostics = read_clips(path, 20, allow_partial=True, source_metadata=metadata)
                excluded = sorted({row['name'] for row in diagnostics if row['name']})
                self.fails('declared_idle_not_in_csv', clips=clips,
                           csv_first_name=metadata['first_declared_name'], csv_excluded_names=excluded)

    def test_native_first_row_fallback_still_rejects_eventful_rest(self):
        self.obj['Animation']['LeftIdleAnimationList'] = ['missing']
        self.fails('initial_pose_requires_events', csv_first_name='blink', csv_excluded_names=[])

    def test_conversation_idle_precedes_left_list(self):
        self.obj['Conversation'] = {'IdleAnimation': 'idle_right'}
        result = self.select()
        self.assertEqual(result['rest_name'], 'idle_right')
        self.assertEqual(result['provenance']['rest']['field'], 'Conversation.IdleAnimation')

    def test_no_declared_rest_requires_complete_csv_audit(self):
        self.obj['Animation']['LeftIdleAnimationList'] = []
        for excluded in [None, 'bad', [None]]:
            with self.subTest(excluded=excluded):
                self.fails('native_initial_pose_unverified', csv_first_name='foreign',
                           csv_excluded_names=excluded)

    def test_native_loader_prefers_exact_default_over_original_first_clip(self):
        self.obj['Animation'] = {'AnimationEventsXML': ''}
        self.clips.append({'name': 'default', 'start': 12, 'end': 14, 'fps': 15})
        result = self.select(csv_first_name='foreign', csv_excluded_names=[])
        self.assertEqual(result['rest_name'], 'default')
        self.assertEqual(result['clip_names'], ['default'])
        rest = result['provenance']['rest']
        self.assertEqual(rest['requested_name'], 'default')
        self.assertTrue(rest['default_clip_present'])
        self.assertEqual(rest['rule'], 'reviewed-native-RK-load-default-clip')
        self.assertIn('RK drawable load', rest['context'])

    def test_native_loader_missing_default_uses_only_original_csv_first(self):
        self.obj['Animation'] = {'AnimationEventsXML': ''}
        result = self.select(csv_first_name='foreign', csv_excluded_names=[])
        self.assertEqual(result['rest_name'], 'foreign')
        self.assertEqual(result['clip_names'], ['foreign'])
        rest = result['provenance']['rest']
        self.assertFalse(rest['default_clip_present'])
        self.assertEqual(rest['original_csv_first_name'], 'foreign')
        self.assertEqual(rest['rule'], 'reviewed-native-RK-load-default-miss-to-original-CSV-row-0')
        self.assertNotIn('idle', result['clip_names'])

    def test_declared_idle_still_precedes_native_loader_default(self):
        self.clips.append({'name': 'default', 'start': 12, 'end': 14, 'fps': 15})
        result = self.select(csv_first_name='foreign', csv_excluded_names=[])
        self.assertEqual(result['rest_name'], 'idle')
        self.assertNotIn('default', result['clip_names'])
        self.assertNotIn('default_clip_present', result['provenance']['rest'])
        self.obj['Conversation'] = {'IdleAnimation': 'idle_right'}
        self.assertEqual(self.select()['rest_name'], 'idle_right')

    def test_native_loader_never_skips_bad_default_or_original_first(self):
        from game_data.model_gen.converter.csv import read_clips
        self.obj['Animation']['LeftIdleAnimationList'] = []
        for rows in ['foreign,0,2,15\ndefault,broken,5,15\n',
                     'foreign,0,2,15\ndefault,3,5,15\ndefault,6,8,15\n',
                     'bad_first,broken,2,15\nforeign,3,5,15\n',
                     'bad_first,0,2,15\nforeign,3,5,15\nbad_first,6,8,15\n',
                     '\nforeign,3,5,15\n']:
            with self.subTest(rows=rows):
                path = self.folder / 'strict.csv'
                path.write_text(rows)
                metadata = {}
                clips, _, diagnostics = read_clips(path, 20, allow_partial=True, source_metadata=metadata)
                excluded = sorted({row['name'] for row in diagnostics if row['name']})
                self.fails('native_initial_pose_unverified', clips=clips,
                           csv_first_name=metadata['first_declared_name'], csv_excluded_names=excluded)

    def test_native_loader_rest_still_requires_event_free_clip(self):
        self.obj['Animation']['LeftIdleAnimationList'] = []
        self.fails('initial_pose_requires_events', csv_first_name='blink', csv_excluded_names=[])
        self.clips.append({'name': 'default', 'start': 12, 'end': 14, 'fps': 15})
        self.write_events(self.xml.replace('Name="blink"', 'Name="default"'))
        self.fails('initial_pose_requires_events', csv_first_name='foreign', csv_excluded_names=[])

    def test_native_loader_default_does_not_repair_missing_controller_or_all_hidden(self):
        self.obj['Animation']['LeftIdleAnimationList'] = []
        (self.folder / 'pony_type01.xml').unlink()
        self.fails('missing_event_source', csv_first_name='foreign', csv_excluded_names=[])
        self.meshes = ['body']
        self.write_events('<MeshList><body><SubObject ID="body" Name="body" DefaultVisible="0" />'
                          '</body></MeshList><AnimationList />')
        self.fails('no_visible_meshes', csv_first_name='foreign', csv_excluded_names=[])

    def test_missing_nonrest_clip_is_explicit_diagnostic(self):
        self.obj['Animation']['RightIdleAnimationList'] = ['missing']
        result = self.select()
        self.assertEqual(result['excluded_clips'][-1]['reason'], 'declared_clip_not_in_csv')

    def test_duplicate_event_declarations_cannot_erase_events(self):
        self.write_events(self.xml.replace('</AnimationList>', '<Animation Name="blink" /></AnimationList>'))
        self.assertNotIn('blink', self.select()['clip_names'])

    def test_conflicting_mesh_default_rejected(self):
        self.write_events(self.xml.replace('</body>', '<SubObject ID="second" Name="prop" DefaultVisible="1" /></body>'))
        self.fails('conflicting_mesh_defaults')

    def test_missing_controlled_mesh_rejected(self):
        self.write_events(self.xml.replace('Name="prop" DefaultVisible="0"', 'Name="missing_mesh" DefaultVisible="1"'))
        self.fails('missing_controlled_mesh')

    def test_other_model_defaults_do_not_apply(self):
        self.write_events(self.xml.replace('<body>', '<other>').replace('</body>', '</other>'))
        self.assertIn('prop', self.select()['visible_meshes'])

    def test_event_document_structure_and_dtd_rejected(self):
        self.write_events('<!DOCTYPE anything><AnimationList />')
        self.fails('invalid_event_source')
        self.write_events('<Unknown />')
        self.fails('unsupported_event_source')

    def test_missing_schema_is_not_hardcoded(self):
        self.fails('missing_eye_defaults', game_objects={})

    def test_pet_declared_idle_has_positive_controller_absence_evidence(self):
        obj = {'Object': {'Model': 'body', 'Animation': 'pet_rig', 'AnimationIdle': 'idle'}}
        (self.folder / 'pony_type01.xml').unlink()
        result = self.select(category='PonyPet', obj=obj)
        self.assertIsNone(result['event_path'])
        self.assertTrue(result['events_absent'])
        self.assertEqual(result['visible_meshes'], [m for m in self.meshes if m != 'eyes_shut'])
        self.assertEqual(result['clip_names'], ['idle'])

    def test_pet_eye_names_are_exact_and_do_not_invent_aliases(self):
        obj = {'Object': {'Model': 'body', 'Animation': 'pet_rig', 'AnimationIdle': 'blink'}}
        self.meshes = ['body', 'eyes_shut', 'deer_eyes_open', 'deer_eyes_shut', 'open_open']
        result = self.select(category='PonyPet', obj=obj)
        self.assertEqual(result['visible_meshes'], ['body', 'deer_eyes_open', 'deer_eyes_shut', 'open_open'])
        # The existing Pony XML marks blink eventful, but the Pet controller
        # does not load that resource, even when it exists locally.
        self.assertEqual(result['clip_names'], ['blink'])

    def test_empty_pet_idle_requires_original_valid_csv_first_row(self):
        obj = {'Object': {'Model': 'body', 'Animation': 'pet_rig', 'AnimationIdle': ''}}
        self.fails('original_first_csv_clip_unverified', category='PonyPet', obj=obj)
        result = self.select(category='PonyPet', obj=obj, csv_first_name='foreign')
        self.assertEqual(result['clip_names'], ['foreign'])
        self.assertEqual(result['rest_name'], 'foreign')

    def test_bad_pet_first_row_never_uses_later_valid_clip(self):
        obj = {'Object': {'Model': 'body', 'Animation': 'pet_rig', 'AnimationIdle': ''}}
        self.fails('declared_idle_not_in_csv', category='PonyPet', obj=obj, csv_first_name='invalid_first')

    def test_explicit_missing_pet_idle_never_falls_back_to_first_csv(self):
        obj = {'Object': {'Model': 'body', 'Animation': 'pet_rig', 'AnimationIdle': 'missing'}}
        self.fails('declared_idle_not_in_csv', category='PonyPet', obj=obj, csv_first_name='idle')

    def test_missing_default_hidden_prop_is_a_recorded_no_op(self):
        self.write_events(self.xml.replace('Name="prop"', 'Name="not_in_rk"'))
        result = self.select()
        self.assertEqual(result['provenance']['absent_default_hidden_meshes'], ['not_in_rk'])
        self.assertNotIn('blink', result['clip_names'])

    def test_declared_field_index_preserved_across_empty_items(self):
        self.obj['Animation']['LeftIdleAnimationList'] = ['', 'idle']
        self.assertEqual(self.select()['provenance']['clip_fields']['idle'][0],
                         'Animation.LeftIdleAnimationList[1]')

    def test_symlink_event_outside_source_is_rejected(self):
        (self.folder / 'pony_type01.xml').unlink()
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / 'event.xml'
            target.write_text(self.xml)
            (self.folder / 'pony_type01.xml').symlink_to(target)
            self.fails('invalid_event_source')


if __name__ == '__main__':
    unittest.main()
