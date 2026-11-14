"""Shared-model wardrobe assets keep defaults, every set and upload closure."""
import copy
import hashlib
import json
import struct
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from bootstrap import ROOT
from game_data import model_gen
from game_data.model_paths import asset_digest, is_asset_path
from game_data.model_gen.wardrobe import resolve_wardrobe
from luna_kit.gameobjectdata import GameObjectData
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

    def prepare_partial_owner(self):
        self.prepare_wardrobe(second_owner=True)
        path = self.native/'gameobjectdata.xml'
        tree = ET.parse(path)
        sets = tree.find("./Category[@ID='Pony']/GameObject[@ID='Synthetic_Flag']/Sets/Sets")
        ET.SubElement(sets, 'Item', Value='Synthetic_OtherOutfit')
        outfit = ET.SubElement(tree.find("./Category[@ID='PonySet']"), 'GameObject', ID='Synthetic_OtherOutfit')
        ET.SubElement(outfit, 'Parts', Body='other_body', Mane='', Tail='')
        ET.SubElement(ET.SubElement(outfit, 'PonySet'), 'SubSets')
        part = ET.SubElement(tree.find("./Category[@ID='PonyPart']"), 'GameObject', ID='other_body')
        ET.SubElement(part, 'PonyPart', Type='Body', ModelName='uncontrolled_trim', LinkedPart='')
        tree.write(path, encoding='utf-8')
        self.objects = GameObjectData(path, self.native/'shopdata.xml', self.native/'gameobjectcategorydata.xml')

    def test_shared_costume_keeps_its_public_owner_without_extra_clothing(self):
        self.prepare_partial_owner()
        report = self.generate(public_identities={'Synthetic_Outfit'},
            outfit_owners={'Synthetic_Outfit':'Synthetic_SecondOwner'})

        data = (self.dist/self.catalogue()['models']['Synthetic_Outfit']['path']).read_bytes()
        glb = document(data)
        names = {primitive['extras']['nativeMeshName'] for mesh in glb['meshes'] for primitive in mesh['primitives']}
        self.assertEqual(names, {'flag', 'body1', 'mane0', 'tail1'})
        row = report['identities']['Synthetic_Outfit']
        self.assertEqual(row['owners'], ['Synthetic_Flag', 'Synthetic_SecondOwner'])
        self.assertEqual(row['owner'], 'Synthetic_SecondOwner')
        self.assertEqual(row['owner_source'], 'public.costume.pony')

    def test_shared_costume_uses_an_owner_whose_eye_controls_resolve(self):
        for matching_open in [False, True]:
            with self.subTest(matching_open=matching_open):
                self.prepare_wardrobe(second_owner=True,
                    extra_mesh_names=['eyes_open', 'eyes_shut', 'eyes_happy', 'eyes_frown'])
                path = self.native/'gameobjectdata.xml'
                tree = ET.parse(path)
                for identity in ['Synthetic_Flag', 'Synthetic_SecondOwner']:
                    eyes = tree.find(f"./Category[@ID='Pony']/GameObject[@ID='{identity}']/Eyes")
                    for state, mesh in [('Open','eyes_open'), ('Closed','eyes_shut'), ('Happy','eyes_happy'), ('Frown','eyes_frown')]:
                        group = eyes.find('EyeSubObject_' + state)
                        group.clear()
                        stale = identity == 'Synthetic_SecondOwner' and (state != 'Open' or not matching_open)
                        ET.SubElement(group, 'Item', Value=('stale_' if stale else '') + mesh)
                tree.write(path, encoding='utf-8')
                self.objects = GameObjectData(path, self.native/'shopdata.xml', self.native/'gameobjectcategorydata.xml')

                report = self.generate(public_identities={'Synthetic_Outfit'},
                    outfit_owners={'Synthetic_Outfit':'Synthetic_SecondOwner'})

                glb = document((self.dist/self.catalogue()['models']['Synthetic_Outfit']['path']).read_bytes())
                shown = {glb['nodes'][index]['mesh'] for index in glb['scenes'][0]['nodes'] if 'mesh' in glb['nodes'][index]}
                names = {primitive['extras']['nativeMeshName'] for index in shown for primitive in glb['meshes'][index]['primitives']}
                self.assertEqual(names, {'flag', 'body1', 'mane0', 'tail1', 'uncontrolled_trim', 'eyes_open'})
                self.assertEqual(report['identities']['Synthetic_Outfit']['owner'], 'Synthetic_Flag')

    def test_equivalent_eyeless_contexts_keep_the_public_owner(self):
        self.prepare_wardrobe(second_owner=True)
        path = self.native/'gameobjectdata.xml'
        tree = ET.parse(path)
        for identity in ['Synthetic_Flag', 'Synthetic_SecondOwner']:
            opened = tree.find(f"./Category[@ID='Pony']/GameObject[@ID='{identity}']/Eyes/EyeSubObject_Open/Item")
            opened.set('Value', 'missing_eye')
        tree.write(path, encoding='utf-8')
        self.objects = GameObjectData(path, self.native/'shopdata.xml', self.native/'gameobjectcategorydata.xml')
        report = self.generate(public_identities={'Synthetic_Unrelated', 'Synthetic_Outfit'},
            outfit_owners={'Synthetic_Outfit':'Synthetic_SecondOwner'})
        self.assertIn('Synthetic_Outfit', self.catalogue()['models'])
        self.assertEqual(report['identities']['Synthetic_Outfit']['owner'], 'Synthetic_SecondOwner')

    def test_shared_context_does_not_bypass_public_or_render_owner_overrides(self):
        self.prepare_partial_owner()
        self.objects['Pony']['Synthetic_SecondOwner']['Eyes']['EyeSubObject_Open'] = ['missing_eye']
        for identity in ['Synthetic_SecondOwner', 'Synthetic_Flag']:
            with self.subTest(identity=identity):
                report = self.generate(public_identities={'Synthetic_Unrelated', 'Synthetic_Outfit'},
                    outfit_owners={'Synthetic_Outfit':'Synthetic_SecondOwner'},
                    campaign={'game_object_data':{'Pony':{identity:{'Model':{'MediumLOD':'changed'}}}}})
                self.assertNotIn('Synthetic_Outfit', self.catalogue()['models'])
                self.assertEqual(report['generation']['excluded']['Synthetic_Outfit']['code'],
                                 'campaign_visual_override_unverified')

    def test_shared_context_preserves_specific_wardrobe_diagnostics(self):
        self.prepare_wardrobe(second_owner=True)
        self.objects['PonySet']['Synthetic_DefaultSet']['Parts']['Mane'] = ''
        report = self.generate(public_identities={'Synthetic_Unrelated', 'Synthetic_Outfit'},
            outfit_owners={'Synthetic_Outfit':'Synthetic_SecondOwner'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Outfit']['code'],
                         'incomplete_default_wardrobe')

    def test_shared_costume_preserves_a_complete_public_owner(self):
        self.prepare_wardrobe(second_owner=True)
        report = self.generate(public_identities={'Synthetic_Outfit'},
            outfit_owners={'Synthetic_Outfit':'Synthetic_SecondOwner'})
        row = report['identities']['Synthetic_Outfit']
        self.assertEqual(row['owner'], 'Synthetic_SecondOwner')
        self.assertEqual(row['owner_source'], 'public.costume.pony')

    def test_shared_model_keeps_all_sets_and_only_its_first_default_visible(self):
        self.prepare_partial_owner()
        report = self.generate(public_identities={'Synthetic_Flag', 'Synthetic_SecondOwner'})
        for identity in ['Synthetic_Flag', 'Synthetic_SecondOwner']:
            with self.subTest(identity=identity):
                row = self.catalogue()['models'][identity]
                glb = document((self.dist/row['path']).read_bytes())
                shown = {glb['nodes'][index]['mesh'] for index in glb['scenes'][0]['nodes'] if 'mesh' in glb['nodes'][index]}
                if shown:
                    meshes = [glb['meshes'][index] for index in shown]
                else:
                    meshes = glb['meshes']
                names = {primitive['extras']['nativeMeshName'] for mesh in meshes for primitive in mesh['primitives']}
                self.assertEqual(names, {'flag', 'body0', 'mane0', 'tail0'})
                wardrobe = document((self.dist/row['wardrobe']['path']).read_bytes())['extras']['atpWardrobe']
                self.assertEqual(set(wardrobe['sets']), {'Synthetic_DefaultSet','Synthetic_Outfit','Synthetic_OtherOutfit'})
                self.assertEqual(wardrobe['defaults'], {'Body':'body0','Mane':'mane0','Tail':'tail0'})
                self.assertEqual(report['identities'][identity]['selection']['wardrobe']['default_set'], 'Synthetic_DefaultSet')

    def test_same_model_unions_disjoint_sets_without_replacing_an_owner(self):
        self.prepare_partial_owner()
        path = self.native/'gameobjectdata.xml'
        tree = ET.parse(path)
        first = tree.find("./Category[@ID='Pony']/GameObject[@ID='Synthetic_Flag']/Sets/Sets")
        first.remove(list(first)[-1])
        second = tree.find("./Category[@ID='Pony']/GameObject[@ID='Synthetic_SecondOwner']/Sets/Sets")
        list(second)[1].set('Value', 'Synthetic_OtherOutfit')
        tree.write(path, encoding='utf-8')
        self.objects = GameObjectData(path, self.native/'shopdata.xml', self.native/'gameobjectcategorydata.xml')
        report = self.generate(public_identities={'Synthetic_Flag','Synthetic_SecondOwner','Synthetic_Outfit'},
            outfit_owners={'Synthetic_Outfit':'Synthetic_Flag'})
        for identity in ['Synthetic_Flag','Synthetic_SecondOwner']:
            row = self.catalogue()['models'][identity]
            profile = document((self.dist/row['wardrobe']['path']).read_bytes())['extras']['atpWardrobe']
            self.assertEqual(set(profile['sets']), {'Synthetic_DefaultSet','Synthetic_Outfit','Synthetic_OtherOutfit'})
            glb = document((self.dist/row['path']).read_bytes())
            names = {p['extras']['nativeMeshName'] for mesh in glb['meshes'] for p in mesh['primitives']}
            self.assertEqual(names, {'flag','body0','mane0','tail0'})
        self.assertEqual(report['identities']['Synthetic_Outfit']['owner'], 'Synthetic_Flag')

    def test_shared_model_sets_do_not_depend_on_animation_library_identity(self):
        self.prepare_partial_owner()
        for suffix in ['anim','csv']:
            (self.native/f'other.{suffix}').write_bytes((self.native/f'flag.{suffix}').read_bytes())
        self.objects['Pony']['Synthetic_SecondOwner']['Animation']['Rig_MediumLOD'] = 'other'
        self.generate(public_identities={'Synthetic_SecondOwner'})
        row = self.catalogue()['models']['Synthetic_SecondOwner']
        profile = document((self.dist/row['wardrobe']['path']).read_bytes())['extras']['atpWardrobe']
        self.assertEqual(set(profile['sets']), {'Synthetic_DefaultSet','Synthetic_Outfit','Synthetic_OtherOutfit'})

    def test_shared_sets_keep_cross_slot_mesh_conflict_checks(self):
        self.prepare_partial_owner()
        path = self.native/'gameobjectdata.xml'
        tree = ET.parse(path)
        first = tree.find("./Category[@ID='Pony']/GameObject[@ID='Synthetic_Flag']/Sets/Sets")
        first.remove(list(first)[-1])
        second = tree.find("./Category[@ID='Pony']/GameObject[@ID='Synthetic_SecondOwner']/Sets/Sets")
        second.clear()
        for identity in ['Foreign_Default','Synthetic_OtherOutfit']:ET.SubElement(second,'Item',Value=identity)
        default = ET.SubElement(tree.find("./Category[@ID='PonySet']"),'GameObject',ID='Foreign_Default')
        ET.SubElement(default,'Parts',Body='body0',Mane='foreign_mane',Tail='tail0')
        ET.SubElement(ET.SubElement(default,'PonySet'),'SubSets')
        part = ET.SubElement(tree.find("./Category[@ID='PonyPart']"),'GameObject',ID='foreign_mane')
        ET.SubElement(part,'PonyPart',Type='Mane',ModelName='uncontrolled_trim',LinkedPart='')
        tree.find("./Category[@ID='PonyPart']/GameObject[@ID='other_body']/PonyPart").set('ModelName','mane0')
        tree.write(path,encoding='utf-8')
        self.objects = GameObjectData(path,self.native/'shopdata.xml',self.native/'gameobjectcategorydata.xml')
        report = self.generate(public_identities={'Synthetic_Flag','Synthetic_Unrelated'})
        self.assertNotIn('Synthetic_Flag',self.catalogue()['models'])
        self.assertEqual(report['generation']['excluded']['Synthetic_Flag']['code'],
                         'conflicting_wardrobe_mesh_visibility')

    def test_shared_registration_checks_only_the_contributing_fields(self):
        self.prepare_partial_owner()
        for change, blocked in [({'Model':{'MediumLOD':'different'}},True),
                                ({'Sets':{'Sets':['Synthetic_DefaultSet']}},True),
                                ({'Model':{'Scale':2}},False),
                                ({'Animation':{'LeftIdleAnimationList':['rest']}},False),
                                ({'Eyes':{'EyeSubObject_Open':['missing_eye']}},False)]:
            with self.subTest(change=change):
                report = self.generate(public_identities={'Synthetic_SecondOwner','Synthetic_Unrelated'},
                    campaign={'game_object_data':{'Pony':{'Synthetic_Flag':change}}})
                if blocked:
                    self.assertNotIn('Synthetic_SecondOwner',self.catalogue()['models'])
                    self.assertEqual(report['generation']['excluded']['Synthetic_SecondOwner']['code'],
                                     'campaign_visual_override_unverified')
                else:self.assertIn('Synthetic_SecondOwner',self.catalogue()['models'])

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
