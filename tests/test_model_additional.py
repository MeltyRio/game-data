"""Synthetic normal-pipeline scenes with independent AdditionalObject actors."""
import copy
import hashlib
import struct
import unittest
from xml.etree import ElementTree as ET

import numpy as np

from bootstrap import ROOT
from native_fixture import copy_rk_with_material
import test_model_generation as generation_fixture
from luna_kit.gameobjectdata import GameObjectData


class AdditionalGeneration(unittest.TestCase):
    setUp = generation_fixture.ModelGeneration.setUp
    generate = generation_fixture.ModelGeneration.generate
    catalogue = generation_fixture.ModelGeneration.catalogue
    seed_generation = generation_fixture.ModelGeneration.seed_generation

    def prepare(self, *, locked=False, another=False, root='root'):
        schema = ET.parse(self.native/'gameobjectcategorydata.xml')
        pony = schema.find("./GameObjectCategory[@Name='Pony']")
        model = pony.find("./Parameter[@Name='Model']")
        ET.SubElement(model, 'Attribute', Name='Scale', Type='float', DefaultValue='2')
        ET.SubElement(model, 'Attribute', Name='RootBone', Type='string', DefaultValue='root')
        group = ET.SubElement(pony, 'Parameter', Name='AdditionalObject', Optional='1')
        for name, kind in [('Model','string'), ('Animation','string'), ('Scale','float'),
                           ('OffsetX','float'), ('OffsetY','float'), ('LockRotate','bool')]:
            ET.SubElement(group, 'Attribute', Name=name, Type=kind)
        schema.write(self.native/'gameobjectcategorydata.xml', encoding='utf-8')
        tree = ET.parse(self.native/'gameobjectdata.xml')
        body = tree.find("./Category[@ID='Pony']/GameObject[@ID='Synthetic_Flag']")
        body.find('Model').set('Scale', '2')
        body.find('Model').set('RootBone', root)
        ET.SubElement(body, 'AdditionalObject', Model='child', Animation='not_a_clip_or_library',
                      Scale='3', OffsetX='-100', OffsetY='-200', LockRotate=str(int(locked)))
        if another:
            other = copy.deepcopy(body)
            other.set('ID', 'Synthetic_Second')
            other.find('AdditionalObject').set('OffsetX', '50')
            tree.find("./Category[@ID='Pony']").append(other)
        tree.write(self.native/'gameobjectdata.xml', encoding='utf-8')
        (self.native/'child.rk').write_bytes((self.native/'flag.rk').read_bytes())
        animation = bytearray((self.native/'flag.anim').read_bytes())
        struct.pack_into('<f', animation, 92 + 16, 9.)
        (self.native/'child.anim').write_bytes(animation)
        (self.native/'child.csv').write_text('second,1,4,15\nfirst,0,0,15\n')
        self.objects = GameObjectData(self.native/'gameobjectdata.xml', self.native/'shopdata.xml',
                                     self.native/'gameobjectcategorydata.xml')

    def assert_scene_closed(self, row):
        child = row['additionalActor']['model']
        for model in [row, child]:
            for ref in [model, *model['animations']]:
                data = (self.dist/ref['path']).read_bytes()
                self.assertEqual(len(data), ref['bytes'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), ref['sha256'])
                self.assertEqual(ref['rigKey'], model['rigKey'])
        self.assertNotEqual(row['rigKey'], child['rigKey'])
        self.assertEqual(set(self.catalogue()['models']), {'Synthetic_Flag'})

    def test_normal_generation_keeps_two_independent_rigs_and_native_queue_order(self):
        self.prepare()
        report = self.generate(public_identities={'Synthetic_Flag'})
        self.assertEqual(self.catalogue()['schemaVersion'], 4)
        row = self.catalogue()['models']['Synthetic_Flag']
        self.assert_scene_closed(row)
        child = row['additionalActor']
        self.assertEqual(child['controller'], 'native-first-then-random-v1')
        self.assertEqual(child['model']['path'], f"models/3d/{child['model']['sha256']}.glb")
        for clip in child['model']['animations']:
            self.assertEqual(clip['path'], f"models/3d/anim/{clip['sha256']}/{clip['id']}.glb")
        self.assertEqual(child['parentRoot'], 0)
        self.assertEqual([clip['id'] for clip in child['model']['animations']], ['second', 'first'])
        self.assertTrue(report['identities']['Synthetic_Flag']['additional_actor']['converted'])
        self.assertNotIn('source_tables', (self.dist/'models_3d.json').read_text())
        self.assertFalse(any(path.suffix == '.json' for path in (self.dist/'models/3d').iterdir()))


    def test_parent_book_idle_recomputes_placement_without_changing_child_assets(self):
        self.prepare()
        animation = bytearray((self.native/'flag.anim').read_bytes())
        # Two native joints per frame: the second frame moves only the parent.
        struct.pack_into('<3f', animation, 92 + 64 + 16, 2., 3., 4.)
        (self.native/'flag.anim').write_bytes(animation)
        with (self.native/'flag.csv').open('a') as stream:
            stream.write('book_idle,1,1,15\n')
        self.generate()
        before = self.catalogue()['models']['Synthetic_Flag']['additionalActor']
        child = before['model']
        child_bytes = {ref['path']: (self.dist/ref['path']).read_bytes()
                       for ref in [child, *child['animations']]}
        self.objects['Pony']['Synthetic_Flag']['Conversation']['IdleAnimation'] = 'book_idle'
        report = self.generate()
        after = self.catalogue()['models']['Synthetic_Flag']['additionalActor']
        selection = report['identities']['Synthetic_Flag']['selection']
        self.assertEqual(selection['rest']['name'], 'book_idle')
        self.assertEqual(after['model'], before['model'])
        self.assertEqual(after['parentRoot'], before['parentRoot'])
        for path, data in child_bytes.items():
            self.assertEqual((self.dist/path).read_bytes(), data)
        old_transform = np.array(before['transform']).reshape(4, 4, order='F')
        new_transform = np.array(after['transform']).reshape(4, 4, order='F')
        np.testing.assert_array_equal(new_transform[:3, :3], old_transform[:3, :3])
        np.testing.assert_allclose(new_transform[:3, 3] - old_transform[:3, 3], [2., 0., -4.], atol=1e-9)
        proof = report['identities']['Synthetic_Flag']['additional_actor']['conversion_provenance']
        self.assertEqual(proof['rest_root_native'], [2., 3., 4.])


    def test_failed_child_does_not_stage_partial_parent_and_leaves_ordinary_assets(self):
        self.prepare()
        (self.native/'child.rk').unlink()
        ordinary = self.dist/'ordinary.txt'
        ordinary.parent.mkdir(parents=True)
        ordinary.write_text('keep')
        with self.assertRaisesRegex(ValueError, 'No verified basic'):
            self.generate()
        self.assertFalse((self.dist/'models_3d.json').exists())
        self.assertFalse(list((self.dist/'models/3d').rglob('*.glb')))
        self.assertEqual(ordinary.read_text(), 'keep')

    def test_unencodable_child_clip_keeps_original_indices_by_excluding_parent(self):
        self.prepare()
        # The CSV is valid, but its first clip cannot fit FLOAT sample times.
        # Dropping it would silently make the second native action index zero.
        (self.native/'child.csv').write_text('second,1,4,1e-40\nfirst,0,0,15\n')
        ordinary = copy.deepcopy(self.objects['Pony']['Synthetic_Flag'])
        ordinary.pop('AdditionalObject')
        self.objects['Pony']['Synthetic_Ordinary'] = ordinary
        with np.errstate(over='ignore'):
            report = self.generate()
        self.assertEqual(set(self.catalogue()['models']), {'Synthetic_Ordinary'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Flag']['code'],
                         'additional_actor_unverified')
        self.assertIn('original CSV animation',
                      report['generation']['excluded']['Synthetic_Flag']['reason'])
        self.assertFalse((self.dist/'models/3d/Synthetic_Flag.glb').exists())

    def test_invalid_child_material_excludes_parent_but_keeps_ordinary_model(self):
        self.prepare()
        copy_rk_with_material(self.native/'child.rk', self.native/'child.rk', 'child_material')
        (self.native/'child_material.rkm').write_text('not a material property')
        ordinary = copy.deepcopy(self.objects['Pony']['Synthetic_Flag'])
        ordinary.pop('AdditionalObject')
        self.objects['Pony']['Synthetic_Ordinary'] = ordinary
        report = self.generate()
        self.assertEqual(set(self.catalogue()['models']), {'Synthetic_Ordinary'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Flag']['code'], 'NativeInputError')
        self.assertIn('Invalid or duplicate material properties: child_material.rkm',
                      report['generation']['excluded']['Synthetic_Flag']['reason'])
        self.assertFalse((self.dist/'models/3d/Synthetic_Flag.glb').exists())


if __name__ == '__main__':
    unittest.main()
