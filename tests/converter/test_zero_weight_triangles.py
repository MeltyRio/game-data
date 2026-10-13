"""Self-authored geometry; shader digest mocks contain no game shader bytes."""
from __future__ import annotations

import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from game_data.model_gen.converter import ConversionError, from_native
from game_data.model_gen.converter.export import convert_native
from game_data.model_gen.converter.native import NativeInputError, load_native
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
        # Only mock the reviewed fingerprints for synthetic files. The loader
        # still performs real filesystem reads and SHA-256 comparisons.
        self.digests = {}
        for name in ('rkshader.rkvs', 'rkshadercore.rkvs', 'rkshader.rkps'):
            data = ('synthetic shader source: ' + name).encode()
            (self.folder / name).write_bytes(data)
            self.digests[name] = hashlib.sha256(data).hexdigest()
        self.mock = patch('game_data.model_gen.converter.native._ZERO_WEIGHT_SHADER_SOURCES', self.digests)
        self.mock.start()

    def tearDown(self):
        self.mock.stop()
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
        self.assertEqual(evidence['shader_sources'], self.digests)
        self.assertTrue(set(self.digests).issubset({p['name'] for p in native['provenance']['files']}))

    def test_zero_only_primitive_and_unused_material_are_omitted(self):
        baseline = self.bundle()
        add_triangles(self.folder, separate=True)
        native = self.load(('body', 'degenerate'))
        self.assertEqual([p['name'] for p in native['primitives']], ['body'])
        self.assertEqual([m['name'] for m in native['materials']], ['body'])
        self.assertTrue(native['provenance']['zero_weight_triangles']['removed'][0]['primitive_removed'])
        self.assertEqual(self.bundle(('body', 'degenerate')).files, baseline.files)

    def test_hidden_zero_vertices_do_not_require_shader_sources(self):
        baseline = self.bundle()
        add_triangles(self.folder, separate=True)
        for name in self.digests:
            (self.folder / name).unlink()
        current = self.bundle()
        self.assertEqual(current.files, baseline.files)
        self.assertNotIn('zero_weight_triangles', current.provenance)
        self.assertFalse(set(self.digests) & {p['name'] for p in current.provenance['files']})

    def test_zero_only_model_is_rejected(self):
        add_triangles(self.folder, separate=True)
        with self.assertRaisesRegex(NativeInputError, 'no renderable model geometry'):
            self.load(('degenerate',))

    def test_missing_or_changed_shader_source_is_rejected(self):
        add_triangles(self.folder)
        for name in self.digests:
            with self.subTest(name=name):
                path = self.folder / name
                original = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(NativeInputError, 'Missing native dependency'):
                    self.load()
                path.write_bytes(original + b'changed')
                with self.assertRaisesRegex(NativeInputError, 'Unreviewed native zero-weight shader'):
                    self.load()
                path.write_bytes(original)

    def test_nonzero_weight_residue_is_never_deleted(self):
        add_triangles(self.folder, weight=1)
        native = self.load()
        self.assertEqual(len(native['primitives'][0]['indices']), 3)
        self.assertNotIn('zero_weight_triangles', native['provenance'])
        with self.assertRaisesRegex(ConversionError, 'not normalized'):
            from_native(native, identity='SyntheticNative')

    def test_special_shader_is_not_authorized_by_degenerate_geometry(self):
        add_triangles(self.folder)
        path = self.folder / 'body.rkm'
        path.write_text(path.read_text().replace('rkshader', 'rkshader_horn'))
        with self.assertRaisesRegex(NativeInputError, 'Missing native dependency'):
            self.load()

    def test_mixed_triangle_is_rejected_without_using_known_shader(self):
        add_triangles(self.folder)
        path = self.folder / 'tiny.rk'
        raw = bytearray(path.read_bytes())
        for i in range(24):
            tag, start, _, _ = struct.unpack_from('<4I', raw, 80 + i * 16)
            if tag == 4:
                struct.pack_into('<H', raw, start + 3 * 2, 0)
        path.write_bytes(raw)
        with self.assertRaisesRegex(NativeInputError, 'Zero-weight vertices in mixed triangles'):
            self.load()
