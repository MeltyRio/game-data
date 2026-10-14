"""Synthetic native controls and normal generation; no game assets required."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from bootstrap import ROOT
from native_fixture import write_fixture, write_native_tables
from game_data.model_gen.interactions import native_click_names, prepare_interactions, InteractionError
from game_data.model_gen.selection import select_native_view
from game_data.model_gen import generate_models
from game_data.model_gen.converter.native import NativeInputError, _verify_event_source
from game_data.model_associations import _model


class Objects(dict):
    CATEGORY_DATA = {'Pony': {'Eyes': {'attributes': {
        'EyeSubObject_' + state: {'default': name}
        for state, name in [('Open', 'eyes_open'), ('Closed', 'eyes_shut'),
                            ('Happy', 'eyes_happy'), ('Frown', 'eyes_frown')]
    }}}}


def glb_json(data):
    return json.loads(data[20:20 + struct.unpack_from('<I', data, 12)[0]])


class NativeControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'rig.xml'
        self.names = ['body', 'eyes_open', 'eyes_shut', 'eyes_shut', 'eyes_happy', 'eyes_frown']
        self.obj = {'Model': {'MediumLOD': 'body'}, 'AI': {'Special_AI': 0},
                    'Animation': {'Rig_MediumLOD': 'rig', 'AnimationEventsXML': 'rig',
                                  'LeftIdleAnimationList': ['rest', 'wave']}}
        self.clips = [{'name': name, 'start': 0, 'end': 4, 'fps': 15} for name in
                      ['rest', 'wave', 'gen_tap_north', 'gen_snap_01', 'arena_pony_defeat']]
        self.xml('<Frame Index="1"><EyeSet Open="Closed" EnableBlink="1"/></Frame>')

    def xml(self, frames):
        self.path.write_text('<MeshList/><AnimationList><Animation Name="wave">'+frames+'</Animation></AnimationList>')

    def run_selection(self, **kwargs):
        base = select_native_view('Pony', self.obj, Objects(), model_stem='body', mesh_names=list(dict.fromkeys(self.names)),
            clips=self.clips, extracted_dir=self.root, csv_first_name='rest', csv_excluded_names=[])
        return prepare_interactions(base, 'Pony', self.obj, model_stem='body', mesh_names=self.names,
                                    clips=self.clips, **kwargs)

    def test_pipeline_duplicate_non_eye_names_preserve_original_lookup_noop(self):
        self.names = ['body', 'body']
        row = self.run_selection()
        self.assertEqual(row['visible_meshes'], ['body'])
        self.assertEqual(row['provenance']['eyes']['rule'], 'reviewed-single-mesh-eye-lookups-no-op')
        self.assertEqual(row['model_interactions']['eyes'], {state: [] for state in ['Open','Closed','Happy','Frown']})

    def test_duplicate_native_eye_meshes_have_original_indices(self):
        result = self.run_selection()
        self.assertEqual(result['model_interactions']['eyes']['Closed'], [2, 3])
        self.assertEqual(result['interactions']['clickAnimations'], ['gen_tap_north'])
        self.assertIn('eyes_shut', result['retained_meshes'])
        self.assertNotIn('eyes_shut', result['visible_meshes'])
        self.assertEqual(result['animation_events']['wave'], [
            {'frame': 1, 'type': 'EyeSet', 'state': 'Closed', 'enableBlink': True}])
        self.assertIn('wave', result['clip_names'])

    def test_conversation_overrides_native_random_pool(self):
        self.obj['Conversation'] = {'IdleAnimation': 'rest', 'InteractionAnimation': 'wave'}
        self.assertEqual(self.run_selection()['interactions']['clickAnimations'], ['wave'])
        self.obj['Conversation']['InteractionAnimation'] = ''
        self.assertEqual(self.run_selection()['interactions']['clickAnimations'], [])

    def test_exact_special_ai_branches_not_prefix_guesses(self):
        self.assertIn('granny_tap_east', native_click_names({'AI': {'Special_AI': 3}}))
        self.assertIn('gen_interact_02_med', native_click_names({'AI': {'Special_AI': 2}}))
        self.assertIn('gen_upright_interact_bow', native_click_names({'AI': {'Special_AI': 4}}))
        with self.assertRaises(InteractionError): native_click_names({'AI': {'Special_AI': 99}})

    def test_unknown_ai_keeps_verified_eyes_but_no_click(self):
        self.obj['AI']['Special_AI'] = 99
        row = self.run_selection()
        self.assertEqual(row['interactions']['clickAnimations'], [])
        self.assertEqual(row['model_interactions']['eyes']['Open'], [1])
        self.assertTrue(any(d['code'] == 'click-unavailable' for d in row['provenance']['interactions']['diagnostics']))

    def test_duplicate_callbacks_are_not_silently_collapsed(self):
        self.xml('<Frame Index="1"><EyeSet Open="Closed" EnableBlink="1"/><EyeSet Open="Closed" EnableBlink="1"/></Frame>')
        with self.assertRaisesRegex(InteractionError, 'Duplicate native event'):
            self.run_selection()
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(NativeInputError, 'Duplicate native event'):
            _verify_event_source(self.path, False, {'wave'}, {digest: ['wave']})

    def test_unsupported_event_excludes_whole_clip_including_supported_events(self):
        self.xml('<Frame Index="1"><EyeSet Open="Closed" EnableBlink="1"/><Sound Name="test"/></Frame>')
        row = self.run_selection()
        self.assertNotIn('wave', row['clip_names'])
        diagnostic = next(d for d in row['provenance']['interactions']['diagnostics'] if d.get('name') == 'wave')
        self.assertEqual(diagnostic['reason'], 'unsupported-native-event:Sound')
        self.assertEqual(len(diagnostic['events']), 2)

    def test_out_of_range_event_is_explicitly_excluded(self):
        for index in [5, 6]:
            self.xml(f'<Frame Index="{index}"><EyeSet Open="Closed" EnableBlink="1"/></Frame>')
            row = self.run_selection()
            self.assertNotIn('wave', row['clip_names'])
            self.assertIn('native-event-frame-outside-clip', str(row['provenance']['interactions']))

    def test_conflicting_eye_events_reject_instead_of_last_write_wins(self):
        self.xml('<Frame Index="1"><EyeSet Open="Closed" EnableBlink="1"/><EyeSet Open="Open" EnableBlink="0"/></Frame>')
        row = self.run_selection()
        self.assertNotIn('wave', row['clip_names'])
        self.assertIn('conflicting-native-event', str(row['provenance']['interactions']))

    def test_missing_state_never_uses_another_expression(self):
        self.names.remove('eyes_frown')
        self.xml('<Frame Index="1"><EyeSet Open="Frown" EnableBlink="1"/></Frame>')
        row = self.run_selection()
        self.assertNotIn('wave', row['clip_names'])
        self.assertIn('missing-native-eye-state:Frown', str(row['provenance']['interactions']))

    def test_native_empty_open_keeps_eye_event_as_noop_without_geometry(self):
        self.obj['Eyes'] = {'EyeSubObject_'+state: [] for state in ['Open', 'Closed', 'Happy', 'Frown']}
        row = self.run_selection()
        self.assertEqual(row['model_interactions']['eyes'], {state: [] for state in ['Open', 'Closed', 'Happy', 'Frown']})
        self.assertEqual(row['animation_events']['wave'][0]['state'], 'Closed')
        self.assertEqual(row['retained_meshes'], row['visible_meshes'])

    def test_eye_none_is_preserved_as_explicit_clear(self):
        self.xml('<Frame Index="0"><EyeSet Open="None" EnableBlink="0"/></Frame>')
        self.assertEqual(self.run_selection()['animation_events']['wave'][0]['state'], 'None')

    def test_dizzy_missing_emitter_dependency_does_not_publish_bare_clip(self):
        self.path.write_text('<MeshList/><AnimationList><Animation Name="arena_pony_defeat">'
            '<Frame Index="1"><Emitter Particle="mlp_arena_pony_defeat_02"/></Frame></Animation></AnimationList>')
        def missing(attrs, frame): raise ValueError('Missing native texture')
        row = self.run_selection(effect_resolver=missing)
        self.assertNotIn('dizzyAnimation', row['interactions'])
        self.assertNotIn('arena_pony_defeat', row['clip_names'])
        self.assertIn('Missing native texture', str(row['provenance']['interactions']))

    def test_emitter_and_eye_order_are_preserved(self):
        self.xml('<Frame Index="1"><Emitter Particle="mlp_arena_pony_defeat_02"/><EyeSet Open="Happy" EnableBlink="0"/></Frame>')
        row = self.run_selection(effect_resolver=lambda attrs, frame: {'frame': frame, 'type': 'Emitter', 'effect': attrs['Particle']})
        self.assertEqual([e['type'] for e in row['animation_events']['wave']], ['Emitter', 'EyeSet'])

    def test_converter_approval_is_bound_to_current_xml_and_supported_types(self):
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(NativeInputError): _verify_event_source(self.path, False, {'wave'})
        _verify_event_source(self.path, False, {'wave'}, {digest: ['wave']})
        self.xml('<Frame Index="1"><EyeSet Open="Happy" EnableBlink="1"/></Frame>')
        with self.assertRaises(NativeInputError): _verify_event_source(self.path, False, {'wave'}, {digest: ['wave']})
        self.xml('<Frame Index="1"><Sound Name="test"/></Frame>')
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(NativeInputError): _verify_event_source(self.path, False, {'wave'}, {digest: ['wave']})


class NormalPipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.raw = self.root/'raw'; self.dist = self.root/'dist'
        write_fixture(self.raw); self.objects = write_native_tables(self.raw)
        self.names = ['body', 'eyes_open', 'eyes_shut', 'eyes_shut', 'eyes_happy', 'eyes_frown']
        data = (self.raw/'flag.rk').read_bytes()
        entries, sections = [], []
        for i in range(24):
            tag, offset, count, size = struct.unpack_from('<4I', data, 80+i*16)
            if tag: sections.append((tag, count, data[offset:offset+size]))
        offset, body = 80+24*16, bytearray()
        for tag, count, raw in sections:
            if tag == 1:
                count, raw = len(self.names), b''.join(struct.pack('<4I', 4, i*12, 0, 0) for i in range(len(self.names)))
            elif tag == 4: count, raw = count*len(self.names), raw*len(self.names)
            elif tag == 16: count, raw = len(self.names), b''.join(name.encode().ljust(64, b'\0') for name in self.names)
            entries.append(struct.pack('<4I', tag, offset, count, len(raw)))
            body.extend(raw); offset += len(raw)
        (self.raw/'flag.rk').write_bytes(data[:80]+b''.join(entries).ljust(24*16, b'\0')+body)
        obj = self.objects['Pony']['Synthetic_Flag']
        obj['Eyes'] = {'EyeSubObject_'+state: [name] for state, name in zip(
            ['Open','Closed','Happy','Frown'], ['eyes_open','eyes_shut','eyes_happy','eyes_frown'])}
        obj['Conversation']['InteractionAnimation'] = 'wave'
        (self.raw/'flag.xml').write_text('<MeshList/><AnimationList><Animation Name="wave">'
            '<Frame Index="1"><EyeSet Open="Happy" EnableBlink="0"/></Frame></Animation></AnimationList>')

    def test_normal_generation_retains_hidden_meshes_and_source_events(self):
        self.assertEqual(_model(self.raw/'flag.rk')['meshes'], self.names)
        report = generate_models(self.objects, self.raw, self.dist, self.root/'report.json', campaign={})
        catalogue = json.loads((self.dist/'models_3d.json').read_text())
        self.assertEqual(catalogue['schemaVersion'], 3)
        model = catalogue['models']['Synthetic_Flag']
        self.assertEqual(model['path'], 'models/3d/Synthetic_Flag.glb')
        self.assertEqual(model['interactions'], {'version': 1, 'clickAnimations': ['wave']})
        doc = glb_json((self.dist/model['path']).read_bytes())
        hidden = doc['extras']['atpInteractions']['hiddenNodes']
        self.assertEqual(len(hidden), 4)
        self.assertEqual(len(doc['scenes']), 1)
        self.assertFalse(set(hidden) & set(doc['scenes'][0]['nodes']))
        self.assertTrue(all(not doc['nodes'][i]['extras']['atpDefaultVisible'] for i in hidden))
        self.assertEqual(doc['extras']['atpInteractions']['eyes']['Closed'], [2, 3])
        wave = next(row for row in model['animations'] if row['id'] == 'wave')
        clip = glb_json((self.dist/wave['path']).read_bytes())['extras']['atpClip']
        self.assertEqual(clip['eventProfile'], 'ponybook-v1')
        self.assertEqual(clip['events'], [{'frame': 1, 'type': 'EyeSet', 'state': 'Happy', 'enableBlink': False}])
        self.assertTrue(report['identities']['Synthetic_Flag']['converted'])

    def test_normal_generation_keeps_explicit_empty_eye_noop(self):
        self.objects['Pony']['Synthetic_Flag']['Eyes'] = {
            'EyeSubObject_'+state: [] for state in ['Open','Closed','Happy','Frown']}
        generate_models(self.objects, self.raw, self.dist, self.root/'report.json', campaign={})
        row = json.loads((self.dist/'models_3d.json').read_text())['models']['Synthetic_Flag']
        doc = glb_json((self.dist/row['path']).read_bytes())
        self.assertEqual(doc['extras']['atpInteractions']['eyes'], {
            state: [] for state in ['Open','Closed','Happy','Frown']})
        wave = next(clip for clip in row['animations'] if clip['id'] == 'wave')
        self.assertEqual(len(glb_json((self.dist/wave['path']).read_bytes())['extras']['atpClip']['events']), 1)

    def test_converter_rejects_missing_or_changed_source_events(self):
        from unittest.mock import patch
        from game_data.model_gen import convert_native
        captured = []
        def record(*args, **kwargs):
            captured.append((args, kwargs))
            return convert_native(*args, **kwargs)
        with patch('game_data.model_gen.convert_native', side_effect=record):
            generate_models(self.objects, self.raw, self.dist, self.root/'report.json', campaign={})
        args, kwargs = captured[0]
        for changed in [{}, {'wave': []}, {'wave': [{'frame': 1, 'type': 'EyeSet', 'state': 'Closed', 'enableBlink': False}]}]:
            with self.subTest(events=changed), self.assertRaisesRegex(NativeInputError, 'Compiled events differ'):
                convert_native(*args, **{**kwargs, 'animation_events': changed})
        invalid = copy.deepcopy(kwargs['model_interactions'])
        invalid['eyes']['Closed'] = [9999]
        with self.assertRaisesRegex(NativeInputError, 'eye index'):
            convert_native(*args, **{**kwargs, 'model_interactions': invalid})
        with self.assertRaisesRegex(NativeInputError, 'interaction animation identity'):
            convert_native(*args, **{**kwargs, 'interactions': {'version': 1, 'clickAnimations': ['unrelated']}})

    def test_optional_interaction_conversion_failure_preserves_static_model(self):
        from unittest.mock import patch
        from game_data.model_gen import convert_native
        def injected(*args, **kwargs):
            if kwargs.get('interactions'): raise NativeInputError('Injected hidden-eye material failure')
            return convert_native(*args, **kwargs)
        with patch('game_data.model_gen.prepare_interactions', side_effect=lambda selection, *a, **k: selection):
            generate_models(self.objects, self.raw, self.dist, self.root/'baseline-report.json', campaign={})
        baseline_catalogue = (self.dist/'models_3d.json').read_bytes()
        baseline_files = {p.relative_to(self.dist): p.read_bytes() for p in (self.dist/'models').rglob('*.glb')}
        with patch('game_data.model_gen.convert_native', side_effect=injected):
            report = generate_models(self.objects, self.raw, self.dist, self.root/'report.json', campaign={})
        model = json.loads((self.dist/'models_3d.json').read_text())['models']['Synthetic_Flag']
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), baseline_catalogue)
        for path, original in baseline_files.items():
            self.assertEqual((self.dist/path).read_bytes(), original)
        self.assertNotIn('interactions', model)
        self.assertNotIn('wave', [row['id'] for row in model['animations']])
        self.assertIn('interaction-assets-unavailable', str(report['identities']['Synthetic_Flag']['selection']))


if __name__ == '__main__': unittest.main()
