"""Malformed native data is rejected before narrowing or indexing arrays."""
import unittest

import numpy as np

from game_data.model_gen.converter import ConversionError, Rig, model_glb
from .test_converter import fixture, unpack
from .test_export import accessor


class BoundaryTests(unittest.TestCase):
    def test_duplicate_joint_weights_preserve_linear_blend_and_source(self):
        model, clip = fixture()
        model.joints = np.array([[1, 1, 0, 0], [1, 1, 1, 0], [1, 1, 1, 1], [0, 1, 0, 1]])
        model.weights = np.array([[.25, .75, 0, 0], [.25, .25, .5, 0], [.125, .125, .25, .5], [.1, .2, .3, .4]])
        original_joints, original_weights = model.joints.copy(), model.weights.copy()
        document, binary = unpack(model_glb(model))
        attrs = document['meshes'][0]['primitives'][0]['attributes']
        joints = accessor(document, binary, attrs['JOINTS_0'])
        weights = accessor(document, binary, attrs['WEIGHTS_0'])
        np.testing.assert_array_equal(joints, [[1, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0], [0, 1, 0, 0]])
        np.testing.assert_allclose(weights, [[1, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0], [.4, .6, 0, 0]], atol=3e-8)
        vertices = np.c_[model.positions, np.ones(len(model.positions))]
        inverse = np.linalg.inv(model.bind_global)
        for pose in clip.global_frames:
            transforms = pose @ inverse
            expected = sum(original_weights[:, slot, None] * np.einsum('nij,nj->ni', transforms[original_joints[:, slot]], vertices)
                           for slot in range(4))
            actual = sum(weights[:, slot, None] * np.einsum('nij,nj->ni', transforms[joints[:, slot]], vertices)
                         for slot in range(4))
            np.testing.assert_allclose(actual, expected, rtol=0, atol=2e-7)
        np.testing.assert_array_equal(model.joints, original_joints)
        np.testing.assert_array_equal(model.weights, original_weights)

    def test_bad_index_and_material_types_are_not_coerced(self):
        for index in (-1, 2.5, 4, float('nan')):
            model, _ = fixture()
            model.primitives[0]['indices'] = np.array([[0, 1, index]])
            with self.subTest(index=index), self.assertRaisesRegex(ConversionError, 'triangle indices'):
                model_glb(model)
        for material in (.5, False):
            model, _ = fixture()
            model.primitives[0]['material'] = material
            with self.subTest(material=material), self.assertRaisesRegex(ConversionError, 'primitive material'):
                model_glb(model)

    def test_joint_indices_cannot_overflow_unsigned_short(self):
        model, _ = fixture()
        count = 65537
        model.rig = Rig('a' * 64, [-1] * count, ['joint'] * count)
        model.bind_global = np.broadcast_to(np.eye(4), (count, 4, 4)).copy()
        model.rest_global = model.bind_global.copy()
        model.joints[0, 0] = count - 1
        with self.assertRaisesRegex(ConversionError, 'joint index range'):
            model_glb(model)

    def test_noninvertible_bind_and_cyclic_rig_are_rejected(self):
        model, _ = fixture()
        model.bind_global[1, 0] = 0
        with self.assertRaisesRegex(ConversionError, 'bind matrices'):
            model_glb(model)
        model, _ = fixture()
        model.rig.parents[0] = 1
        with self.assertRaisesRegex(ConversionError, 'parent index/cycle'):
            model_glb(model)


if __name__ == '__main__':
    unittest.main()
