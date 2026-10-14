"""Synthetic normal-pipeline scenes with independent AdditionalObject actors."""
import copy
import hashlib
import json
import math
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

import numpy as np

from bootstrap import ROOT
import test_model_generation as generation_fixture
from game_data import model_gen
from game_data.model_gen import additional
from game_data.model_gen.converter.csv import read_clips
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
        self.assertEqual(self.catalogue()['schemaVersion'], 2)
        row = self.catalogue()['models']['Synthetic_Flag']
        self.assert_scene_closed(row)
        child = row['additionalActor']
        self.assertEqual(child['controller'], 'native-first-then-random-v1')
        self.assertEqual(child['model']['path'], f"models/3d/{child['model']['sha256']}.glb")
        for clip in child['model']['animations']:
            self.assertEqual(clip['path'], f"models/3d/{clip['sha256']}/{clip['id']}.glb")
        self.assertEqual(child['parentRoot'], 0)
        self.assertEqual([clip['id'] for clip in child['model']['animations']], ['second', 'first'])
        self.assertTrue(report['identities']['Synthetic_Flag']['additional_actor']['converted'])
        self.assertNotIn('source_tables', (self.dist/'models_3d.json').read_text())
        self.assertFalse(any(path.suffix == '.json' for path in (self.dist/'models/3d').iterdir()))

    def test_child_native_tail_keeps_every_numeric_index_and_declared_duration(self):
        self.prepare()
        (self.native/'child.csv').write_text('second,1,4,15\nlast,45,125,15\n')
        report = self.generate(public_identities={'Synthetic_Flag'})
        child = self.catalogue()['models']['Synthetic_Flag']['additionalActor']['model']
        self.assertEqual([clip['id'] for clip in child['animations']], ['second', 'last'])
        self.assertEqual(child['animations'][1]['duration'], 81/15)
        proof = report['identities']['Synthetic_Flag']['additional_actor']['conversion_provenance']['child_source']['native_frame_boundary']
        self.assertEqual(proof['clips'][1]['clamped_samples'], 80)
        self.assertEqual(proof['source_frame_count'], 46)

    def test_child_is_shared_but_each_parent_placement_is_independent(self):
        self.prepare(another=True)
        real = additional.convert_native
        with patch.object(additional, 'convert_native', wraps=real) as convert:
            self.generate()
        self.assertEqual(convert.call_count, 1)
        a, b = [self.catalogue()['models'][identity]['additionalActor'] for identity in ['Synthetic_Flag','Synthetic_Second']]
        self.assertEqual(a['model'], b['model'])
        self.assertNotEqual(a['transform'], b['transform'])

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

    def test_plain_generation_stays_schema_one_and_bytes_unchanged(self):
        old = self.seed_generation()
        old_row = self.catalogue()['models']['Synthetic_Flag']
        old_files = {ref['path']:(self.dist/ref['path']).read_bytes() for ref in [old_row,*old_row['animations']]}
        self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), old)
        self.assertEqual(self.catalogue()['schemaVersion'], 1)
        for path, data in old_files.items():
            self.assertEqual((self.dist/path).read_bytes(), data)

    def test_bad_child_rows_preserve_previous_catalogue_instead_of_reindexing(self):
        old = self.seed_generation()
        self.prepare()
        for rows in ['second,1,4,15\nfirst,0,10000,15\n', 'second,1,4,15\nfirst,NaN,2,15\n', 'second,1,4,15\nsecond,1,4,15\n',
                     'second,1,4,15\n\nfirst,0,0,15\n', '\nsecond,1,4,15\n']:
            with self.subTest(rows=rows):
                (self.native/'child.csv').write_text(rows)
                with self.assertRaisesRegex(ValueError, 'No verified basic'):
                    self.generate()
                self.assertEqual((self.dist/'models_3d.json').read_bytes(), old)
                audit = json.loads(self.report.read_text())
                self.assertEqual(audit['generation']['excluded']['Synthetic_Flag']['code'], 'additional_actor_unverified')

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

    def test_missing_or_ambiguous_root_is_never_replaced_with_joint_zero(self):
        self.prepare(root='missing')
        with self.assertRaisesRegex(ValueError, 'No verified basic'):
            self.generate()
        self.assertIn('one exact native joint', json.loads(self.report.read_text())['generation']['excluded']['Synthetic_Flag']['reason'])
        self.objects['Pony']['Synthetic_Flag']['Model']['RootBone'] = 'flag'
        self.generate()
        self.assertEqual(self.catalogue()['models']['Synthetic_Flag']['additionalActor']['parentRoot'], 1)

    def test_visual_campaign_override_excludes_child_owner(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'No verified basic'):
            self.generate(campaign={'game_object_data':{'Pony':{'Synthetic_Flag':{'AdditionalObject':{'Scale':7}}}}})
        self.assertEqual(json.loads(self.report.read_text())['generation']['excluded']['Synthetic_Flag']['code'],
                         'campaign_visual_override_unverified')

    def test_partial_bad_child_excludes_only_its_owner(self):
        self.prepare(another=True)
        self.objects['Pony']['Synthetic_Second']['AdditionalObject']['Model'] = 'missing'
        report = self.generate()
        self.assertEqual(set(self.catalogue()['models']), {'Synthetic_Flag'})
        self.assertEqual(self.catalogue()['schemaVersion'], 2)
        self.assertEqual(report['generation']['excluded']['Synthetic_Second']['code'], 'additional_actor_unverified')
        self.assert_scene_closed(self.catalogue()['models']['Synthetic_Flag'])

    def test_user_vector_campaign_override_is_a_visual_dependency(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'No verified basic'):
            self.generate(campaign={'game_object_data':{'Pony':{'Synthetic_Flag':{'UserVector':{'x':1}}}}})
        self.assertEqual(json.loads(self.report.read_text())['generation']['excluded']['Synthetic_Flag']['code'],
                         'campaign_visual_override_unverified')

    def test_child_hash_mismatch_never_publishes_the_new_scene(self):
        from dataclasses import replace
        old = self.seed_generation()
        old_assets = set((self.dist/'models/3d').rglob('*.glb'))
        self.prepare()
        real = additional.convert_native
        def corrupt(*args, **kwargs):
            bundle = real(*args, **kwargs)
            files = dict(bundle.files)
            key = next(iter(files));files[key] += b'changed'
            return replace(bundle, files=files)
        with patch.object(additional, 'convert_native', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'asset key or digest'):
                self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), old)
        self.assertEqual(set((self.dist/'models/3d').rglob('*.glb')), old_assets)

    def test_matrix_matches_emulated_native_world_bases_without_map_depth(self):
        # Independent oracle builds the native 16-float matrices exactly as
        # their constructors store elements, then compares world-space points.
        def stored_rotation(axis, angle):
            a = np.eye(4).reshape(-1, order='F').copy()
            c, s = math.cos(angle), math.sin(angle)
            if axis == 'y':
                a[[0,10]] = c; a[8] = -s; a[2] = s
            else:
                a[[5,10]] = c; a[6] = -s; a[9] = s
            return a.reshape(4,4,order='F')
        for locked in [False, True]:
            with self.subTest(locked=locked):
                if not (self.native/'child.rk').exists():self.prepare(locked=locked)
                self.objects['Pony']['Synthetic_Flag']['AdditionalObject']['LockRotate'] = locked
                # Nonzero native rest root X/Y/Z; Y must not move the child.
                animation = bytearray((self.native/'flag.anim').read_bytes())
                struct.pack_into('<3f', animation, 92 + 16, 2., 3., 4.)
                (self.native/'flag.anim').write_bytes(animation)
                self.generate()
                meta = self.catalogue()['models']['Synthetic_Flag']['additionalActor']
                actual = np.array(meta['transform']).reshape(4,4,order='F')
                yaw = stored_rotation('y',0.19634954631328583)
                ground = stored_rotation('x',0.0872664675116539) @ stored_rotation('y',0.7853981852531433)
                owner = ground @ yaw @ np.diag([2.,2.,2.,1.])
                root_cancel = np.eye(4);root_cancel[:3,3] = [-2.,0.,-4.]
                owner = owner @ root_cancel;owner[:3,3] += [4.,-64.,0.]
                child = (stored_rotation('y',1.5707963705062866) if locked else yaw) @ np.diag([3.,3.,3.,1.])
                child[:3,3] += [-100.,-200.,0.]
                coordinates = np.diag([1.,-1.,-1.,1.])
                for point in [[0.,0.,0.,1.],[3.,-2.,7.,1.],[-4.,1.,2.,1.]]:
                    # The actual ATP placement must recover the same child
                    # point when returned to the owner's native world basis.
                    obtained = owner @ coordinates @ actual @ coordinates @ point
                    np.testing.assert_allclose(obtained, child @ point, atol=1e-9)
                # Current-root compensation is translation only, X/Z only.
                current = actual.copy();current[0,3] += 5.;current[2,3] -= 7.
                moved_owner = ground @ yaw @ np.diag([2.,2.,2.,1.])
                root_cancel[:3,3] = [-7.,0.,-11.]
                moved_owner = moved_owner @ root_cancel;moved_owner[:3,3] += [4.,-64.,0.]
                np.testing.assert_allclose(moved_owner @ coordinates @ current @ coordinates,
                                           child, atol=1e-9)


if __name__ == '__main__':
    unittest.main()
