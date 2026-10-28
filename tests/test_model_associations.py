"""Self-authored sources only; these tests do not download or publish assets."""
import copy
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

    def test_animation_metadata_discovery_does_not_decode_frames(self):
        from game_data.model_gen.converter.native_animation import read_animation
        path = self.folder / 'flag.anim'
        raw = bytearray(path.read_bytes())
        struct.pack_into('<f', raw, 92 + 16, float('nan'))
        path.write_bytes(raw)
        result = self.run_audit()
        self.assertTrue(result['identities']['Pony_Test']['references_resolved'])
        self.assertEqual(result['animation_libraries']['flag.anim'],
                         {'file': 'flag.anim', 'bone_count': 2, 'frame_count': 46,
                          'frame_type': 0, 'embedded_name': 'flag'})
        with self.assertRaisesRegex(ValueError, 'Non-finite'):
            read_animation(path)

    def test_invalid_animation_headers_are_association_errors(self):
        path = self.folder / 'flag.anim'
        valid = path.read_bytes()
        for raw in (valid[:91], b'BROKEN!!' + valid[8:], valid[:-1], valid + b'\0'):
            with self.subTest(bytes=len(raw)):
                path.write_bytes(raw)
                with self.assertRaises(audit.AssociationError):
                    audit._animation(path)
                self.assertFalse(self.row()['references_resolved'])

    def test_embedded_names_are_metadata_not_resource_stems(self):
        path = self.folder / 'flag.anim'
        valid = path.read_bytes()
        for name in (b'', b'art/flag', b'../flag'):
            with self.subTest(name=name):
                path.write_bytes(valid[:16] + name.ljust(64, b'\0') + valid[80:])
                row = self.row()
                self.assertTrue(row['references_resolved'])
                self.assertEqual(row['csv']['file'], 'flag.csv')
                self.assertEqual(row['csv_source']['embedded_name'], name.decode('ascii'))

    def test_discovery_still_rejects_non_ascii_embedded_names(self):
        path = self.folder / 'flag.anim'
        valid = path.read_bytes()
        path.write_bytes(valid[:16] + b'flag\xff'.ljust(64, b'\0') + valid[80:])
        self.assertFalse(self.row()['references_resolved'])

    def test_discovery_still_rejects_unsafe_declared_resource_stems(self):
        for field, prop in (('Model', 'MediumLOD'), ('Animation', 'Rig_MediumLOD')):
            for name in ('', 'art/flag', '../flag'):
                with self.subTest(field=field, name=name):
                    self.objects['Pony']['Pony_Test'] = pony()
                    self.objects['Pony']['Pony_Test'][field][prop] = name
                    row = self.row()
                    self.assertFalse(row['references_resolved'])
                    self.assertIn('Missing or unsafe native resource stem', row['error'])


    def test_same_bone_count_candidate_not_used_for_missing_library(self):
        self.objects['Pony']['Pony_Test']['Animation']['Rig_MediumLOD'] = 'missing'
        row = self.row()
        self.assertFalse(row['references_resolved'])
        self.assertIn('missing.anim', row['error'])


    def add_outfit(self):
        self.objects['Pony']['Pony_Test']['Sets']['Sets'] = ['Set_Test']
        self.objects['PonySet']['Set_Test'] = {'Parts': {'Body':'Part_Body'}, 'PonySet': {'SubSets':[]}}
        self.objects['PonyPart']['Part_Body'] = {'PonyPart': {'Type':'Body','ModelName':'flag','LinkedPart':'Part_Tail'}}
        self.objects['PonyPart']['Part_Tail'] = {'PonyPart': {'Type':'Tail','ModelName':'flag','LinkedPart':''}}


    def test_ambiguous_outfit_owners_not_chosen_even_with_shared_rig(self):
        self.add_outfit()
        self.objects['Pony']['Pony_Other'] = copy.deepcopy(self.objects['Pony']['Pony_Test'])
        row = self.run_audit()['identities']['Set_Test']
        self.assertEqual(len(row['owners']), 2)
        self.assertFalse(row['references_resolved'])

    def test_public_outfit_owner_must_be_registered_by_native_tables(self):
        self.add_outfit()
        self.objects['Pony']['Pony_Other'] = copy.deepcopy(self.objects['Pony']['Pony_Test'])
        result = audit.discover_associations(self.objects, self.folder,
            outfit_owners={'Set_Test': 'Pony_Other'})
        row = result['identities']['Set_Test']
        self.assertTrue(row['references_resolved'])
        self.assertEqual(row['owner'], 'Pony_Other')
        self.assertEqual(row['owners'], ['Pony_Other', 'Pony_Test'])

        self.objects['Pony']['Pony_Unrelated'] = pony()
        for owner in ('Pony_Unrelated', 'Pony_Missing'):
            with self.subTest(owner=owner):
                result = audit.discover_associations(self.objects, self.folder,
                    outfit_owners={'Set_Test': owner})
                self.assertFalse(result['identities']['Set_Test']['references_resolved'])

    def test_registration_errors_are_scoped_to_the_requested_owner(self):
        self.add_outfit()
        original = copy.deepcopy(self.objects)
        for fault in ('missing', 'self', 'nested'):
            with self.subTest(fault=fault):
                self.objects = copy.deepcopy(original)
                broken = pony()
                broken['Sets']['Sets'] = ['Set_Broken']
                self.objects['Pony']['Pony_Broken'] = broken
                if fault != 'missing':
                    self.objects['PonySet']['Set_Broken'] = {
                        'PonySet': {'SubSets': ['Set_Broken' if fault == 'self' else 'Set_Child']}}
                if fault == 'nested':
                    self.objects['PonySet']['Set_Child'] = {'PonySet': {'SubSets': ['Set_Test']}}
                result = audit.discover_associations(self.objects, self.folder,
                    outfit_owners={'Set_Test': 'Pony_Test'})
                row = result['identities']['Set_Test']
                self.assertTrue(row['references_resolved'])
                self.assertEqual(row['owner'], 'Pony_Test')
                self.assertEqual(row['owners'], ['Pony_Test'])
                self.assertEqual(set(result['set_graph_errors']), {'Pony_Broken'})
                broken_row = result['identities']['Pony_Broken']
                self.assertFalse(broken_row['references_resolved'])
                self.assertEqual(broken_row['model'], 'flag.rk')

                # Unknown registrations can still conceal an inferred owner.
                self.assertFalse(self.run_audit()['identities']['Set_Test']['references_resolved'])
                result = audit.discover_associations(self.objects, self.folder,
                    outfit_owners={'Set_Test': 'Pony_Broken'})
                self.assertFalse(result['identities']['Set_Test']['references_resolved'])

    def test_registration_inspection_preserves_termination_order_and_strict_errors(self):
        from game_data.model_gen.wardrobe import inspect_registered_sets, registered_sets, WardrobeError
        self.add_outfit()
        owner, sets = self.objects['Pony']['Pony_Test'], self.objects['PonySet']
        sets['Set_Child'] = {'PonySet': {'SubSets': []}}
        sets['Set_Test']['PonySet']['SubSets'] = ['Set_Child']
        owner['Sets']['Sets'] = ['Set_Test', '', 'Missing_Later']
        self.assertEqual(inspect_registered_sets(owner, sets), (['Set_Test', 'Set_Child'], None))
        self.assertEqual(registered_sets(owner, sets), ['Set_Test', 'Set_Child'])

        owner['Sets']['Sets'] = ['Set_Test', 'Missing_Later']
        sets['Set_Test']['PonySet']['SubSets'] = ['', 'Set_Child']
        declared, error = inspect_registered_sets(owner, sets)
        self.assertEqual(declared, ['Set_Test', 'Set_Child', 'Missing_Later'])
        self.assertEqual(error.code, 'missing_wardrobe_set')
        self.assertEqual(str(error), "Missing SubSets reference: ''")
        with self.assertRaises(WardrobeError) as caught:
            registered_sets(owner, sets)
        self.assertEqual((caught.exception.code, str(caught.exception)), (error.code, str(error)))

    def test_explicit_owner_still_requires_target_dependencies(self):
        self.add_outfit()
        original = copy.deepcopy(self.objects)
        for fault in ('owner_library', 'owner_registration', 'part', 'linked_part'):
            with self.subTest(fault=fault):
                self.objects = copy.deepcopy(original)
                owner = self.objects['Pony']['Pony_Test']
                if fault == 'owner_library':
                    owner['Animation']['Rig_MediumLOD'] = 'missing'
                elif fault == 'owner_registration':
                    owner['Sets']['Sets'].append('Set_Missing')
                elif fault == 'part':
                    del self.objects['PonyPart']['Part_Body']
                else:
                    del self.objects['PonyPart']['Part_Tail']
                result = audit.discover_associations(self.objects, self.folder,
                    outfit_owners={'Set_Test': 'Pony_Test'})
                row = result['identities']['Set_Test']
                self.assertFalse(row['references_resolved'])
                self.assertIn('error', row)

    def test_public_owner_does_not_create_an_unregistered_outfit_relationship(self):
        self.add_outfit()
        self.objects['Pony']['Pony_Test']['Sets']['Sets'] = []
        result = audit.discover_associations(self.objects, self.folder,
            outfit_owners={'Set_Test': 'Pony_Test'})
        self.assertFalse(result['identities']['Set_Test']['references_resolved'])


if __name__ == '__main__':
    unittest.main()
