"""Native interpolation payloads use only self-authored mathematical poses."""
import unittest

import numpy as np

from game_data.model_gen.converter import Clip, ConversionError, animation_glb
from game_data.model_gen.converter.core import clip_from_native
from .test_converter import fixture, unpack
from .test_export import accessor


def native_clip():
    model, _ = fixture()
    translations = np.array([[[0., 0, 0], [2, 0, 0]],
                             [[1., 0, 0], [2, 1, 0]]])
    # Different non-unit lengths and a negative hemisphere are deliberate.
    rotations = np.array([[[0., 0, 0, .75], [0, 0, 0, -.8]],
                          [[0., 0, 1.2, 0], [0, 0, -.6, -.6]]])
    return clip_from_native({'name': 'native', 'translations': translations,
                             'rotations_xyzw': rotations, 'fps': 10}, model.rig)


class NativeSamplingTests(unittest.TestCase):
    def test_raw_global_layout_and_quaternion_lengths_survive(self):
        clip = native_clip()
        document, binary = unpack(animation_glb(clip))
        sampling = document['extras']['atpClip']['nativeSampling']
        self.assertEqual(set(sampling), {'version', 'translations', 'rotations'})
        self.assertEqual(sampling['version'], 1)
        for key, source, width in [('translations', clip.native_translations, 3),
                                   ('rotations', clip.native_rotations_xyzw, 4)]:
            definition = document['accessors'][sampling[key]]
            self.assertEqual((definition['componentType'], definition['type'], definition['count']),
                             (5126, f'VEC{width}', 4))
            values = accessor(document, binary, sampling[key]).reshape(2, 2, width)
            np.testing.assert_array_equal(values, source.astype('<f4'))
        q = accessor(document, binary, sampling['rotations'])
        self.assertLess(q[1, 3], 0)
        self.assertAlmostEqual(np.linalg.norm(q[0]), .75)
        self.assertAlmostEqual(np.linalg.norm(q[2]), 1.2)

    def test_standard_tracks_rig_and_duration_are_unchanged(self):
        clip = native_clip()
        legacy = Clip(clip.name, clip.rig, clip.global_frames, clip.fps)
        old, old_binary = unpack(animation_glb(legacy))
        new, new_binary = unpack(animation_glb(clip))
        self.assertNotIn('nativeSampling', old['extras']['atpClip'])
        self.assertEqual(old['animations'], new['animations'])
        self.assertEqual(old['nodes'], new['nodes'])
        self.assertEqual(old['extras']['atpRig'], new['extras']['atpRig'])
        self.assertEqual(old['extras']['atpClip']['duration'], new['extras']['atpClip']['duration'])
        for i in range(len(old['accessors'])):
            np.testing.assert_array_equal(accessor(old, old_binary, i), accessor(new, new_binary, i))


    def test_raw_pose_must_match_existing_sampled_pose(self):
        for attribute, index, value in [('native_translations', (0, 1, 0), 8.),
                                       ('native_rotations_xyzw', (0, 1, 0), .5)]:
            with self.subTest(attribute=attribute):
                clip = native_clip()
                getattr(clip, attribute)[index] = value
                with self.assertRaisesRegex(ConversionError, 'differ from animation frames'):
                    animation_glb(clip)


if __name__ == '__main__':
    unittest.main()
