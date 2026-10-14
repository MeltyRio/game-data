"""Synthetic emitter resources; no native game artwork is distributed."""
import io
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from bootstrap import ROOT
from PIL import Image
from game_data.model_gen.native_emitter import DIZZY_EMITTER, load_native_emitter, parse_dizzy_particle
from game_data.model_gen.converter.native import NativeInputError


def particle():
    root = ET.Element('ParticleTemplate')
    ET.SubElement(root, 'Texture', name='synthetic', u1='0', v1='0', u2='1', v2='1')
    ET.SubElement(root, 'Timing', EmissionRate='-1', Lifetime='3', ParticleCount='3', TimeWarp='1', SpeedMultiplier='1')
    ET.SubElement(root, 'Force', x='0', y='0', z='0', w='0')
    for side, bound in [('Min', '-1'), ('Max', '1')]:
        parent = ET.SubElement(root, side)
        for i in range(3):
            node = ET.SubElement(parent, f'Delta{i}')
            ET.SubElement(node, 'Position', x=bound, y=bound, z='0', s='5' if i == 0 else '6')
            ET.SubElement(node, 'Rotation', rot=str(i * 90))
            ET.SubElement(node, 'Colour', r='1', g='0.5', b='0.25', a='0' if i == 2 else '1')
            ET.SubElement(node, 'Size', x='2', y='2')
            ET.SubElement(node, 'Time', portion=str(i))
    return root


class NativeEmitterTests(unittest.TestCase):
    def test_synthetic_values_are_preserved_not_replaced_by_game_constants(self):
        name, result = parse_dizzy_particle(ET.tostring(particle()))
        self.assertEqual(name, 'synthetic')
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['lifetime'], 3)
        self.assertEqual(result['min'][0][:3], [-1, -1, 0])
        self.assertEqual(result['positionModes'], [5, 6, 6])
        self.assertEqual(result['portions'], [0, 1, 2])

    def test_unknown_semantics_are_rejected_instead_of_discarded(self):
        for mutate in [
            lambda r: r.find('Timing').set('Sound', 'sound.wav'),
            lambda r: r.find('Min/Delta0/Position').set('s', '0'),
            lambda r: r.find('Min/Delta1/Time').set('portion', '2'),
            lambda r: r.find('Max/Delta0/Position').set('z', '1'),
            lambda r: r.find('Force').set('w', '1'),
            lambda r: r.find('Min/Delta0/Colour').set('r', 'nan'),
            lambda r: r.find('Texture').set('u2', '-1'),
            lambda r: ET.SubElement(r, 'Controller'),
            lambda r: r.find('Timing').set('ParticleCount', '129'),
            lambda r: r.find('Min/Delta0').append(ET.Element('Offset')),
        ]:
            with self.subTest(mutate=mutate):
                root = particle(); mutate(root)
                with self.assertRaises(NativeInputError): parse_dizzy_particle(ET.tostring(root))

    def test_luna_material_and_synthetic_png_closure_and_private_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / (DIZZY_EMITTER + '.particle')).write_bytes(ET.tostring(particle()))
            material = folder / 'synthetic.rkm'
            material.write_text('DiffuseTexture=synthetic\nNoCompress=1\nBlendMode=add\nDepthTest=0\nDepthWrite=0\nShader=rkshader_particle\n')
            Image.new('RGBA', (2, 2), (255, 255, 255, 255)).save(folder/'synthetic.png')
            profile, png, sources = load_native_emitter(folder, DIZZY_EMITTER)
            self.assertEqual(profile['anchor'], 'model-origin')
            self.assertNotIn('texture', profile)  # Assigned only by the GLB writer.
            self.assertEqual(Image.open(io.BytesIO(png)).size, (2, 2))
            self.assertEqual(set(sources), {DIZZY_EMITTER+'.particle', 'synthetic.rkm', 'synthetic.png'})
            self.assertNotIn(temp, str(profile))
            with self.assertRaises(NativeInputError): load_native_emitter(folder, 'unreviewed')
            material.write_text(material.read_text()+'DepthWrite=1\n')
            with self.assertRaises(NativeInputError): load_native_emitter(folder, DIZZY_EMITTER)
            material.write_text('UnknownController=1\n')
            with self.assertRaises(NativeInputError): load_native_emitter(folder, DIZZY_EMITTER)
            material.unlink()
            with self.assertRaises(NativeInputError): load_native_emitter(folder, DIZZY_EMITTER)
