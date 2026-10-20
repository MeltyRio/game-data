"""Format and cache boundaries using self-created bytes, never game fixtures."""
import math
from pathlib import Path
import struct
import tempfile
import unittest
import numpy as np
from game_data.model_gen.converter.native_animation import read_animation


class AnimationFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'sample.anim'
    def tearDown(self):
        self.temp.cleanup()
    def write(self, kind, records, *, frames=2, bones=1):
        self.path.write_bytes(struct.pack('<8sII64sIII', b'RKFORMAT', 5, 2, b'synthetic', bones, frames, kind) + records)
    def test_float_type0_keeps_signed_position_rotation_and_frame_boundaries(self):
        s = math.sqrt(.5)
        self.write(0, struct.pack('<16f', 0, 0, s, s, -20, 8, 3, 1, 0, 1, 0, 0, 1000.25, -9.5, 6, 1))
        result = read_animation(self.path)
        np.testing.assert_array_equal(result.translations[:, 0], [[-20, 8, 3], [1000.25, -9.5, 6]])
        # Two distinct rotations, including W=0, must not swap position and quat.
        np.testing.assert_allclose(result.rotations_xyzw[:, 0], [[0, 0, s, s], [0, 1, 0, 0]], atol=3e-8)
        self.assertFalse(result.translations.flags.writeable)
    def test_type1_float_position_does_not_use_type4_fixed_point_scale(self):
        self.write(1, struct.pack('<4f3h', -32.75, 0.5, 1200.125, 0, 32767, 0, 0) + struct.pack('<4f3h', 5, -9, 1, -1, 0, 0, 0))
        result = read_animation(self.path)
        np.testing.assert_array_equal(result.translations[:, 0], [[-32.75, .5, 1200.125], [5, -9, 1]])
        np.testing.assert_array_equal(result.rotations_xyzw[:, 0], [[1, 0, 0, 0], [0, 0, 0, -1]])
    def test_type4_matches_pinned_public_reader_without_csv_side_effects(self):
        from luna_kit.model import Anim
        self.write(4, struct.pack('<3hh3b', -300, 200, 100, 23169, 0, 0, 89) + struct.pack('<3hh3b', 3, -7, 9, -32767, 0, 0, 0))
        raw = self.path.read_bytes(); frames = Anim(raw).get_frames(); result = read_animation(self.path)
        expected_t = [[[pose.position.x, pose.position.y, pose.position.z] for pose in frame] for frame in frames]
        expected_q = [[[pose.quaternion.x, pose.quaternion.y, pose.quaternion.z, pose.quaternion.w] for pose in frame] for frame in frames]
        np.testing.assert_array_equal(result.translations, expected_t)
        np.testing.assert_array_equal(result.rotations_xyzw, expected_q)

if __name__ == '__main__': unittest.main()
