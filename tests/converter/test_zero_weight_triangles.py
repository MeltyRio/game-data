"""Native material behavior from self-authored resources, without shader files."""
from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from game_data.model_gen.converter import from_native
from game_data.model_gen.converter.export import convert_native
from game_data.model_gen.converter.native import load_native
from converter.test_native_adapter import fixture


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
