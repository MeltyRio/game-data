"""Exercise the actual costume transformer without CLI/cloud initialization."""
import ast
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock

from luna_kit.gameobjectdata import GameObject


ROOT = Path(__file__).resolve().parents[1]


def load_data_types():
    # Import the real Pydantic types under an isolated package name. Importing
    # game_data itself would initialize unrelated downloader/image dependencies.
    name = '_transformer_native_input_types'
    package = ModuleType(name)
    package.__path__ = [str(ROOT / 'src/game_data/data_types')]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(
        name + '.GameDataTypes', ROOT / 'src/game_data/data_types/GameDataTypes.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class NativeInputs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_types = load_data_types()
        self.raw = {'PonySet': {
            'SET_Base': GameObject('SET_Base', 'PonySet',
                                  {'PonySet': {'SubSets': ['SET_Sub']}}),
            'SET_Sub': GameObject('SET_Sub', 'PonySet',
                                 {'PonySet': {'SubSets': []}}),
        }}
        pony = data_types.PonyType.model_construct(index=0, id='Pony_Test',
                                                   costumes=['SET_Base'])
        self.public = SimpleNamespace(
            costume=SimpleNamespace(objects={}),
            pony=SimpleNamespace(objects={'Pony_Test': pony}))
        self.transformer = SimpleNamespace(
            gameObjectData=self.raw,
            game_data=SimpleNamespace(game_objects=self.public),
            game_objects_folder=Path(temporary.name),
            translate_string=lambda value: {'english': value},
            add_image=lambda *args: None)
        self.console = Mock()
        source = ROOT / 'src/game_data/transformer.py'
        tree = ast.parse(source.read_text())
        transformer = next(node for node in tree.body
                           if isinstance(node, ast.ClassDef) and node.name == 'Transformer')
        method = next(node for node in transformer.body
                      if isinstance(node, ast.FunctionDef) and node.name == 'get_category_costume')
        namespace = {'CostumeType': data_types.CostumeType,
                     'track': lambda items, **kwargs: items, 'console': self.console}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
        self.transform = namespace['get_category_costume']

    def test_costume_subsets_preserve_native_declarations_and_public_group(self):
        original = copy.deepcopy(self.raw)
        self.transform(self.transformer)
        self.console.print_exception.assert_not_called()
        self.assertEqual(self.raw, original)
        self.assertEqual(self.public.costume.objects['SET_Base'].subsets,
                         ['SET_Base', 'SET_Sub'])
        self.assertEqual(self.public.costume.objects['SET_Sub'].subsets,
                         ['SET_Base', 'SET_Sub'])
        self.assertEqual(self.public.pony.objects['Pony_Test'].costumes,
                         ['SET_Base', 'SET_Sub'])
        self.assertIsNot(self.public.costume.objects['SET_Base'].subsets,
                         self.raw['PonySet']['SET_Base']['PonySet']['SubSets'])

    def test_repeated_transform_does_not_accumulate_parent_in_native_subsets(self):
        original = copy.deepcopy(self.raw)
        self.transform(self.transformer)
        self.transform(self.transformer)
        self.console.print_exception.assert_not_called()
        self.assertEqual(self.raw, original)
        self.assertEqual(self.public.costume.objects['SET_Base'].subsets,
                         ['SET_Base', 'SET_Sub'])


if __name__ == '__main__':
    unittest.main()
