"""Owner-scoped wardrobe assets keep existing previews and upload closure."""
import copy
import hashlib
import json
import struct
import unittest
from unittest.mock import patch

from bootstrap import ROOT
from game_data import model_gen
from game_data.model_paths import asset_digest, is_asset_path
from game_data.model_gen.wardrobe import resolve_wardrobe
import test_model_generation as generation_fixture
from test_model_sync import load_sync, MemoryClient


def document(data):
    size, = struct.unpack_from('<I', data, 12)
    return json.loads(data[20:20 + size])


class WardrobeAssets(unittest.TestCase):
    setUp = generation_fixture.ModelGeneration.setUp
    generate = generation_fixture.ModelGeneration.generate
    catalogue = generation_fixture.ModelGeneration.catalogue
    prepare_wardrobe = generation_fixture.ModelGeneration.prepare_wardrobe

    def test_owner_bundle_preserves_normal_preview_and_shared_animations(self):
        self.prepare_wardrobe()
        convert = model_gen.convert_native
        def ordinary(*args, **kwargs):
            kwargs.pop('wardrobe', None)
            return convert(*args, **kwargs)
        identities = {'Synthetic_Flag', 'Synthetic_Outfit'}
        with patch.object(model_gen, 'convert_native', ordinary):
            self.generate(public_identities=identities)
        before = self.catalogue()
        original = {row['path']: (self.dist/row['path']).read_bytes() for row in before['models'].values()}
        with patch.object(model_gen, 'load_model', wraps=model_gen.load_model) as parse:
            report = self.generate(public_identities=identities)
        rows = self.catalogue()['models']
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(report['generation']['cache']['animations']['decoded'], 1)
        for identity, baseline in before['models'].items():
            self.assertEqual({k:v for k,v in rows[identity].items() if k != 'wardrobe'}, baseline)
            self.assertEqual((self.dist/baseline['path']).read_bytes(), original[baseline['path']])
        self.assertNotIn('wardrobe', rows['Synthetic_Outfit'])
        ref = rows['Synthetic_Flag']['wardrobe']
        self.assertEqual(ref['path'], f"models/3d/Synthetic_Flag_wardrobe_{ref['sha256']}.glb")
        self.assertTrue(is_asset_path(ref['path']))
        self.assertEqual(asset_digest(ref['path']), ref['sha256'])
        data = (self.dist/ref['path']).read_bytes()
        self.assertEqual(ref['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(ref['bytes'], len(data))
        self.assertEqual(ref['rigKey'], rows['Synthetic_Flag']['rigKey'])
        glb = document(data)
        profile = glb['extras']['atpWardrobe']
        self.assertEqual(profile['defaults'], {'Body':'body0', 'Mane':'mane0', 'Tail':'tail0'})
        self.assertEqual(profile['sets']['Synthetic_Outfit'], {'Body':'body1'})
        self.assertEqual(profile['parts']['body1'], {'slot':'Body', 'meshes':[2], 'linked':'tail1'})
        self.assertEqual(profile['parts']['tail1'], {'slot':'Tail', 'meshes':[5]})
        selectable = {pid for slots in profile['sets'].values() for pid in slots.values()}
        self.assertNotIn('tail1', selectable)
        hidden = glb['extras']['atpInteractions']['hiddenNodes']
        self.assertEqual({glb['nodes'][i]['extras']['atpNativeMeshIndex'] for i in hidden}, {2,5})
        self.assertEqual(glb['extras']['atpRig'], document(original[rows['Synthetic_Flag']['path']])['extras']['atpRig'])

    def test_wardrobe_content_key_is_reused_and_changes_with_its_parts(self):
        self.prepare_wardrobe()
        self.generate(public_identities={'Synthetic_Flag'})
        before = self.catalogue()['models']['Synthetic_Flag']
        path = self.dist/before['wardrobe']['path']
        data, stamp = path.read_bytes(), path.stat().st_mtime_ns
        report = self.generate(public_identities={'Synthetic_Flag'})
        self.assertEqual(self.catalogue()['models']['Synthetic_Flag'], before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(report['generation']['cache']['animations']['compiled'], 0)

        self.objects['PonyPart']['body1']['PonyPart']['LinkedPart'] = ''
        self.generate(public_identities={'Synthetic_Flag'})
        after = self.catalogue()['models']['Synthetic_Flag']
        self.assertNotEqual(after['wardrobe']['sha256'], before['wardrobe']['sha256'])
        self.assertNotEqual(after['wardrobe']['path'], before['wardrobe']['path'])
        self.assertEqual(after['wardrobe']['path'],
                         f"models/3d/Synthetic_Flag_wardrobe_{after['wardrobe']['sha256']}.glb")
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(after['animations'], before['animations'])

    def test_missing_exact_mesh_is_empty_and_alternative_context_is_not_invented(self):
        from game_data.model_gen.wardrobe import interactive_wardrobe
        self.prepare_wardrobe()
        resolved = resolve_wardrobe(self.objects['Pony']['Synthetic_Flag'], self.objects)['provenance']
        names = ['flag', 'body0', 'body1', 'mane0', 'tail0', 'uncontrolled_trim']
        profile = interactive_wardrobe(resolved, names)
        self.assertEqual(profile['parts']['tail1']['meshes'], [])
        self.assertEqual(profile['sets']['Synthetic_Outfit'], {'Body':'body1'})
        resolved['alternative_only_sets'] = ['Synthetic_Outfit']
        profile = interactive_wardrobe(resolved, names)
        self.assertNotIn('Synthetic_Outfit', profile['sets'])
        self.assertNotIn('body1', profile['parts'])
        self.assertNotIn('tail1', profile['parts'])

    def test_eye_or_prop_overlap_disables_only_optional_wardrobe(self):
        self.prepare_wardrobe()
        convert = model_gen.convert_native
        def overlap(*args, **kwargs):
            kwargs.update(retained_meshes=[*kwargs['visible_meshes'], 'body1'],
                          interactions={'version':1, 'clickAnimations':[]},
                          model_interactions={'version':1, 'subObjects':[2]},
                          animation_events={}, event_approvals={})
            return convert(*args, **kwargs)
        with patch.object(model_gen, 'convert_native', overlap):
            report = self.generate(public_identities={'Synthetic_Flag'})
        self.assertIn('Synthetic_Flag', self.catalogue()['models'])
        self.assertNotIn('wardrobe', self.catalogue()['models']['Synthetic_Flag'])
        self.assertIn('overlap', report['identities']['Synthetic_Flag']['conversion_provenance']['wardrobe_unavailable'])

    def test_sync_includes_wardrobe_and_rejects_missing_or_incompatible_dependency(self):
        self.prepare_wardrobe()
        self.generate(public_identities={'Synthetic_Flag'})
        wire = json.loads((self.dist/'models_3d.json').read_text())
        ref = wire['models']['Synthetic_Flag']['wardrobe']
        prepare = load_sync(MemoryClient())['prepare_model_upload']
        _, assets = prepare(self.dist)
        self.assertIn(ref['path'], assets)
        self.assertEqual(assets[ref['path']]['cache_control'], 'max-age=2592000')
        for invalid in [
            f"models/3d/Other_wardrobe_{ref['sha256']}.glb",
            f"models/3d/Synthetic_Flag_wardrobe_{'0'*64}.glb",
            f"models/3d/../Synthetic_Flag_wardrobe_{ref['sha256']}.glb",
            f"models/3d/Synthetic_Flag%2F_wardrobe_{ref['sha256']}.glb",
        ]:
            changed = copy.deepcopy(wire)
            changed['models']['Synthetic_Flag']['wardrobe']['path'] = invalid
            (self.dist/'models_3d.json').write_text(json.dumps(changed))
            with self.subTest(path=invalid), self.assertRaisesRegex(ValueError, 'identity, size or path'):
                prepare(self.dist)

        changed = copy.deepcopy(wire)
        changed['models']['Synthetic_Flag']['wardrobe']['rigKey'] = '0'*64
        (self.dist/'models_3d.json').write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'wardrobe'):
            prepare(self.dist)
        (self.dist/'models_3d.json').write_text(json.dumps(wire))
        (self.dist/ref['path']).unlink()
        with self.assertRaisesRegex(ValueError, 'Missing or external'):
            prepare(self.dist)


if __name__ == '__main__':
    unittest.main()
