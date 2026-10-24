"""Synthetic emitter resources; no native game artwork is distributed."""
import io
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from bootstrap import ROOT
from PIL import Image
from game_data.model_gen.native_emitter import DIZZY_EMITTER, load_native_emitter, parse_dizzy_particle
from game_data.model_gen.converter.native import NativeInputError
from game_data.model_gen.converter.texture_cache import TextureCache


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


class EmitterTextureCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root/'native'; self.folder.mkdir()
        self.material = self.folder/'synthetic.rkm'
        self.cache = TextureCache(self.root/'cache')
        self.fixture()

    def fixture(self, compressed=False):
        (self.folder/(DIZZY_EMITTER+'.particle')).write_bytes(ET.tostring(particle()))
        self.material.write_text('DiffuseTexture=synthetic\nNoCompress='+str(int(not compressed))+
            '\nBlendMode=add\nDepthTest=0\nDepthWrite=0\nShader=rkshader_particle\n')
        Image.new('RGBA', (2, 2), (40, 80, 120, 128)).save(self.folder/'synthetic.png')
        if compressed:
            self.pvr(self.folder/'synthetic.pvr', (255, 0, 0, 255))

    def pvr(self, path, pixel, size=(2, 2)):
        width, height = size
        header = struct.pack('<4sI4s4s9I', b'PVR\x03', 0, b'rgba', bytes([8]*4),
                             0, 0, height, width, 1, 1, 1, 1, 0)
        path.write_bytes(header + bytes(pixel)*(width*height))

    def test_png_bytes_are_shared_but_profiles_and_source_names_are_independent(self):
        original = load_native_emitter(self.folder, DIZZY_EMITTER)
        first = load_native_emitter(self.folder, DIZZY_EMITTER, texture_cache=self.cache)
        self.assertEqual(first, original)
        other = self.root/'other'; other.mkdir()
        doc = particle(); doc.find('Timing').set('ParticleCount', '7')
        (other/(DIZZY_EMITTER+'.particle')).write_bytes(ET.tostring(doc))
        (other/'synthetic.rkm').write_text(self.material.read_text().replace('DiffuseTexture=synthetic', 'DiffuseTexture=renamed'))
        (other/'renamed.png').write_bytes((self.folder/'synthetic.png').read_bytes())
        warm_cache = TextureCache(self.cache.directory)
        with patch.object(Image.Image, 'convert', side_effect=AssertionError('Decoded a cached image')):
            second = load_native_emitter(other, DIZZY_EMITTER, texture_cache=warm_cache)
        self.assertEqual((self.cache.misses, warm_cache.hits), (1, 1))
        self.assertEqual(first[1], second[1])
        self.assertEqual((first[0]['particle']['count'], second[0]['particle']['count']), (3, 7))
        self.assertEqual(second[2], [DIZZY_EMITTER+'.particle', 'synthetic.rkm', 'renamed.png'])
        first[0]['particle']['min'][0][0] = 99
        self.assertEqual(second[0]['particle']['min'][0][0], -1)
        self.assertEqual(load_native_emitter(self.folder, DIZZY_EMITTER, texture_cache=warm_cache), original)

    def test_pvr_alpha_content_and_presence_invalidate_without_losing_embedded_alpha(self):
        self.fixture(compressed=True)
        primary = self.folder/'synthetic.pvr'; alpha = self.folder/'synthetic.alpha.pvr'
        for alpha_value, embedded, expected in [(64, 255, 64), (96, 255, 96), (32, 80, 80), (None, 255, 255)]:
            with self.subTest(alpha=alpha_value, embedded=embedded):
                self.pvr(primary, (255, 0, 0, embedded))
                if alpha_value is None: alpha.unlink()
                else: self.pvr(alpha, (alpha_value, alpha_value, alpha_value, 255))
                uncached = load_native_emitter(self.folder, DIZZY_EMITTER)
                actual = load_native_emitter(self.folder, DIZZY_EMITTER, texture_cache=self.cache)
                self.assertEqual(actual, uncached)
                with Image.open(io.BytesIO(actual[1])) as image:
                    self.assertEqual(image.size, (2, 2))
                    self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, expected))
                with patch('luna_kit.pvr.PVR', side_effect=AssertionError('Decoded cached PVR')):
                    self.assertEqual(load_native_emitter(self.folder, DIZZY_EMITTER, texture_cache=self.cache), actual)
        self.assertEqual((self.cache.misses, self.cache.hits), (4, 4))

    def test_warm_cache_keeps_strict_png_and_material_validation(self):
        for invalid in ('jpeg', 'corrupt', 'missing', 'material', 'particle'):
            with self.subTest(invalid=invalid):
                self.fixture()
                load_native_emitter(self.folder, DIZZY_EMITTER, texture_cache=self.cache)
                path = self.folder/'synthetic.png'
                if invalid == 'jpeg': Image.new('RGB', (2, 2)).save(path, format='JPEG')
                elif invalid == 'corrupt': path.write_bytes(b'bad PNG')
                elif invalid == 'missing': path.unlink()
                elif invalid == 'material': self.material.write_text(self.material.read_text()+'DepthWrite=1\n')
                else: (self.folder/(DIZZY_EMITTER+'.particle')).write_bytes(b'<bad')
                with self.assertRaises(NativeInputError) as original:
                    load_native_emitter(self.folder, DIZZY_EMITTER)
                with self.assertRaises(NativeInputError) as cached:
                    load_native_emitter(self.folder, DIZZY_EMITTER, texture_cache=self.cache)
                self.assertEqual(str(cached.exception), str(original.exception))

    def test_invalid_sidecars_preserve_primary_decode_error_precedence(self):
        self.fixture(compressed=True)
        primary = self.folder/'synthetic.pvr'; alpha = self.folder/'synthetic.alpha.pvr'
        for invalid in ('size', 'broken', 'escape', 'directory'):
            with self.subTest(invalid=invalid):
                self.pvr(primary, (255, 0, 0, 255))
                if invalid == 'size': self.pvr(alpha, (64, 64, 64, 255), (3, 2))
                elif invalid == 'broken': alpha.symlink_to('missing')
                elif invalid == 'escape':
                    self.pvr(self.root/'outside.pvr', (64, 64, 64, 255)); alpha.symlink_to(self.root/'outside.pvr')
                else: alpha.mkdir()
                for corrupt_primary in (False, True):
                    if corrupt_primary: primary.write_bytes(b'broken PVR')
                    with self.assertRaises(NativeInputError) as original:
                        load_native_emitter(self.folder, DIZZY_EMITTER)
                    with self.assertRaises(NativeInputError) as cached:
                        load_native_emitter(self.folder, DIZZY_EMITTER, texture_cache=self.cache)
                    self.assertEqual(str(cached.exception), str(original.exception))
                if invalid == 'directory': alpha.rmdir()
                else: alpha.unlink()

    def test_disabled_unwritable_and_corrupt_cache_preserve_successful_output(self):
        expected = load_native_emitter(self.folder, DIZZY_EMITTER)
        blocked = self.root/'blocked'; blocked.write_text('not a directory')
        for cache in (TextureCache(self.root/'disabled', max_bytes=0), TextureCache(blocked), self.cache):
            self.assertEqual(load_native_emitter(self.folder, DIZZY_EMITTER, texture_cache=cache), expected)
        entry, = self.cache.directory.glob('*.pngcache')
        data = bytearray(entry.read_bytes()); data[data.index(b'IDAT')+4] ^= 1; entry.write_bytes(data)
        self.assertEqual(load_native_emitter(self.folder, DIZZY_EMITTER, texture_cache=self.cache), expected)
        self.assertEqual((self.cache.misses, self.cache.invalid_entries), (2, 1))
