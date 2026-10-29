"""Synthetic ARKs exercise Luna extraction and per-file failure reporting."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console

from bootstrap import ROOT
from game_data import extractor
from luna_kit.ark import ARK


def write_ark(folder, files, name='001_and_startup.ark'):
    folder.mkdir(parents=True, exist_ok=True)
    with ARK(folder/name, 'w') as ark:
        for filename, data in files.items():
            with ark.open(filename, 'w') as entry:
                entry.write(data)


class ExtractionFailures(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.arks, self.output = self.root/'arks', self.root/'extracted'

    def test_per_file_errors_are_logged_and_other_files_still_extract(self):
        failures = {'body.rkm', 'materials/eyes.RKM', 'unrelated.txt'}
        write_ark(self.arks, {name: b'synthetic' for name in
                             ['body.rkm', 'materials/eyes.RKM', 'unrelated.txt', 'body.pvr']})
        original = ARK.extract

        def inject(ark, filename, *args, **kwargs):
            if filename in failures:
                raise OSError('synthetic extraction failure')
            return original(ark, filename, *args, **kwargs)

        with patch.object(ARK, 'extract', inject), \
             patch.object(extractor, 'console', Console(quiet=True)) as console, \
             patch.object(console, 'print', wraps=console.print) as printing:
            result = extractor.extract(self.arks, self.output)
        self.assertIsNone(result)
        self.assertEqual((self.output/'body.pvr').read_bytes(), b'synthetic')
        errors = [call.args[0] for call in printing.call_args_list
                  if call.args and isinstance(call.args[0], OSError)]
        self.assertEqual(len(errors), 3)
        self.assertEqual({error.__notes__[0] for error in errors},
                         {'filename: ' + name for name in failures})
