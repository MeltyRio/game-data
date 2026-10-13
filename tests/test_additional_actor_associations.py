"""Native companion associations are diagnostic, not compound viewer output."""
from pathlib import Path
import struct
import tempfile
import unittest

from bootstrap import ROOT
from game_data import model_associations as audit
from native_fixture import write_fixture


class AdditionalActorAssociations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        write_fixture(self.folder)
        for suffix in ('.rk', '.anim', '.csv'):
            (self.folder / ('companion' + suffix)).write_bytes((self.folder / ('flag' + suffix)).read_bytes())
        self.pony = {'Model': {'MediumLOD': 'flag'}, 'Animation': {'Rig_MediumLOD': 'flag'},
                     'Conversation': {'IdleAnimation': 'rest'},
                     'AdditionalObject': {'Model': 'companion', 'Animation': 'not_a_library_or_clip'}}
        self.objects = {'Pony': {'Pony_Test': self.pony}}

    def result(self):
        return audit.discover_associations(self.objects, self.folder)

    def row(self):
        return self.result()['identities']['Pony_Test']

    def test_native_model_field_resolves_independent_rk_anim_csv(self):
        row = self.row()
        actor = row['additional_actor']
        self.assertTrue(row['references_resolved'])
        self.assertTrue(actor['references_resolved'])
        self.assertEqual(actor['model'], 'companion.rk')
        self.assertEqual(actor['animation'], 'companion.anim')
        self.assertEqual(actor['csv']['file'], 'companion.csv')
        self.assertEqual(actor['declaration']['Animation'], 'not_a_library_or_clip')
        self.assertFalse(actor['renderer_supported'])
        self.assertFalse(row['converted'])
        self.assertIn('additional_actor_conversion_not_run', row['blockers'])
        self.assertNotIn('additional_actor_dependency_unresolved', row['blockers'])
        self.assertNotIn('initial_clip', actor)

    def test_shared_companion_preserves_each_owner(self):
        self.objects['Pony']['Pony_Other'] = dict(self.pony)
        result = self.result()
        self.assertEqual(result['summary']['additional_actors'], 2)
        self.assertEqual(result['summary']['additional_actor_references_resolved'], 2)
        self.assertEqual(set(result['animation_libraries']), {'flag.anim', 'companion.anim'})
        self.assertEqual(set(result['identities']), {'Pony_Test', 'Pony_Other'})

    def test_embedded_name_does_not_redirect_companion_csv(self):
        data = bytearray((self.folder / 'companion.anim').read_bytes())
        data[16:80] = b'flag'.ljust(64, b'\0')
        (self.folder / 'companion.anim').write_bytes(data)
        self.assertEqual(self.row()['additional_actor']['csv']['file'], 'companion.csv')

    def test_missing_companion_csv_is_separate_from_primary_chain(self):
        (self.folder / 'companion.csv').unlink()
        row = self.row()
        self.assertTrue(row['references_resolved'])
        self.assertFalse(row['additional_actor']['references_resolved'])
        self.assertIn('companion.csv', row['additional_actor']['error'])
        self.assertIn('additional_actor_dependency_unresolved', row['blockers'])

    def test_missing_companion_library_is_not_replaced_with_primary(self):
        (self.folder / 'companion.anim').unlink()
        actor = self.row()['additional_actor']
        self.assertFalse(actor['references_resolved'])
        self.assertIn('companion.anim', actor['error'])

    def test_companion_bone_mismatch_is_rejected(self):
        path = self.folder / 'companion.anim'
        data = bytearray(path.read_bytes())
        struct.pack_into('<II', data, 80, 1, 92)
        path.write_bytes(data)
        actor = self.row()['additional_actor']
        self.assertFalse(actor['references_resolved'])
        self.assertIn('bone counts differ', actor['error'])

    def test_companion_resource_spelling_is_exact(self):
        self.pony['AdditionalObject']['Model'] = 'COMPANION'
        self.assertFalse(self.row()['additional_actor']['references_resolved'])

    def test_animation_field_alone_does_not_create_native_actor(self):
        self.pony['AdditionalObject']['Model'] = ''
        row = self.row()
        self.assertNotIn('additional_actor', row)
        self.assertNotIn('additional_actor_conversion_not_run', row['blockers'])
        self.assertEqual(row['additional_object']['Animation'], 'not_a_library_or_clip')


if __name__ == '__main__':
    unittest.main()
