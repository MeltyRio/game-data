"""Encoded texture cache: persistent reuse, exact dependencies and safe recovery."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import bootstrap
from game_data.model_gen.converter.native import load_native
from game_data.model_gen.converter.texture_cache import TextureCache
from native_fixture import write_fixture


class TextureCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.png'
        Image.new('RGBA', (3, 2), (10, 20, 30, 255)).save(self.source)
        self.decodes = 0

    def cache(self, **kwargs):
        return TextureCache(self.root / 'cache', **kwargs)

    def decode(self):
        self.decodes += 1
        with Image.open(self.source) as image:
            rgba = image.convert('RGBA')
            output = io.BytesIO()
            rgba.save(output, format='PNG')
            return output.getvalue(), rgba.width, rgba.height

    def read(self, cache, sources=None, settings='rgba-png-v1'):
        return cache.get_or_decode(sources or [self.source], settings=settings, decode=self.decode)

    def test_new_instance_reuses_encoded_bytes_without_decoding(self):
        first = self.read(self.cache())
        with patch.object(Image.Image, 'convert', side_effect=AssertionError('Decoded a cache hit')):
            second = self.read(self.cache())
        self.assertEqual(first, second)
        self.assertEqual(second[1:], (3, 2))
        self.assertEqual(self.decodes, 1)
        with Image.open(io.BytesIO(second[0])) as image:
            self.assertEqual(image.getpixel((1, 1)), (10, 20, 30, 255))

    def test_source_content_alpha_content_and_settings_invalidate(self):
        alpha = self.root / 'source.alpha.pvr'
        alpha.write_bytes(b'first alpha')
        cache = self.cache()
        sources = [self.source, alpha]
        first = self.read(cache, sources)
        stamp = self.source.stat()
        Image.new('RGBA', (3, 2), (90, 20, 30, 255)).save(self.source)
        import os
        os.utime(self.source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        second = self.read(cache, sources)
        self.assertNotEqual(first[0], second[0])
        alpha.write_bytes(b'other alpha')
        self.read(cache, sources)
        self.read(cache, sources, settings='other-conversion')
        self.read(cache, [self.source])
        self.assertEqual(self.decodes, 5)

    def test_corrupt_and_partial_entries_recover_through_decoder(self):
        cache = self.cache()
        expected = self.read(cache)
        entry, = (self.root / 'cache').glob('*.pngcache')
        valid = entry.read_bytes()
        for bad in (valid[:20], valid[:-1], valid[:-8] + b'corrupt!', b'not a cache'):
            with self.subTest(length=len(bad)):
                entry.write_bytes(bad)
                self.assertEqual(self.read(cache), expected)
                self.assertEqual(entry.read_bytes(), valid)
        self.assertEqual(self.decodes, 5)
        self.assertEqual(getattr(cache, 'invalid_entries', 0), 4)
        self.assertFalse(list((self.root / 'cache').glob('*.tmp')))

    def test_eviction_bounds_disk_bytes_and_evicts_oldest(self):
        cache = self.cache(max_bytes=400)
        self.read(cache)
        first, = (self.root / 'cache').glob('*.pngcache')
        for red in (80, 90, 100, 110):
            Image.new('RGBA', (3, 2), (red, 20, 30, 255)).save(self.source)
            self.read(cache)
        entries = list((self.root / 'cache').glob('*.pngcache'))
        self.assertGreater(len(entries), 0)
        self.assertLessEqual(sum(path.stat().st_size for path in entries), 400)
        self.assertFalse(first.exists())
        self.assertEqual(self.read(self.cache(max_bytes=400))[1:], (3, 2))
        self.assertEqual(self.decodes, 5)

    def test_disabled_or_unwritable_cache_preserves_decode(self):
        cache = self.cache(max_bytes=0)
        self.read(cache)
        self.read(cache)
        self.assertEqual(self.decodes, 2)
        self.assertFalse((self.root / 'cache').exists())
        (self.root / 'cache').write_text('a file blocks the cache directory')
        self.assertEqual(self.read(self.cache())[1:], (3, 2))
        self.assertEqual(self.decodes, 3)

    def test_source_changed_during_decode_does_not_poison_old_key(self):
        cache = self.cache()
        original = self.source.read_bytes()
        def changing_decode():
            result = self.decode()
            Image.new('RGBA', (3, 2), (90, 20, 30, 255)).save(self.source)
            return result
        cache.get_or_decode([self.source], settings='rgba-png-v1', decode=changing_decode)
        self.source.write_bytes(original)
        self.read(cache)
        self.assertEqual(self.decodes, 2)

    def test_decoder_version_changes_invalidate_persistent_entry(self):
        self.read(self.cache())
        with patch('game_data.model_gen.converter.texture_cache._decoder_fingerprint', return_value='changed-decoder'):
            self.read(self.cache())
        self.assertEqual(self.decodes, 2)

    def test_failed_atomic_replace_leaves_no_partial_entry(self):
        cache = self.cache()
        with patch('game_data.model_gen.converter.texture_cache.os.replace', side_effect=OSError('disk full')):
            result = self.read(cache)
        self.assertEqual(result[1:], (3, 2))
        self.assertEqual(list((self.root / 'cache').iterdir()), [])
        self.assertEqual(getattr(cache, 'io_errors', 0), 1)
        self.read(cache)
        self.assertEqual(self.decodes, 2)

    def test_oversized_result_is_not_retained(self):
        cache = self.cache(max_bytes=20)
        self.read(cache)
        self.read(cache)
        self.assertEqual(self.decodes, 2)
        self.assertFalse((self.root / 'cache').exists())

    def test_native_pvr_alpha_changes_invalidate_and_preserve_embedded_alpha(self):
        import struct
        native = self.root / 'native'
        write_fixture(native)
        rkm = native / 'flag.rkm'
        rkm.write_text(rkm.read_text().replace('NoCompress=true', 'NoCompress=false'))
        header = struct.pack('<4sI4s4s9I', b'PVR\x03', 0, b'rgba', bytes([8] * 4),
                             0, 0, 2, 2, 1, 1, 1, 1, 0)
        primary = native / 'flag.pvr'
        alpha = native / 'flag.alpha.pvr'
        primary.write_bytes(header + bytes([255, 0, 0, 255]) * 4)
        alpha.write_bytes(header + bytes([128, 128, 128, 255]) * 4)
        args = (native / 'flag.rk', native / 'flag.anim', native / 'flag.csv', ['wave'])
        kwargs = dict(visible_meshes=['flag'], rest_name='rest', events_absent=True)
        cache = self.cache()
        def pixels():
            actual = load_native(*args, **kwargs, texture_cache=cache)['materials'][0]
            baseline = load_native(*args, **kwargs)['materials'][0]
            self.assertEqual(actual, baseline)
            with Image.open(io.BytesIO(actual['png_bytes'])) as image:
                return image.getpixel((0, 0))
        self.assertEqual(pixels(), (255, 0, 0, 128))
        self.assertEqual(pixels(), (255, 0, 0, 128))
        alpha.write_bytes(header + bytes([64, 64, 64, 255]) * 4)
        self.assertEqual(pixels(), (255, 0, 0, 64))
        primary.write_bytes(header + bytes([255, 0, 0, 96]) * 4)
        self.assertEqual(pixels(), (255, 0, 0, 96))
        self.assertEqual((cache.hits, cache.misses), (1, 3))

    def test_native_loader_cached_material_is_byte_equivalent(self):
        write_fixture(self.root / 'native')
        native = self.root / 'native'
        args = (native / 'flag.rk', native / 'flag.anim', native / 'flag.csv', ['wave'])
        kwargs = dict(visible_meshes=['flag'], rest_name='rest', events_absent=True)
        cache = self.cache()
        baseline = load_native(*args, **kwargs)
        cold = load_native(*args, **kwargs, texture_cache=cache)
        with patch.object(Image.Image, 'convert', side_effect=AssertionError('Decoded cached native PNG')):
            warm = load_native(*args, **kwargs, texture_cache=self.cache())
        self.assertEqual(baseline['materials'], cold['materials'])
        self.assertEqual(baseline['materials'], warm['materials'])
        self.assertEqual(baseline['provenance'], warm['provenance'])


if __name__ == '__main__':
    unittest.main()
