import copy
import unittest

from bootstrap import ROOT
from game_data.model_gen.wardrobe import WardrobeError, registered_sets, resolve_wardrobe


def fixture():
    owner = {'Sets': {'Sets': ['base', 'dress']}}
    objects = {'PonySet': {
        'base': {'Parts': {'Body': 'body0', 'Mane': 'mane0', 'Tail': 'tail0'}, 'PonySet': {'SubSets': []}},
        'dress': {'Parts': {'Body': 'body1', 'Mane': 'mane1', 'Tail': ''}, 'PonySet': {'SubSets': []}},
    }, 'PonyPart': {}}
    for slot in ('Body', 'Mane', 'Tail'):
        for suffix in ('0', '1'):
            pid = slot.lower() + suffix
            objects['PonyPart'][pid] = {'PonyPart': {'Type': slot, 'ModelName': pid, 'LinkedPart': ''}}
    return owner, objects


class WardrobeTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.objects = fixture()

    def resolve(self, **kwargs):
        return resolve_wardrobe(self.owner, self.objects, **kwargs)

    def fails(self, code, **kwargs):
        with self.assertRaises(WardrobeError) as result:
            self.resolve(**kwargs)
        self.assertEqual(result.exception.code, code)

    def test_default_first_set_preserves_non_wardrobe_geometry(self):
        result = self.resolve()
        self.assertEqual({n for n, shown in result['mesh_visibility'].items() if shown}, {'body0', 'mane0', 'tail0'})
        self.assertNotIn('unregistered_prop', result['mesh_visibility'])
        self.assertEqual(result['provenance']['set_ids'], ['base', 'dress'])

    def test_partial_outfit_keeps_default_tail(self):
        self.assertEqual(self.resolve(outfit_id='dress')['provenance']['selected_slots'],
                         {'Body': 'body1', 'Mane': 'mane1', 'Tail': 'tail0'})

    def test_linked_tail_is_selected_and_hidden_for_other_outfits(self):
        self.objects['PonyPart']['body1']['PonyPart']['LinkedPart'] = 'tail1'
        self.assertFalse(self.resolve()['mesh_visibility']['tail1'])
        result = self.resolve(outfit_id='dress')
        self.assertTrue(result['mesh_visibility']['tail1'])
        self.assertFalse(result['mesh_visibility']['tail0'])
        self.assertIn('tail1', result['provenance']['part_ids'])

    def test_default_constructor_uses_direct_slots_without_linked_overwrite(self):
        self.objects['PonyPart']['body0']['PonyPart']['LinkedPart'] = 'tail1'
        self.objects['PonySet']['base']['Parts']['Tail'] = ''
        self.fails('incomplete_default_wardrobe')

    def test_registered_direct_subset_participates(self):
        self.owner['Sets']['Sets'] = ['base']
        self.objects['PonySet']['base']['PonySet']['SubSets'] = ['dress']
        self.assertEqual(self.resolve(outfit_id='dress')['provenance']['selected_slots']['Body'], 'body1')

    def test_sets_first_empty_terminates_before_unknown_reference(self):
        self.owner['Sets']['Sets'] = ['base', '', 'missing']
        self.assertEqual(self.resolve()['provenance']['set_ids'], ['base'])

    def test_empty_subsets_is_not_mistaken_for_a_terminator(self):
        self.objects['PonySet']['base']['PonySet']['SubSets'] = ['', 'dress']
        self.fails('missing_wardrobe_set')

    def test_nested_subset_is_unverified(self):
        self.owner['Sets']['Sets'] = ['base']
        self.objects['PonySet']['base']['PonySet']['SubSets'] = ['dress']
        self.objects['PonySet']['dress']['PonySet']['SubSets'] = ['third']
        self.fails('nested_wardrobe_subsets_unverified')

    def test_nested_link_is_unverified(self):
        self.objects['PonyPart']['body1']['PonyPart']['LinkedPart'] = 'tail1'
        self.objects['PonyPart']['tail1']['PonyPart']['LinkedPart'] = 'mane1'
        self.fails('nested_linked_part_unverified')

    def test_conflicting_linked_slots_rejected(self):
        self.objects['PonyPart']['body1']['PonyPart']['LinkedPart'] = 'tail1'
        self.objects['PonySet']['dress']['Parts']['Tail'] = 'tail0'
        self.fails('conflicting_wardrobe_parts')

    def test_missing_parts_and_wrong_slot_rejected(self):
        self.objects['PonySet']['dress']['Parts']['Body'] = 'absent'
        self.fails('invalid_wardrobe_part')
        self.objects['PonySet']['dress']['Parts']['Body'] = 'mane1'
        self.fails('wardrobe_slot_mismatch')

    def test_unregistered_outfit_never_borrows_a_wardrobe(self):
        self.fails('outfit_not_registered', outfit_id='foreign')

    def test_same_mesh_cannot_receive_conflicting_slot_controls(self):
        self.objects['PonyPart']['body1']['PonyPart']['ModelName'] = 'tail0'
        self.fails('conflicting_wardrobe_mesh_visibility')

    def test_ponyset_enabled_is_not_a_mesh_visibility_flag(self):
        self.objects['PonySet']['dress']['PonySet']['Enabled'] = False
        self.assertTrue(self.resolve(outfit_id='dress')['mesh_visibility']['body1'])


if __name__ == '__main__':
    unittest.main()
