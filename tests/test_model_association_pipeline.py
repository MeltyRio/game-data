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
from unittest.mock import Mock, patch

from bootstrap import ROOT
from game_data import extractor, model_associations as audit, model_gen
from luna_kit.ark import ARK
from rich.console import Console
from native_fixture import write_fixture, write_native_tables
from test_extractor import write_ark


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = ROOT/'src/game_data/__init__.py'
        tree = ast.parse(source.read_text())
        build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'build_cdn')
        # Execute the actual function, excluding imports/CLI/env initializers.
        # Every downloader/cloud entry point is a failing mock.
        self.objects = {'Pony':{}}
        self.transformer = Mock()
        self.transformer.gameObjectData = self.objects
        self.game_data = SimpleNamespace(game_objects=SimpleNamespace(**{name:SimpleNamespace(objects={name+'_id':SimpleNamespace(tags=[])}) for name in ('pony','pet','costume')}))
        self.transformer.game_data = self.game_data
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
            'generate_images':Mock(), 'generate_models':self.writer}
        exec(compile(ast.Module(body=[build],type_ignores=[]),str(source),'exec'),self.globals)

    def run_build(self, skip):
        return self.globals['build_cdn'](self.root/'raw',self.root/'dist','test',False,self.root/'overrides',skip=skip)

    def test_normal_stage_reuses_transformer_native_objects(self):
        self.assertTrue(self.run_build(['download','extract']))
        self.writer.assert_called_once_with(self.objects,self.root/'raw/extracted',self.root/'dist',
            self.root/'raw/model-associations.json',campaign=self.transformer.defaultGameCampaign,
            public_identities={'pony_id','pet_id','costume_id'})
        self.globals['GameObjectData'].assert_not_called()
        self.transformer.start.assert_called_once()
        self.transformer.save.assert_called_once()
        self.globals['generate_images'].assert_called_once()
        self.globals['sync'].assert_not_called()
        self.globals['get_s3_client'].assert_not_called()

    def test_skip_transform_reloads_native_tables_not_public_models(self):
        self.assertTrue(self.run_build(['download','extract','transform']))
        self.globals['GameObjectData'].assert_called_once_with(self.root/'raw/extracted/gameobjectdata.xml',
            self.root/'raw/extracted/shopdata.xml',self.root/'raw/extracted/gameobjectcategorydata.xml')
        self.assertEqual(self.writer.call_args.args[0], {'PonyPet':{}})
        self.globals['Transformer'].assert_not_called()

    def test_existing_npc_tags_filter_models_without_changing_page_data(self):
        for name in ('pony', 'pet', 'costume'):
            objects = getattr(self.game_data.game_objects, name).objects
            objects[name+'_npc'] = SimpleNamespace(tags=['quest', 'npc'])
            objects[name+'_other_tags'] = SimpleNamespace(tags=['unused', 'quest'])
        self.game_data.game_objects.pony.objects['Pony_NPC_Like_Name'] = SimpleNamespace(tags=[])
        before = deepcopy(self.game_data)
        expected = {'Pony_NPC_Like_Name', 'pony_id', 'pet_id', 'costume_id',
                    'pony_other_tags', 'pet_other_tags', 'costume_other_tags'}
        for skip in (['download', 'extract'], ['download', 'extract', 'transform']):
            with self.subTest(skip=skip):
                self.writer.reset_mock()
                self.assertTrue(self.run_build(skip))
                self.writer.assert_called_once()
                self.assertEqual(self.writer.call_args.kwargs['public_identities'], expected)
                self.assertEqual(self.game_data, before)

    def test_npc_filter_replaces_catalogue_without_deleting_shared_or_old_assets(self):
        native = self.root/'raw/extracted'
        write_fixture(native)
        self.objects = write_native_tables(native, {
            identity: {'model': 'flag', 'animation': 'flag', 'events': 'flag'}
            for identity in ('Synthetic_Flag', 'Synthetic_NPC')})
        self.transformer.gameObjectData = self.objects
        self.game_data.game_objects.pony.objects = {
            'Synthetic_Flag': SimpleNamespace(tags=[]),
            'Synthetic_NPC': SimpleNamespace(tags=['npc']),
        }
        self.game_data.game_objects.pet.objects = {}
        self.game_data.game_objects.costume.objects = {}
        dist = self.root/'dist'
        catalogue = dist/'models_3d.json'
        report_path = self.root/'raw/model-associations.json'
        # Seed a previous generation in which both identities were eligible.
        model_gen.generate_models(self.objects, native, dist, report_path,
                                  campaign=self.transformer.defaultGameCampaign)
        before = json.loads(catalogue.read_text())['models']
        self.assertEqual(before['Synthetic_Flag']['animations'], before['Synthetic_NPC']['animations'])
        assets = {ref['path']: (dist/ref['path']).read_bytes()
                  for row in before.values() for ref in [row, *row['animations']]}
        self.writer.side_effect = model_gen.generate_models
        self.globals['GameObjectData'].return_value = self.objects

        self.assertTrue(self.run_build(['download', 'extract']))

        self.assertEqual(json.loads(catalogue.read_text())['models'],
                         {'Synthetic_Flag': before['Synthetic_Flag']})
        self.assertEqual({name: (dist/name).read_bytes() for name in assets}, assets)
        report = json.loads(report_path.read_text())
        self.assertEqual(report['generation']['converted'], 1)
        self.assertEqual(report['generation']['excluded'], {})
        self.assertFalse(report['identities']['Synthetic_NPC']['converted'])
        self.assertEqual(report['generation']['unreferenced_local_assets'],
                         [before['Synthetic_NPC']['path']])
        self.assertIn('Synthetic_NPC', self.game_data.game_objects.pony.objects)
        self.globals['sync'].assert_not_called()
        self.globals['get_s3_client'].assert_not_called()

        # A build containing only NPCs must clear the catalogue, not retain it.
        self.game_data.game_objects.pony.objects['Synthetic_Flag'].tags = ['npc']
        self.assertTrue(self.run_build(['download', 'extract', 'transform']))
        self.assertEqual(json.loads(catalogue.read_text())['models'], {})
        self.assertEqual({name: (dist/name).read_bytes() for name in assets}, assets)

    def test_explicit_skip_does_not_read_or_write_association_inputs(self):
        self.assertTrue(self.run_build(['download','extract','transform','model_gen']))
        self.writer.assert_not_called()
        self.globals['GameObjectData'].assert_not_called()

    def test_report_failure_stops_before_image_or_upload_stage(self):
        self.writer.side_effect = audit.AssociationError('private output inside dist')
        with self.assertRaises(audit.AssociationError):
            self.run_build(['download','extract'])
        self.globals['generate_images'].assert_not_called()
        self.globals['sync'].assert_not_called()

    def prepare_extraction(self, *, rkm=True, extra_files=None):
        source = self.root/'archive-input'
        write_fixture(source)
        self.transformer.gameObjectData = write_native_tables(source)
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

    def test_rkm_failure_stops_before_generation_upload_and_dlc_update(self):
        # The second case verifies failures are not lost if extraction later
        # raises outside the per-file handler (for example during atlas work).
        for atlas_error in (False, True):
            with self.subTest(atlas_error=atlas_error):
                self.prepare_extraction(extra_files={'atlas/broken.texatlas': b'synthetic'}
                                        if atlas_error else {})
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
                if atlas_error:
                    atlas.assert_called_once_with(self.root/'raw/extracted/atlas/broken.texatlas')
                else:
                    atlas.assert_not_called()
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

    def test_skip_model_gen_keeps_existing_rkm_error_tolerance(self):
        self.prepare_extraction()
        original = ARK.extract

        def inject(ark, filename, *args, **kwargs):
            if filename == 'flag.rkm':
                raise OSError('synthetic RKM extraction failure')
            return original(ark, filename, *args, **kwargs)

        with patch.object(ARK, 'extract', inject), patch.object(extractor, 'console', Console(quiet=True)):
            self.assertTrue(self.run_build(['download', 'model_gen']))
        self.transformer.start.assert_called_once()
        self.writer.assert_not_called()
        self.globals['generate_images'].assert_called_once()
