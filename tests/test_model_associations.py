"""Self-authored sources only; these tests do not download or publish assets."""
import copy
import json
from pathlib import Path
import struct
import tempfile
import unittest
from native_fixture import write_fixture

# Use the normal package hierarchy without initializing CLI/cloud services.
from bootstrap import ROOT
from game_data import model_associations as audit


def pony(model='flag', library='flag'):
    return {'Model': {'MediumLOD': model}, 'Animation': {'Rig_MediumLOD': library},
            'Conversation': {'IdleAnimation': 'rest'}, 'Sets': {'Sets': []}}


class Associations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'extracted'
        write_fixture(self.folder)
        self.objects = {'Pony': {'Pony_Test': pony()}, 'PonyPet': {}, 'PonySet': {}, 'PonyPart': {}}

    def run_audit(self):
        return audit.discover_associations(self.objects, self.folder)

    def row(self):
        return self.run_audit()['identities']['Pony_Test']

    def test_exact_declared_rk_anim_csv_with_real_luna_rk_header_parsers(self):
        result = self.run_audit()
        row = result['identities']['Pony_Test']
        self.assertTrue(row['references_resolved'])
        self.assertEqual(row['csv']['file'], 'flag.csv')
        self.assertEqual([c['name'] for c in result['csv_libraries']['flag.csv']['clips']], ['rest', 'wave'])
        self.assertFalse(row['converted'])
        self.assertFalse(result['conversion_enabled'])
        self.assertEqual(result['summary']['converted'], 0)
        self.assertIn('visibility_unverified', row['blockers'])

    def test_pet_uses_object_not_pony_model_fields(self):
        self.objects['PonyPet']['Pet_Test'] = {'Object': {'Model': 'flag', 'Animation': 'flag', 'AnimationIdle': 'rest'}}
        row = self.run_audit()['identities']['Pet_Test']
        self.assertTrue(row['references_resolved'])
        self.assertEqual(row['fields']['animation'], 'Object.Animation')

    def test_shared_library_keeps_distinct_object_identity(self):
        self.objects['Pony']['Pony_Other'] = pony()
        result = self.run_audit()
        self.assertEqual(len(result['identities']), 2)
        self.assertEqual(len(result['models']), 1)
        self.assertEqual(len(result['animation_libraries']), 1)

    def test_different_library_selected_by_field(self):
        (self.folder/'other.anim').write_bytes((self.folder/'flag.anim').read_bytes())
        (self.folder/'other.csv').write_text('other_idle,0,1,15\n')
        self.objects['Pony']['Pony_Test']['Animation']['Rig_MediumLOD'] = 'other'
        row = self.row()
        self.assertEqual(row['animation'], 'other.anim')
        self.assertEqual(self.run_audit()['csv_libraries']['other.csv']['clips'][0]['name'], 'other_idle')

    def test_misleading_embedded_name_never_selects_another_csv(self):
        path = self.folder/'flag.anim'
        data = bytearray(path.read_bytes())
        data[16:80] = b'other'.ljust(64, b'\0')
        path.write_bytes(data)
        (self.folder/'other.csv').write_text('wrong,0,1,15\n')
        row = self.row()
        self.assertEqual(row['csv_source']['embedded_name'], 'other')
        self.assertEqual(row['csv']['file'], 'flag.csv')
        self.assertEqual(self.run_audit()['csv_libraries']['flag.csv']['clips'][0]['name'], 'rest')

    def test_missing_declared_csv_does_not_fall_back_to_embedded_csv(self):
        path = self.folder/'flag.anim'
        data = bytearray(path.read_bytes()); data[16:80] = b'other'.ljust(64,b'\0'); path.write_bytes(data)
        (self.folder/'other.csv').write_text('wrong,0,1,15\n')
        (self.folder/'flag.csv').unlink()
        row = self.row()
        self.assertIn('invalid_or_missing_csv', row['blockers'])
        self.assertNotIn('clips', row['csv'])

    def test_same_bone_count_candidate_not_used_for_missing_library(self):
        self.objects['Pony']['Pony_Test']['Animation']['Rig_MediumLOD'] = 'missing'
        row = self.row()
        self.assertFalse(row['references_resolved'])
        self.assertIn('missing.anim', row['error'])

    def test_valid_length_but_wrong_bone_count_is_rejected(self):
        path = self.folder/'flag.anim'; data = bytearray(path.read_bytes())
        struct.pack_into('<II', data, 80, 1, 92); path.write_bytes(data)
        row = self.row()
        self.assertFalse(row['references_resolved'])
        self.assertIn('bone counts differ', row['error'])

    def test_truncated_animation_is_diagnostic(self):
        path = self.folder/'flag.anim'; path.write_bytes(path.read_bytes()[:-1])
        self.assertIn('byte length', self.row()['error'])

    def test_unknown_animation_format_is_diagnostic(self):
        path = self.folder/'flag.anim'; data = bytearray(path.read_bytes()); struct.pack_into('<I',data,88,9); path.write_bytes(data)
        self.assertIn('Unsupported ANIM', self.row()['error'])

    def test_missing_model_is_diagnostic(self):
        (self.folder/'flag.rk').unlink()
        self.assertFalse(self.row()['references_resolved'])

    def test_missing_stem_is_not_inferred_from_id(self):
        del self.objects['Pony']['Pony_Test']['Model']['MediumLOD']
        self.assertFalse(self.row()['references_resolved'])

    def test_path_traversal_rejected(self):
        self.objects['Pony']['Pony_Test']['Model']['MediumLOD'] = '../flag'
        self.assertIn('unsafe', self.row()['error'])

    def test_dependency_symlink_outside_closure_rejected(self):
        target = Path(self.temp.name)/'outside.rk'
        (self.folder/'flag.rk').rename(target)
        (self.folder/'flag.rk').symlink_to(target)
        self.assertIn('leaves', self.row()['error'])

    def test_case_spelling_not_guessed(self):
        self.objects['Pony']['Pony_Test']['Model']['MediumLOD'] = 'FLAG'
        self.assertFalse(self.row()['references_resolved'])

    def test_missing_and_explicit_empty_eyes_remain_distinct(self):
        self.assertIsNone(self.row()['eyes'])
        self.objects['Pony']['Pony_Test']['Eyes'] = {'EyeSubObject_Open': []}
        self.assertEqual(self.row()['eyes'], {'EyeSubObject_Open': []})

    def test_missing_idle_not_replaced_with_first_csv_clip(self):
        del self.objects['Pony']['Pony_Test']['Conversation']
        row = self.row()
        self.assertIsNone(row['idle']['value'])
        self.assertIn('initial_pose_not_declared', row['blockers'])

    def test_unknown_idle_diagnostic(self):
        self.objects['Pony']['Pony_Test']['Conversation']['IdleAnimation'] = 'missing'
        self.assertIn('declared_idle_not_in_csv', self.row()['blockers'])

    def test_additional_object_animation_not_misread_as_library(self):
        self.objects['Pony']['Pony_Test']['AdditionalObject'] = {'Model':'companion','Animation':'idle'}
        result = self.run_audit()
        self.assertEqual(len(result['animation_libraries']), 1)
        self.assertIn('additional_actor_conversion_not_run', result['identities']['Pony_Test']['blockers'])

    def test_explicit_event_field_not_overwritten_by_rig(self):
        self.objects['Pony']['Pony_Test']['Animation']['AnimationEventsXML'] = 'different_events'
        self.assertEqual(self.row()['events'], 'different_events')
        self.assertIn('event_source_unverified', self.row()['blockers'])

    def add_outfit(self):
        self.objects['Pony']['Pony_Test']['Sets']['Sets'] = ['Set_Test']
        self.objects['PonySet']['Set_Test'] = {'Parts': {'Body':'Part_Body'}, 'PonySet': {'SubSets':[]}}
        self.objects['PonyPart']['Part_Body'] = {'PonyPart': {'Type':'Body','ModelName':'flag','LinkedPart':'Part_Tail'}}
        self.objects['PonyPart']['Part_Tail'] = {'PonyPart': {'Type':'Tail','ModelName':'flag','LinkedPart':''}}

    def test_linked_parts_resolve_exact_mesh_in_owner_rk(self):
        self.add_outfit()
        row = self.run_audit()['identities']['Set_Test']
        self.assertTrue(row['references_resolved'])
        self.assertEqual(set(row['parts']), {'Body','Tail'})
        self.assertEqual(row['model'], 'flag.rk')
        self.assertFalse(row['converted'])

    def test_ambiguous_outfit_owners_not_chosen_even_with_shared_rig(self):
        self.add_outfit()
        self.objects['Pony']['Pony_Other'] = copy.deepcopy(self.objects['Pony']['Pony_Test'])
        row = self.run_audit()['identities']['Set_Test']
        self.assertEqual(len(row['owners']), 2)
        self.assertFalse(row['references_resolved'])

    def test_unowned_outfit_not_matched_by_mesh_name(self):
        self.add_outfit(); self.objects['Pony']['Pony_Test']['Sets']['Sets'] = []
        self.assertFalse(self.run_audit()['identities']['Set_Test']['references_resolved'])

    def test_missing_outfit_mesh_not_substituted(self):
        self.add_outfit(); self.objects['PonyPart']['Part_Tail']['PonyPart']['ModelName'] = 'missing'
        row = self.run_audit()['identities']['Set_Test']
        self.assertTrue(row['references_resolved'])
        self.assertEqual(row['absent_declared_meshes'], ['missing'])
        self.assertEqual(row['parts']['Tail']['mesh'], 'missing')

    def test_part_cycle_is_diagnostic(self):
        self.add_outfit(); self.objects['PonyPart']['Part_Tail']['PonyPart']['LinkedPart'] = 'Part_Body'
        self.assertIn('Cyclic LinkedPart', self.run_audit()['identities']['Set_Test']['error'])

    def test_sets_stop_at_first_empty_before_missing_declaration(self):
        self.add_outfit()
        self.objects['Pony']['Pony_Test']['Sets']['Sets'] = ['Set_Test', '', 'Missing']
        self.assertTrue(self.run_audit()['identities']['Set_Test']['references_resolved'])

    def test_subset_cycle_is_diagnostic(self):
        self.add_outfit(); self.objects['PonySet']['Set_Test']['PonySet']['SubSets'] = ['Set_Test']
        self.assertIn('Cyclic SubSets', self.row()['error'])

    def test_conflicting_linked_slots_rejected(self):
        self.add_outfit(); self.objects['PonyPart']['Part_Tail']['PonyPart']['Type'] = 'Body'
        self.assertIn('Conflicting', self.run_audit()['identities']['Set_Test']['error'])

    def test_invalid_csv_fps_range_and_conflicting_duplicates(self):
        for text in ['rest,0,0,nan\n','rest,0,46,15\n','rest,0,0,0\n','rest,0,0,15\nrest,1,1,15\n','bad\n']:
            with self.subTest(text=text):
                (self.folder/'flag.csv').write_text(text)
                self.assertIn('invalid_or_missing_csv', self.row()['blockers'])

    def test_identical_csv_duplicates_merge_only_identical_definition(self):
        (self.folder/'flag.csv').write_text('rest,0,0,15\nrest,0,0,15\n')
        self.assertEqual(self.row()['csv']['clip_count'], 1)

    def test_unrelated_invalid_clip_does_not_hide_valid_declared_library(self):
        (self.folder/'flag.csv').write_text('rest,0,0,15\nbroken,0,46,15\nwave,0,45,15\n')
        self.objects['Pony']['Pony_Other'] = pony()
        result = self.run_audit()
        library = result['csv_libraries']['flag.csv']
        self.assertEqual([c['name'] for c in library['clips']], ['rest', 'wave'])
        self.assertEqual(library['excluded_clip_names'], ['broken'])
        self.assertEqual(library['diagnostics'][0]['line'], 2)
        for row in result['identities'].values():
            self.assertTrue(row['references_resolved'])
            self.assertEqual(row['csv']['diagnostic_count'], 1)
            self.assertNotIn('diagnostics', row['csv'])
            self.assertNotIn('excluded_clip_names', row['csv'])

    def test_conflicting_clip_cannot_survive_as_first_or_last_choice(self):
        (self.folder/'flag.csv').write_text('rest,0,0,15\nwave,0,44,15\nwave,0,45,15\nwave,0,44,15\n')
        result = self.run_audit()
        self.assertTrue(result['identities']['Pony_Test']['references_resolved'])
        library = result['csv_libraries']['flag.csv']
        self.assertEqual([c['name'] for c in library['clips']], ['rest'])
        self.assertEqual(library['excluded_clip_names'], ['wave'])

    def test_invalid_rest_is_still_a_blocker_when_other_clips_are_valid(self):
        (self.folder/'flag.csv').write_text('rest,0,0,15\nrest,0,0,nan\nwave,0,45,15\n')
        result = self.run_audit()
        row = result['identities']['Pony_Test']
        self.assertIn('declared_idle_not_in_csv', row['blockers'])
        self.assertEqual([c['name'] for c in result['csv_libraries']['flag.csv']['clips']], ['wave'])

    def test_first_source_record_survives_invalid_row_and_duplicate_filtering(self):
        for text in ['first,0,99,15\nrest,0,0,15\n',
                     'first,0,0,15\nrest,0,0,15\nfirst,1,1,15\n']:
            with self.subTest(text=text):
                (self.folder/'flag.csv').write_text(text)
                result = self.run_audit()
                library = result['csv_libraries']['flag.csv']
                self.assertEqual(library['first_declared_name'], 'first')
                self.assertEqual(library['first_declared_line'], 1)
                self.assertEqual([clip['name'] for clip in library['clips']], ['rest'])

    def test_report_written_only_to_explicit_local_path_no_glb(self):
        for name in ['gameobjectdata.xml','gameobjectcategorydata.xml','shopdata.xml']:
            (self.folder/name).write_text('<fixture/>')
        output = Path(self.temp.name)/'raw/audit.json'
        audit.write_association_report(self.objects,self.folder,output,public_dir=Path(self.temp.name)/'dist')
        result = json.loads(output.read_text())
        self.assertFalse(result['campaign']['present'])
        self.assertEqual(result['summary']['converted'], 0)
        self.assertEqual(list(Path(self.temp.name).rglob('*.glb')), [])
        self.assertFalse((Path(self.temp.name)/'dist').exists())

    def test_empty_additional_object_is_not_supported_feature_claim(self):
        self.objects['Pony']['Pony_Test']['AdditionalObject'] = {'Model':'','Animation':'','Scale':0.0}
        self.assertNotIn('additional_actor_conversion_not_run', self.row()['blockers'])

    def test_missing_csv_not_counted_as_complete_reference_chain(self):
        (self.folder/'flag.csv').unlink()
        result = self.run_audit()
        self.assertEqual(result['summary']['references_resolved'], 0)
        self.assertEqual(result['summary']['model_animation_resolved'], 1)

    def test_cross_category_identity_collision_is_not_overwritten(self):
        self.objects['PonyPet']['Pony_Test'] = {'Object':{'Model':'flag','Animation':'flag'}}
        with self.assertRaisesRegex(audit.AssociationError, 'reused'):
            self.run_audit()

    def test_private_report_public_output_guard_and_symlink(self):
        public = Path(self.temp.name)/'dist'
        public.mkdir()
        alias = Path(self.temp.name)/'raw-alias'
        alias.symlink_to(public, target_is_directory=True)
        for folder in [public, alias]:
            with self.subTest(folder=folder), self.assertRaisesRegex(audit.AssociationError,'outside'):
                audit.write_association_report(self.objects,self.folder,folder/'audit.json',public_dir=public)
        self.assertEqual(list(public.iterdir()), [])

    def test_real_luna_gameobjectdata_parser_to_audit(self):
        from luna_kit.gameobjectdata import GameObjectData
        (self.folder/'gameobjectcategorydata.xml').write_text('''<Categories><GameObjectCategory Name="Pony">
<Parameter Name="Model"><Attribute Name="MediumLOD" Type="string"/></Parameter>
<Parameter Name="Animation"><Attribute Name="Rig_MediumLOD" Type="string"/></Parameter>
<Parameter Name="Conversation" Optional="1"><Attribute Name="IdleAnimation" Type="string"/></Parameter>
<Parameter Name="Sets"><Attribute Name="Sets" Type="stringWithDefault" Array="2"/></Parameter>
</GameObjectCategory></Categories>''')
        (self.folder/'gameobjectdata.xml').write_text('''<GameObjects><Category ID="Pony"><GameObject ID="Pony_Test"><Model MediumLOD="flag"/><Animation Rig_MediumLOD="flag"/><Conversation IdleAnimation="rest"/></GameObject></Category></GameObjects>''')
        (self.folder/'shopdata.xml').write_text('<ShopData/>')
        data = GameObjectData(self.folder/'gameobjectdata.xml')
        self.assertTrue(audit.discover_associations(data,self.folder)['identities']['Pony_Test']['references_resolved'])


if __name__ == '__main__':
    unittest.main()
