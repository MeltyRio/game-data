"""Unit-test the real build function with offline pipeline dependencies.

This does not claim to run the complete application or its Python 3.14 env.
"""
import ast
from copy import deepcopy
import json
import struct
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch, ANY

from bootstrap import ROOT
from game_data import extractor, model_gen
from game_data.data_types.GameDataTypes import GameVersion
from game_data.model_gen.converter.animation_cache import AnimationCache
from luna_kit.ark import ARK
from luna_kit.api import Version
from rich.console import Console
from native_fixture import copy_rk_with_material, write_fixture, write_native_tables
from test_extractor import write_ark


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = ROOT/'src/game_data/__init__.py'
        tree = ast.parse(source.read_text())
        functions = {'build_cdn', 'get_s3_version', 'compare_versions'}
        declarations = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions]
        declarations += [n for n in tree.body if isinstance(n, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id.startswith('GAME_VERSION_')
                                 for target in n.targets)]
        # Execute the actual function, excluding imports/CLI/env initializers.
        # Every downloader/cloud entry point is a failing mock.
        self.objects = {'Pony':{}}
        self.transformer = Mock()
        self.transformer.gameObjectData = self.objects
        self.game_data = SimpleNamespace(game_objects=SimpleNamespace(**{name:SimpleNamespace(objects={name+'_id':SimpleNamespace(tags=[])}) for name in ('pony','pet','costume')}))
        self.transformer.game_data = self.game_data
        self.game_data.game_objects.costume.objects['costume_id'].pony = 'pony_id'
        self.transformer.defaultGameCampaign = {'game_object_data':{}}
        folder=self.root/'raw/extracted'
        folder.mkdir(parents=True)
        (folder/'defaultGameCampaign.json').write_text('{}')
        self.writer = Mock(return_value={'summary': {'converted':0}})
        self.globals = {'Path':Path, 'json':json, 'os':Mock(environ={}), 'perf_counter':lambda:0,
            'get_s3_client':Mock(side_effect=AssertionError('cloud client forbidden')),
            'download':Mock(side_effect=AssertionError('download forbidden')),
            'extract':Mock(side_effect=AssertionError('extract forbidden')),
            'sync':Mock(side_effect=AssertionError('upload forbidden')),
            'Notifier':Mock(), 'console':Mock(), 'shutil':Mock(),
            'Transformer':Mock(return_value=self.transformer),
            'GameData':type('GameData',(),{'load':staticmethod(lambda _:self.game_data)}),
            'GameObjectData':Mock(return_value={'PonyPet':{}}),
            'generate_images':Mock(), 'generate_models':self.writer, 'AnimationCache':AnimationCache,
            'GameVersion':GameVersion, 'Version':Version}
        exec(compile(ast.Module(body=declarations,type_ignores=[]),str(source),'exec'),self.globals)

    def run_build(self, skip):
        return self.globals['build_cdn'](self.root/'raw',self.root/'dist','test',False,self.root/'overrides',skip=skip)

    def test_normal_stage_reuses_transformer_native_objects(self):
        self.assertTrue(self.run_build(['download','extract']))
        self.writer.assert_called_once_with(self.objects,self.root/'raw/extracted',self.root/'dist',
            self.root/'raw/model-associations.json',campaign=self.transformer.defaultGameCampaign,
            public_identities={'pony_id','pet_id','costume_id'}, outfit_owners={'costume_id': 'pony_id'},
            animation_cache=ANY)
        self.globals['GameObjectData'].assert_not_called()
        self.transformer.start.assert_called_once()
        self.transformer.save.assert_called_once()
        self.globals['generate_images'].assert_called_once()
        self.globals['sync'].assert_not_called()
        self.globals['get_s3_client'].assert_not_called()


    def test_npc_tags_allow_models_without_changing_page_data(self):
        for name in ('pony', 'pet', 'costume'):
            objects = getattr(self.game_data.game_objects, name).objects
            objects[name+'_npc'] = SimpleNamespace(tags=['quest', 'npc'], pony='')
            objects[name+'_other_tags'] = SimpleNamespace(tags=['unused', 'quest'], pony='')
        self.game_data.game_objects.pony.objects['Pony_NPC_Like_Name'] = SimpleNamespace(tags=[])
        before = deepcopy(self.game_data)
        expected = {'Pony_NPC_Like_Name', 'pony_id', 'pet_id', 'costume_id',
                    'pony_other_tags', 'pet_other_tags', 'costume_other_tags',
                    'pony_npc', 'pet_npc', 'costume_npc'}
        for skip in (['download', 'extract'], ['download', 'extract', 'transform']):
            with self.subTest(skip=skip):
                self.writer.reset_mock()
                self.assertTrue(self.run_build(skip))
                self.writer.assert_called_once()
                self.assertEqual(self.writer.call_args.kwargs['public_identities'], expected)
                self.assertEqual(self.game_data, before)


    def prepare_extraction(self, *, rkm=True, extra_files=None, mixed=False):
        source = self.root/'archive-input'
        write_fixture(source)
        self.transformer.gameObjectData = write_native_tables(source)
        if mixed:
            copy_rk_with_material(source/'flag.rk', source/'safe.rk', 'safe')
            (source/'safe.rkm').write_bytes((source/'flag.rkm').read_bytes())
            self.transformer.gameObjectData = write_native_tables(source, {
                'Synthetic_Flag': {'model':'flag', 'animation':'flag', 'events':'flag'},
                'Synthetic_Good': {'model':'safe', 'animation':'flag', 'events':'flag'}})
        if not rkm:
            (source/'flag.rkm').unlink()
        # Same-name PVR is required by the native absent-RKM rule.
        header = struct.pack('<4sI4s4s9I', b'PVR\x03', 0, b'rgba', bytes([8]*4),
                             0, 0, 2, 2, 1, 1, 1, 1, 0)
        files = {p.name: p.read_bytes() for p in source.iterdir()}
        files['flag.pvr'] = header + bytes([40, 180, 210, 255])*4
        files.update(extra_files or {})
        write_ark(self.root/'raw/arks', files)
        self.game_data.game_objects.pony.objects = {'Synthetic_Flag': SimpleNamespace(tags=[])}
        if mixed:
            self.game_data.game_objects.pony.objects['Synthetic_Good'] = SimpleNamespace(tags=[])
        self.game_data.game_objects.pet.objects = {}
        self.game_data.game_objects.costume.objects = {}
        self.globals['extract'] = extractor.extract
        self.writer.side_effect = model_gen.generate_models

    def test_native_absent_rkm_still_generates_default_material(self):
        self.prepare_extraction(rkm=False)
        with patch.object(extractor, 'console', Console(quiet=True)):
            self.assertTrue(self.run_build(['download']))
        report = json.loads((self.root/'raw/model-associations.json').read_text())
        model = report['identities']['Synthetic_Flag']
        self.assertTrue(model['converted'])
        self.assertEqual(model['conversion_provenance']['material_sources'][0]['status'],
                         'native-default-without-rkm')
        self.assertTrue((self.root/'dist/models/3d/Synthetic_Flag.glb').is_file())
        self.assertFalse((self.root/'raw/extracted/flag.rkm').exists())
        self.globals['generate_images'].assert_called_once()

    def test_rkm_extraction_failure_uses_supplied_files_and_native_default(self):
        self.prepare_extraction(mixed=True)
        original = ARK.extract
        def inject(ark, filename, *args, **kwargs):
            if filename == 'flag.rkm':
                raise OSError('synthetic RKM extraction failure')
            return original(ark, filename, *args, **kwargs)
        with patch.object(ARK, 'extract', inject), patch.object(extractor, 'console', Console(quiet=True)):
            self.assertTrue(self.run_build(['download']))
        catalogue = json.loads((self.root/'dist/models_3d.json').read_text())
        report = json.loads((self.root/'raw/model-associations.json').read_text())
        self.assertEqual(set(catalogue['models']), {'Synthetic_Flag', 'Synthetic_Good'})
        self.assertFalse((self.root/'raw/extracted/flag.rkm').exists())
        self.assertTrue((self.root/'dist/models/3d/Synthetic_Flag.glb').is_file())
        self.assertEqual(report['identities']['Synthetic_Flag']['conversion_provenance']['material_sources'][0]['status'],
                         'native-default-without-rkm')
        self.assertEqual(report['identities']['Synthetic_Good']['conversion_provenance']['material_sources'][0]['status'],
                         'parsed-source-rkm')

    def test_extraction_level_failure_stops_before_generation_upload_and_dlc_update(self):
        # A failure outside the per-file handler has unknown dependency scope.
        self.prepare_extraction(extra_files={'atlas/broken.texatlas': b'synthetic'})
        client = Mock()
        client.get_object.side_effect = OSError('no previous synthetic version')
        self.globals['os'].environ = {'S3_BUCKET': 'synthetic-local-only'}
        self.globals['get_s3_client'] = Mock(return_value=client)
        self.globals['API'] = Mock(side_effect=AssertionError('DLC request forbidden'))
        original = ARK.extract

        def inject(ark, filename, *args, **kwargs):
            if filename == 'flag.rkm':
                raise OSError('synthetic RKM extraction failure')
            return original(ark, filename, *args, **kwargs)

        with patch.object(ARK, 'extract', inject), patch.object(extractor, 'console', Console(quiet=True)), \
             patch.object(extractor, 'TexAtlas', side_effect=OSError('synthetic atlas failure')) as atlas:
            self.assertFalse(self.globals['build_cdn'](
                self.root/'raw', self.root/'dist', '11.4.3c', True,
                self.root/'overrides', skip=['download']))
        atlas.assert_called_once_with(self.root/'raw/extracted/atlas/broken.texatlas')
        self.globals['Transformer'].assert_not_called()
        self.writer.assert_not_called()
        self.globals['generate_images'].assert_not_called()
        self.globals['sync'].assert_not_called()
        client.copy_object.assert_not_called()
        client.put_object.assert_not_called()
        self.globals['API'].assert_not_called()
        self.assertFalse((self.root/'dist/models_3d.json').exists())

    def test_non_rkm_failure_keeps_existing_tolerance(self):
        self.prepare_extraction(extra_files={'unrelated.txt': b'synthetic'})
        original = ARK.extract

        def inject(ark, filename, *args, **kwargs):
            if filename == 'unrelated.txt':
                raise OSError('synthetic unrelated failure')
            return original(ark, filename, *args, **kwargs)

        with patch.object(ARK, 'extract', inject), patch.object(extractor, 'console', Console(quiet=True)):
            self.assertTrue(self.run_build(['download']))
        self.writer.assert_called_once()
        self.globals['generate_images'].assert_called_once()
