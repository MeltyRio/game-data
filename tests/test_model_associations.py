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

    def test_discovery_still_rejects_unsafe_or_non_ascii_embedded_names(self):
        path = self.folder / 'flag.anim'
        valid = path.read_bytes()
        for name in (b'', b'../flag', b'flag\xff'):
            with self.subTest(name=name):
                path.write_bytes(valid[:16] + name.ljust(64, b'\0') + valid[80:])
                self.assertFalse(self.row()['references_resolved'])


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

    def test_public_owner_does_not_create_an_unregistered_outfit_relationship(self):
        self.add_outfit()
        self.objects['Pony']['Pony_Test']['Sets']['Sets'] = []
        result = audit.discover_associations(self.objects, self.folder,
            outfit_owners={'Set_Test': 'Pony_Test'})
        self.assertFalse(result['identities']['Set_Test']['references_resolved'])


if __name__ == '__main__':
    unittest.main()
