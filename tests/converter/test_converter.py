import io
import json
import struct
import unittest

import numpy as np
from PIL import Image

from game_data.model_gen.converter import Model, Rig, Clip, model_glb, animation_glb, ConversionError
from game_data.model_gen.converter.core import local_trs, pose_matrices


def fixture():
    rig = Rig('a' * 64, [-1, 0], ['root', 'child'])
    bind = np.repeat(np.eye(4)[None], 2, axis=0)
    bind[1, 1, 3] = 1
    rest = bind.copy()
    rest[0, 0, 3] = .25
    rest[1, 0, 3] = .25
    png = io.BytesIO()
    image = Image.new('RGBA', (2, 2))
    image.putdata([(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 128), (255, 255, 0, 0)])
    image.save(png, format='PNG')
    m = Model(
        'Synthetic', rig,
        np.array([[-1, 0, 0], [1, 0, 0], [1, 2, 0], [-1, 2, 0]], float),
        np.array([[0, 0], [1, 0], [1, 1], [0, 1]], float),
        np.array([[0, 0, 0, 0], [0, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0]]),
        np.tile([1., 0, 0, 0], (4, 1)), bind, rest,
        [{'name': 'panel', 'material': 0,
          'indices': np.array([[0, 1, 2], [0, 2, 3]]), 'native_back_face': False}],
        [{'name': 'rgba', 'png_bytes': png.getvalue(),
          'rkm_fields': {'BlendMode': 'alpha', 'Cull': '0'}}],
    )
    frames = np.repeat(rest[None], 3, axis=0)
    frames[1, 1, 0, 3] = .75
    frames[2, 1, 0, 3] = 1.25
    return m, Clip('wave', rig, frames, 10)


def unpack(blob):
    magic, version, length = struct.unpack_from('<4sII', blob)
    assert (magic, version, length) == (b'glTF', 2, len(blob))
    n, tag = struct.unpack_from('<I4s', blob, 12)
    assert tag == b'JSON'
    doc = json.loads(blob[20:20 + n])
    size, tag = struct.unpack_from('<I4s', blob, 20 + n)
    assert tag == b'BIN\0'
    return doc, blob[28 + n:28 + n + size]


class ConverterTest(unittest.TestCase):
    def test_static_skin_and_binary(self):
        m, _ = fixture()
        d, b = unpack(model_glb(m))
        self.assertNotIn('animations', d)
        self.assertEqual(d['skins'][0]['joints'], [1, 2])
        self.assertEqual(d['materials'][0]['alphaMode'], 'BLEND')
        self.assertTrue(d['materials'][0]['doubleSided'])
        self.assertIn('KHR_materials_unlit', d['extensionsUsed'])
        self.assertEqual(d['nodes'][0]['rotation'], [1, 0, 0, 0])
        self.assertEqual(d['scenes'][0]['nodes'], [0, 3])
        a = d['accessors'][d['skins'][0]['inverseBindMatrices']]
        v = d['bufferViews'][a['bufferView']]
        inv = np.frombuffer(b[v['byteOffset']:v['byteOffset'] + v['byteLength']], '<f4').reshape(2, 4, 4).transpose(0, 2, 1)
        np.testing.assert_allclose(m.bind_global @ inv, np.eye(4)[None].repeat(2, 0))
        for view in d['bufferViews']:
            self.assertEqual(view['byteOffset'] % 4, 0)
        self.assertFalse(any('uri' in image for image in d['images']))

    def test_external_animation_and_endpoint(self):
        _, clip = fixture()
        d, b = unpack(animation_glb(clip))
        self.assertNotIn('meshes', d)
        self.assertNotIn('images', d)
        self.assertEqual(len(d['animations'][0]['channels']), 4)
        self.assertEqual(d['extras']['atpClip']['duration'], .3)
        self.assertTrue(all(s['interpolation'] == 'STEP' for s in d['animations'][0]['samplers']))

    def test_global_to_local_roundtrip(self):
        m, clip = fixture()
        t, q = local_trs(clip.global_frames, m.rig.parents)
        local = pose_matrices(t, q)
        global_ = local.copy()
        global_[:, 1] = global_[:, 0] @ local[:, 1]
        np.testing.assert_allclose(global_, clip.global_frames, atol=1e-7)

    def test_rotated_pose_roundtrip(self):
        q = np.array([[[0, .5, 0, np.sqrt(.75)], [.2, .3, .4, .8]]])
        t = np.array([[[1, 2, 3], [4, 5, 6]]])
        global_ = pose_matrices(t, q)
        lt, lq = local_trs(global_, [-1, 0])
        local = pose_matrices(lt, lq)
        local[:, 1] = local[:, 0] @ local[:, 1]
        np.testing.assert_allclose(local, global_, atol=1e-7)

    def test_bad_weight_rejected(self):
        m, _ = fixture()
        m.weights[0] = 0
        with self.assertRaisesRegex(ConversionError, 'weights'):
            model_glb(m)

    def test_wrong_joint_rejected(self):
        m, _ = fixture()
        m.joints[0, 0] = 2
        with self.assertRaisesRegex(ConversionError, 'joint'):
            model_glb(m)

    def test_shear_rejected(self):
        m, _ = fixture()
        m.rest_global[0, 0, 1] = .2
        with self.assertRaisesRegex(ConversionError, 'shear'):
            model_glb(m)

    def test_unknown_shader_rejected(self):
        m, _ = fixture()
        m.materials[0]['rkm_fields']['Shader'] = 'unknown'
        with self.assertRaisesRegex(ConversionError, 'shader'):
            model_glb(m)

    def test_model_animation_binding_token(self):
        m, c = fixture()
        self.assertEqual(unpack(model_glb(m))[0]['extras']['atpRig'], unpack(animation_glb(c))[0]['extras']['atpRig'])


if __name__ == '__main__':
    unittest.main()
