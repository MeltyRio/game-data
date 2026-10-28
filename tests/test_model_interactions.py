"""Synthetic native controls and normal generation; no game assets required."""
import copy
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from bootstrap import ROOT
from native_fixture import write_fixture, write_native_tables
from game_data.model_gen.interactions import InteractionError, prepare_interactions
from game_data.model_gen.selection import select_native_view
from game_data.model_gen import generate_models
from game_data.model_gen.converter.native import NativeInputError, _verify_event_source


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


    def prepare_event_paths(self, paths, clips=None, **kwargs):
        clips = self.clips if clips is None else clips
        base = {'clip_names': [clip['name'] for clip in clips], 'excluded_clips': [],
                'visible_meshes': ['body'], 'provenance': {},
                'event_path': paths[0], 'additional_event_paths': paths[1:]}
        return prepare_interactions(base, 'Pony', self.obj, model_stem='body',
                                    mesh_names=self.names, clips=clips, **kwargs)

    def test_event_paths_resolve_once_per_call_and_preserve_alias_approvals(self):
        other = self.root/'other.xml'
        other.write_text('<MeshList/><AnimationList/>')
        alias = self.root/'alias.xml'
        alias.symlink_to(self.path.name)
        names = [clip['name'] for clip in self.clips]
        resolve = Path.resolve
        for second in (other, alias):
            with self.subTest(second=second.name):
                expected = {}
                for name in names:
                    for path in (self.path, second):
                        expected.setdefault(path.resolve(), []).append(name)
                with patch.object(Path, 'resolve', autospec=True, side_effect=resolve) as resolutions:
                    first = self.prepare_event_paths([self.path, second, self.path, second])
                    self.assertEqual(resolutions.call_count, 2)
                    again = self.prepare_event_paths([self.path, second, self.path, second])
                    self.assertEqual(resolutions.call_count, 4)
                self.assertEqual(first, again)
                self.assertEqual(first['clip_names'], names)
                self.assertEqual(first['event_approvals'], expected)
                self.assertEqual(first['animation_events']['wave'], [
                    {'frame': 1, 'type': 'EyeSet', 'state': 'Closed', 'enableBlink': True}])

    def test_event_paths_are_not_resolved_without_approved_clips(self):
        self.xml('<Frame Index="1"><Sound Name="test"/></Frame>')
        for clips in ([], [self.clips[1]]):
            with self.subTest(clips=clips):
                with patch.object(Path, 'resolve', side_effect=AssertionError('Unexpected resolution')) as resolutions:
                    result = self.prepare_event_paths([self.path], clips)
                resolutions.assert_not_called()
                self.assertEqual(result['clip_names'], [])
                self.assertEqual(result['excluded_clips'], [
                    {'name': 'wave', 'reason': 'unsupported-native-event:Sound'}] if clips else [])

    def test_missing_event_file_still_fails_before_resolution(self):
        missing = self.root/'missing.xml'
        for clips in ([], self.clips):
            with self.subTest(clips=clips):
                with patch.object(Path, 'resolve', side_effect=AssertionError('Unexpected resolution')) as resolutions:
                    with self.assertRaises(FileNotFoundError) as raised:
                        self.prepare_event_paths([missing], clips)
                self.assertEqual(raised.exception.filename, str(missing))
                resolutions.assert_not_called()

    def test_event_path_resolution_errors_propagate_after_event_compilation(self):
        self.xml('<Frame Index="1"><Emitter Particle="test"/></Frame>')
        for error in (OSError('Cannot resolve path'), RuntimeError('Cannot resolve path')):
            with self.subTest(error=type(error).__name__):
                calls = []
                def effect(attrs, frame):
                    calls.append('event')
                    return {'frame': frame, 'type': 'Emitter', 'effect': attrs['Particle']}
                def fail(path):
                    calls.append('resolve')
                    raise error
                with patch.object(Path, 'resolve', autospec=True, side_effect=fail):
                    with self.assertRaises(type(error)) as raised:
                        self.prepare_event_paths([self.path], [self.clips[1]], effect_resolver=effect)
                self.assertIs(raised.exception, error)
                self.assertEqual(calls, ['event', 'resolve'])

    def test_duplicate_native_eye_meshes_have_original_indices(self):
        result = self.run_selection()
        self.assertEqual(result['model_interactions']['eyes']['Closed'], [2, 3])
        self.assertTrue(result['model_interactions']['blink'])
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


    def test_unsupported_event_excludes_whole_clip_including_supported_events(self):
        self.xml('<Frame Index="1"><EyeSet Open="Closed" EnableBlink="1"/><Sound Name="test"/></Frame>')
        row = self.run_selection()
        self.assertNotIn('wave', row['clip_names'])
        diagnostic = next(d for d in row['provenance']['interactions']['diagnostics'] if d.get('name') == 'wave')
        self.assertEqual(diagnostic['reason'], 'unsupported-native-event:Sound')
        self.assertEqual(len(diagnostic['events']), 2)


    def test_dizzy_missing_emitter_dependency_does_not_publish_bare_clip(self):
        self.path.write_text('<MeshList/><AnimationList><Animation Name="arena_pony_defeat">'
            '<Frame Index="1"><Emitter Particle="mlp_arena_pony_defeat_02"/></Frame></Animation></AnimationList>')
        def missing(attrs, frame): raise ValueError('Missing native texture')
        row = self.run_selection(effect_resolver=missing)
        self.assertNotIn('dizzyAnimation', row['interactions'])
        self.assertNotIn('arena_pony_defeat', row['clip_names'])
        self.assertIn('Missing native texture', str(row['provenance']['interactions']))


    def subobjects(self, frames, declarations=None):
        declarations = declarations or '<body><SubObject ID="prop" Name="hat" DefaultVisible="0"/></body>'
        self.path.write_text('<MeshList>'+declarations+'</MeshList><AnimationList><Animation Name="wave">'
            + frames + '</Animation></AnimationList>')

    def test_subobject_uses_exact_model_id_and_all_matching_native_mesh_indices(self):
        self.names += ['hat', 'hat']
        self.subobjects('<Frame Index="0"><SubObject ID="prop" Show="1"/></Frame>'
            '<Frame Index="3"><SubObject ID="prop" Show="0"/></Frame>')
        result = self.run_selection()
        self.assertIn('wave', result['clip_names'])
        self.assertEqual(result['model_interactions']['subObjects'], [6, 7])
        self.assertEqual(result['animation_events']['wave'], [
            {'frame': 0, 'type': 'SubObject', 'meshes': [6, 7], 'visible': True},
            {'frame': 3, 'type': 'SubObject', 'meshes': [6, 7], 'visible': False}])
        self.assertIn('hat', result['retained_meshes'])
        self.assertNotIn('hat', result['visible_meshes'])
        read_bytes = Path.read_bytes
        with patch.object(Path, 'read_bytes', autospec=True, side_effect=read_bytes) as reads:
            verified, _ = _verify_event_source(self.path, False, {'wave'}, result['event_approvals'],
                                               model_stem='body', mesh_names=self.names)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(verified['approved_events']['wave'], result['animation_events']['wave'])

    def test_verified_events_merge_declarations_and_bound_targets_in_source_order(self):
        self.names += ['hat', 'hat']
        self.path.write_text('<MeshList><body>'
            '<SubObject ID="prop" Name="hat" DefaultVisible="0"/>'
            '<SubObject ID="alias" Name="hat" DefaultVisible="0"/>'
            '</body></MeshList><AnimationList><Animation Name="wave">'
            '<Frame Index="3"><EyeSet Open="None" EnableBlink="0"/></Frame>'
            '<Frame Index="0"><SubObject ID="prop" Show="1"/></Frame>'
            '</Animation><Animation Name="wave">'
            '<Frame Index="3"><EyeSet Open="None" EnableBlink="0"/></Frame>'
            '<Frame Index="0"><SubObject ID="alias" Show="1"/></Frame>'
            '<Frame Index="1"><Emitter Particle="mlp_arena_pony_defeat_02"/></Frame>'
            '</Animation><Animation Name="rest"/></AnimationList>')
        verified, path = _verify_event_source(self.path, False, {'wave', 'rest'},
            {self.path.resolve(): ['wave']}, model_stem='body', mesh_names=self.names)
        self.assertEqual(path, self.path.resolve())
        self.assertEqual(verified['approved_events'], {
            'wave': [
                {'frame': 3, 'type': 'EyeSet', 'state': 'None', 'enableBlink': False},
                {'frame': 0, 'type': 'SubObject', 'meshes': [6, 7], 'visible': True},
                {'frame': 1, 'type': 'Emitter', 'effect': 'mlp_arena_pony_defeat_02'}],
            'rest': []})

    def test_event_verification_preserves_approval_structure_and_support_boundaries(self):
        eye = '<EyeSet Open="Closed" EnableBlink="1"/>'
        frame = '<Frame Index="1">' + eye + '</Frame>'
        cases = [
            (frame, None, NativeInputError, 'requires unsupported native events'),
            (frame, [], NativeInputError, 'requires unsupported native events'),
            ('<Frame Index="0"/>', [], NativeInputError, 'requires unsupported native events'),
            ('<Frame Index="-1">' + eye + '</Frame>', ['wave'], InteractionError, 'frame'),
            ('<Frame Index="1">' + eye * 2 + '</Frame>', ['wave'], InteractionError, 'Duplicate'),
            ('<Frame Index="1"><EyeSet><Nested/></EyeSet></Frame>', ['wave'], InteractionError, 'event'),
            ('<Frame Index="1"><Sound Name="test"/></Frame>', ['wave'], NativeInputError, 'Unsupported approved'),
            ('<Frame Index="1"><EyeSet Open="Missing" EnableBlink="1"/></Frame>', ['wave'], NativeInputError, 'Unsupported approved'),
            ('<Frame Index="1"><Emitter Particle="missing"/></Frame>', ['wave'], NativeInputError, 'Unsupported approved'),
        ]
        for frames, approved, error, message in cases:
            with self.subTest(frames=frames, approved=approved):
                self.xml(frames)
                approvals = None if approved is None else {self.path.resolve(): approved}
                with self.assertRaisesRegex(error, message):
                    _verify_event_source(self.path, False, {'wave'}, approvals)

    def test_unselected_events_do_not_require_approval_or_supported_structure(self):
        self.xml('<Frame Index="invalid"><Sound Name="test"/></Frame>')
        verified, path = _verify_event_source(self.path, False, {'rest'})
        self.assertEqual(verified, {'status': 'parsed-source-xml', 'name': 'rig.xml',
                                    'checked_clips': ['rest']})
        self.assertEqual(path, self.path.resolve())

    def test_subobject_missing_model_id_mesh_or_conflicting_event_excludes_whole_clip(self):
        self.names += ['hat']
        cases = [
            ('<other><SubObject ID="prop" Name="hat" DefaultVisible="0"/></other>',
             '<Frame Index="0"><SubObject ID="prop" Show="1"/></Frame>'),
            ('<body><SubObject ID="prop" Name="absent" DefaultVisible="0"/></body>',
             '<Frame Index="0"><SubObject ID="prop" Show="1"/></Frame>'),
            ('<body><SubObject ID="prop" Name="hat" DefaultVisible="0"/></body>',
             '<Frame Index="0"><SubObject ID="missing" Show="1"/></Frame>'),
            ('<body><SubObject ID="prop" Name="hat" DefaultVisible="0"/></body>',
             '<Frame Index="0"><SubObject ID="prop" Show="1"/><SubObject ID="prop" Show="0"/></Frame>'),
        ]
        for declarations, frames in cases:
            with self.subTest(declarations=declarations, frames=frames):
                self.subobjects(frames, declarations)
                result = self.run_selection()
                self.assertNotIn('wave', result['clip_names'])
                self.assertTrue(any(row.get('name') == 'wave' for row in result['provenance']['interactions']['diagnostics']))

    def test_subobject_does_not_borrow_target_from_another_event_document(self):
        self.names += ['hat']
        self.subobjects('')
        other = self.root/'book.xml'
        other.write_text('<MeshList/><AnimationList><Animation Name="wave"><Frame Index="0">'
            '<SubObject ID="prop" Show="1"/></Frame></Animation></AnimationList>')
        base = select_native_view('Pony', self.obj, Objects(), model_stem='body', mesh_names=self.names,
            clips=self.clips, extracted_dir=self.root, csv_first_name='rest', csv_excluded_names=[])
        base['additional_event_paths'] = [other]
        result = prepare_interactions(base, 'Pony', self.obj, model_stem='body', mesh_names=self.names, clips=self.clips)
        self.assertNotIn('wave', result['clip_names'])
        self.assertIn('rest', result['clip_names'])
        self.assertTrue(any(row.get('name') == 'wave' and row.get('reason') == 'unresolved-native-subobject:prop'
            for row in result['provenance']['interactions']['diagnostics']))

    def test_all_valid_library_clips_are_considered_and_native_preferences_retained(self):
        self.clips += [{'name': 'library_only', 'start': 0, 'end': 4, 'fps': 15}]
        result = self.run_selection()
        self.assertIn('library_only', result['clip_names'])
        self.assertNotIn('library_only', result['preferred_animations'])
        self.assertIn('rest', result['preferred_animations'])
        self.assertIn('wave', result['preferred_animations'])

    def test_pet_library_expansion_does_not_add_pony_events_or_controllers(self):
        base = {'clip_names': ['rest'], 'excluded_clips': [], 'provenance': {}}
        result = prepare_interactions(base, 'Pet', {}, model_stem='body', mesh_names=['body'], clips=self.clips)
        self.assertEqual(result['clip_names'], ['rest', 'wave', 'gen_tap_north', 'gen_snap_01', 'arena_pony_defeat'])
        self.assertEqual(result['preferred_animations'], ['rest'])
        self.assertNotIn('interactions', result)


class NormalPipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.raw = self.root/'raw'; self.dist = self.root/'dist'
        write_fixture(self.raw); self.objects = write_native_tables(self.raw)
        self.names = ['body', 'eyes_open', 'eyes_shut', 'eyes_shut', 'eyes_happy', 'eyes_frown', 'hat']
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

    def catalogue(self):
        catalogue = json.loads((self.dist/'models_3d.json').read_text())
        for model in catalogue['models'].values():
            model['animations'] = [catalogue['animationAssets'][index] for index in model['animations']]
        return catalogue

    def test_normal_generation_retains_hidden_meshes_and_source_events(self):
        report = generate_models(self.objects, self.raw, self.dist, self.root/'report.json', campaign={})
        self.assertEqual(report['models']['flag.rk']['meshes'], self.names)
        catalogue = self.catalogue()
        self.assertEqual(catalogue['schemaVersion'], 4)
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
        self.assertNotIn('eventProfile', clip)
        self.assertNotIn('events', clip)
        self.assertEqual(wave['eventBinding'], {'version': 2, 'events': [
            {'frame': 1, 'type': 'EyeSet', 'state': 'Happy', 'enableBlink': False}],
            'visibility': [{'meshName': name, 'times': [0, 1 / clip['fps']],
                            'values': [name == 'eyes_open', name == 'eyes_happy']}
                           for name in self.names[1:6]]})
        self.assertTrue(report['identities']['Synthetic_Flag']['converted'])

    def test_normal_generation_keeps_explicit_empty_eye_noop(self):
        self.objects['Pony']['Synthetic_Flag']['Eyes'] = {
            'EyeSubObject_'+state: [] for state in ['Open','Closed','Happy','Frown']}
        generate_models(self.objects, self.raw, self.dist, self.root/'report.json', campaign={})
        row = self.catalogue()['models']['Synthetic_Flag']
        doc = glb_json((self.dist/row['path']).read_bytes())
        self.assertFalse(doc['extras']['atpInteractions']['blink'])
        self.assertEqual(doc['extras']['atpInteractions']['eyes'], {
            state: [] for state in ['Open','Closed','Happy','Frown']})
        wave = next(clip for clip in row['animations'] if clip['id'] == 'wave')
        self.assertNotIn('events', glb_json((self.dist/wave['path']).read_bytes())['extras']['atpClip'])
        self.assertEqual(len(wave['eventBinding']['events']), 1)
        self.assertEqual(wave['eventBinding']['visibility'], [])

    def test_normal_subobject_generation_retains_prop_and_binds_events_outside_shared_glb(self):
        (self.raw/'flag.xml').write_text('<MeshList><flag><SubObject ID="prop" Name="hat" DefaultVisible="0"/>'
            '</flag></MeshList><AnimationList><Animation Name="wave">'
            '<Frame Index="0"><SubObject ID="prop" Show="1"/></Frame>'
            '<Frame Index="1"><EyeSet Open="Happy" EnableBlink="0"/><SubObject ID="prop" Show="0"/></Frame>'
            '</Animation></AnimationList>')
        report = generate_models(self.objects, self.raw, self.dist, self.root/'report.json', campaign={})
        self.assertTrue(report['identities']['Synthetic_Flag']['converted'])
        model = self.catalogue()['models']['Synthetic_Flag']
        self.assertIn('wave', model['preferredAnimations'])
        doc = glb_json((self.dist/model['path']).read_bytes())
        profile = doc['extras']['atpInteractions']
        self.assertEqual(profile['subObjects'], [6])
        prop = next(i for i, node in enumerate(doc['nodes']) if node.get('extras', {}).get('atpNativeMeshIndex') == 6)
        self.assertIn(prop, profile['hiddenNodes'])
        self.assertNotIn(prop, doc['scenes'][0]['nodes'])
        self.assertFalse(doc['nodes'][prop]['extras']['atpDefaultVisible'])
        wave = next(row for row in model['animations'] if row['id'] == 'wave')
        self.assertEqual(wave['eventBinding']['events'], [
            {'frame': 1, 'type': 'EyeSet', 'state': 'Happy', 'enableBlink': False}])
        fps = glb_json((self.dist/wave['path']).read_bytes())['extras']['atpClip']['fps']
        self.assertEqual(next(curve for curve in wave['eventBinding']['visibility'] if curve['meshName'] == 'hat'),
                         {'meshName': 'hat', 'times': [0, 1 / fps], 'values': [True, False]})
        self.assertNotIn('events', glb_json((self.dist/wave['path']).read_bytes())['extras']['atpClip'])

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


if __name__ == '__main__': unittest.main()
