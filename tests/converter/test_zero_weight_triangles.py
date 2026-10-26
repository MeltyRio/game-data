"""Native material behavior from self-authored resources, without shader files."""
from __future__ import annotations

import copy
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from game_data.model_gen.converter import from_native
from game_data.model_gen.converter.export import convert_native
from game_data.model_gen.converter.native import NativeInputError, load_native
from converter.test_native_adapter import fixture
from converter.test_converter import unpack


def add_triangles(folder, *, separate=False, weight=0):
    """Append a four-vertex/two-triangle square to the original moving triangle."""
    path = folder / 'tiny.rk'
    raw = path.read_bytes()
    parts = {}
    for i in range(24):
        tag, start, count, size = struct.unpack_from('<4I', raw, 80 + 16 * i)
        if tag:
            parts[tag] = [count, raw[start:start + size]]
    parts[3][0] += 4
    parts[3][1] += b''.join(struct.pack('<5f', *v) for v in
                           [(3, 3, 3, 0, 0), (4, 3, 3, 1, 0),
                            (4, 4, 3, 1, 1), (3, 4, 3, 0, 1)])
    parts[17][0] += 4
    parts[17][1] += struct.pack('<4B4H', 0, 0, 0, 0, weight, 0, 0, 0) * 4
    parts[4] = [9, struct.pack('<9H', 0, 1, 2, 3, 4, 5, 3, 5, 6)]
    if separate:
        parts[1] = [2, struct.pack('<8I', 1, 0, 0, 0, 2, 3, 1, 0)]
        parts[2] = [2, b'body'.ljust(64, b'\0') + b'degenerate'.ljust(64, b'\0')]
        parts[16] = [2, b'body'.ljust(64, b'\0') + b'degenerate'.ljust(64, b'\0')]
        (folder / 'degenerate.rkm').write_bytes((folder / 'body.rkm').read_bytes())
    else:
        parts[1] = [1, struct.pack('<4I', 3, 0, 0, 0)]
    offset = 80 + 24 * 16
    entries, body = [], bytearray()
    for tag, (count, data) in parts.items():
        entries.append(struct.pack('<4I', tag, offset, count, len(data)))
        body.extend(data)
        offset += len(data)
    path.write_bytes(raw[:80] + b''.join(entries).ljust(24 * 16, b'\0') + body)


class ZeroWeightTriangleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        fixture(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def kwargs(self, visible=('body',)):
        return dict(visible_meshes=visible, rest_name='rest', event_path=self.folder / 'tiny.xml')

    def load(self, visible=('body',)):
        return load_native(self.folder / 'tiny.rk', self.folder / 'tiny.anim',
                           self.folder / 'tiny.csv', ['move'], **self.kwargs(visible))

    def bundle(self, visible=('body',)):
        return convert_native(self.folder / 'tiny.rk', self.folder / 'tiny.anim',
                              self.folder / 'tiny.csv', ['move'], identity='SyntheticNative',
                              **self.kwargs(visible))

    def test_native_degenerate_triangles_preserve_renderable_glbs_and_rig(self):
        baseline = self.bundle()
        add_triangles(self.folder)
        native = self.load()
        evidence = native['provenance']['zero_weight_triangles']
        self.assertEqual(evidence['removed_triangle_count'], 2)
        self.assertEqual(evidence['removed'][0]['triangle_ordinals'], [1, 2])
        self.assertEqual(evidence['removed'][0]['indices'], [[3, 4, 5], [3, 5, 6]])
        self.assertFalse(evidence['removed'][0]['primitive_removed'])
        np.testing.assert_array_equal(native['primitives'][0]['indices'], [[0, 1, 2]])
        model, _ = from_native(native, identity='SyntheticNative')
        self.assertEqual(len(model.positions), 3)
        current = self.bundle()
        self.assertEqual(current.files, baseline.files)
        self.assertEqual(current.catalogue, baseline.catalogue)

    def test_pruned_eye_targets_preserve_events_and_source_blink_capability(self):
        add_triangles(self.folder, separate=True)
        path = self.folder / 'tiny.xml'
        path.write_text('<MeshList/><AnimationList><Animation Name="move">'
            '<Frame Index="1"><EyeSet Open="Closed" EnableBlink="1"/></Frame>'
            '<Frame Index="2"><EyeSet Open="Open" EnableBlink="1"/></Frame>'
            '</Animation></AnimationList>')
        profile = {'version': 1, 'blink': True,
                   'eyes': {'Open': [0], 'Closed': [1], 'Happy': [], 'Frown': []}}
        original = copy.deepcopy(profile)
        events = [dict(frame=frame, type='EyeSet', state=state, enableBlink=True)
                  for frame, state in [(1, 'Closed'), (2, 'Open')]]
        options = dict(identity='SyntheticNative', **self.kwargs(),
            retained_meshes=['body', 'degenerate'], model_interactions=profile,
            interactions={'version': 1, 'clickAnimations': ['move']},
            animation_events={'move': events}, event_approvals={path.resolve(): ['move']})
        def convert(**changes):
            return convert_native(self.folder / 'tiny.rk', self.folder / 'tiny.anim',
                                  self.folder / 'tiny.csv', ['move'], **{**options, **changes})
        bundle = convert()
        row = bundle.catalogue['models']['SyntheticNative']
        document, _ = unpack(bundle.files[row['path']])
        self.assertEqual(document['extras']['atpInteractions']['eyes'],
                         {'Open': [0], 'Closed': [], 'Happy': [], 'Frown': []})
        self.assertTrue(document['extras']['atpInteractions']['blink'])
        self.assertEqual([mesh['name'] for mesh in document['meshes']], ['body'])
        binding = row['animations'][0]['eventBinding']
        self.assertEqual(binding['events'], events)
        self.assertEqual(binding['visibility'], [
            {'meshName': 'body', 'times': [0, 1/15, 2/15], 'values': [True, False, True]}])
        self.assertEqual(row['interactions']['clickAnimations'], ['move'])
        self.assertEqual(profile, original, 'projection must not alter source-state evidence')
        removed = bundle.provenance['zero_weight_triangles']['removed'][0]
        self.assertEqual(removed['native_mesh_index'], 1)
        self.assertTrue(removed['primitive_removed'])
        for invalid in [99, True]:
            bad = copy.deepcopy(profile)
            bad['eyes']['Closed'] = [invalid]
            with self.subTest(invalid=invalid), self.assertRaisesRegex(NativeInputError, 'eye index'):
                convert(model_interactions=bad)
        with self.assertRaisesRegex(NativeInputError, 'eye index'):
            convert(retained_meshes=['body'])
        bad = copy.deepcopy(profile)
        bad['eyes']['Closed'] = []
        with self.assertRaisesRegex(NativeInputError, 'eye state is absent'):
            convert(model_interactions=bad)
