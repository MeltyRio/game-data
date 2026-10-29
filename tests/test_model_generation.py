"""Real source discovery, conversion and local publication with synthetic data."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from bootstrap import ROOT
from native_fixture import copy_rk_with_material, write_fixture, write_native_tables
from game_data import model_gen
from game_data.model_catalogue import animation_assets, resolve_animation
from luna_kit.gameobjectdata import GameObjectData
from luna_kit.model import RKModel


class ModelGeneration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.native = self.root/'raw/extracted'
        self.dist = self.root/'dist'
        self.report = self.root/'raw/model-generation.json'
        write_fixture(self.native)
        self.objects = write_native_tables(self.native)
        self.campaign = {'game_object_data': {}}

    def generate(self, **kwargs):
        return model_gen.generate_models(self.objects, self.native, self.dist, self.report,
            campaign=kwargs.pop('campaign', self.campaign), **kwargs)

    def catalogue(self):
        catalogue = json.loads((self.dist/'models_3d.json').read_text())
        assets = animation_assets(catalogue)
        def expand(model):
            model['animations'] = [resolve_animation(reference, assets) for reference in model['animations']]
            if 'additionalActor' in model:
                expand(model['additionalActor']['model'])
        for model in catalogue['models'].values():
            expand(model)
        return catalogue

    def seed_generation(self):
        self.generate()
        return (self.dist/'models_3d.json').read_bytes()

    def assert_closed(self):
        catalogue = self.catalogue()
        for model in catalogue['models'].values():
            for ref in [model, *model['animations']]:
                data = (self.dist/ref['path']).read_bytes()
                self.assertEqual(len(data), ref['bytes'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), ref['sha256'])
                self.assertEqual(ref['rigKey'], model['rigKey'])
        return catalogue

    def test_selected_models_reuse_unchanged_rk(self):
        (self.native/'unused.rk').write_bytes((self.native/'flag.rk').read_bytes())
        self.objects = write_native_tables(self.native, {
            'Synthetic_Flag': {'model': 'flag', 'animation': 'flag', 'events': 'flag'},
            'Synthetic_Shared': {'model': 'flag', 'animation': 'flag', 'events': 'flag'},
            'Synthetic_Unused': {'model': 'unused', 'animation': 'flag', 'events': 'flag'},
        })
        reads = []
        original_read = RKModel.read
        def read(model, path):
            reads.append(Path(path).name)
            return original_read(model, path)
        with patch.object(RKModel, 'read', read):
            self.generate(public_identities={'Synthetic_Flag', 'Synthetic_Shared'})
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag', 'Synthetic_Shared'})
        self.assertEqual(reads, ['flag.rk'])


    def test_missing_rk_attributes_excludes_only_the_affected_identity(self):
        data = bytearray((self.native/'flag.rk').read_bytes())
        offset = next(80 + index * 16 for index in range(24)
                      if struct.unpack_from('<I', data, 80 + index * 16)[0] == 13)
        struct.pack_into('<4I', data, offset, 0, 0, 0, 0)
        (self.native/'broken.rk').write_bytes(data)
        self.objects = write_native_tables(self.native, {
            'Synthetic_Broken': {'model': 'broken', 'animation': 'flag', 'events': 'flag'},
            'Synthetic_Flag': {'model': 'flag', 'animation': 'flag', 'events': 'flag'},
        })

        report = self.generate()

        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertEqual(report['generation']['converted'], 1)
        self.assertEqual(set(report['generation']['excluded']), {'Synthetic_Broken'})
        self.assertIn('Unsupported RK section layout: 13',
                      report['generation']['excluded']['Synthetic_Broken']['reason'])
        # Discovery resolves filenames; conversion validates the RK contents.
        self.assertTrue(report['identities']['Synthetic_Broken']['references_resolved'])
        self.assertFalse(report['identities']['Synthetic_Broken']['converted'])
        self.assertTrue(report['identities']['Synthetic_Flag']['converted'])

    def test_luna_parse_error_excludes_only_the_affected_identity(self):
        (self.native/'broken.rk').write_bytes((self.native/'flag.rk').read_bytes())
        self.objects = write_native_tables(self.native, {
            'Synthetic_Broken': {'model': 'broken', 'animation': 'flag', 'events': 'flag'},
            'Synthetic_Flag': {'model': 'flag', 'animation': 'flag', 'events': 'flag'},
        })
        original_read = RKModel.read
        def read(model, path):
            if Path(path).name == 'broken.rk':
                raise IndexError('malformed native record')
            return original_read(model, path)
        with patch.object(RKModel, 'read', read):
            report = self.generate()
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertEqual(set(report['generation']['excluded']), {'Synthetic_Broken'})
        self.assertIn('malformed native record', report['generation']['excluded']['Synthetic_Broken']['reason'])

    def prepare_wardrobe(self, *, linked_tail=True, second_owner=False):
        """Self-authored, distinct native meshes plus real Luna-parsed tables."""
        names = ['flag', 'body0', 'body1', 'mane0', 'tail0', 'tail1', 'uncontrolled_trim']
        source = (self.native/'flag.rk').read_bytes()
        sections = []
        for index in range(24):
            tag, offset, count, size = struct.unpack_from('<4I', source, 80 + index * 16)
            if tag:
                sections.append((tag, count, source[offset:offset + size]))
        raw = {tag: data for tag, _, data in sections}
        faces = struct.unpack('<12H', raw[4])
        vertices = list(struct.iter_unpack('<5f', raw[3]))
        replacements = {
            1: (len(names), b''.join(struct.pack('<4I', 4, index * 12, 0, 0) for index in range(len(names)))),
            3: (len(names) * 6, b''.join(struct.pack('<5f', x + index * 1.1, y, z, u, v)
                for index in range(len(names)) for x, y, z, u, v in vertices)),
            4: (len(names) * 12, b''.join(struct.pack('<12H', *(value + index * 6 for value in faces))
                for index in range(len(names)))),
            16: (len(names), b''.join(name.encode().ljust(64, b'\0') for name in names)),
            17: (len(names) * 6, raw[17] * len(names)),
        }
        offset, headers, body = 80 + 24 * 16, [], bytearray()
        for tag, count, data in sections:
            count, data = replacements.get(tag, (count, data))
            headers.append(struct.pack('<4I', tag, offset, count, len(data)))
            body.extend(data)
            offset += len(data)
        (self.native/'wardrobe.rk').write_bytes(source[:80] + b''.join(headers).ljust(24 * 16, b'\0') + body)
        declarations = {'Synthetic_Flag': {'model':'wardrobe', 'animation':'flag', 'events':'flag'},
                        'Synthetic_Unrelated': {'model':'flag', 'animation':'flag', 'events':'flag'}}
        if second_owner:
            declarations['Synthetic_SecondOwner'] = {'model':'wardrobe', 'animation':'flag', 'events':'flag'}
        write_native_tables(self.native, declarations)
        schema = ET.parse(self.native/'gameobjectcategorydata.xml')
        for category, parameters in {
            'PonySet': {'Parts': ['Body', 'Mane', 'Tail'], 'PonySet': ['SubSets']},
            'PonyPart': {'PonyPart': ['Type', 'ModelName', 'LinkedPart']},
        }.items():
            node = ET.SubElement(schema.getroot(), 'GameObjectCategory', Name=category)
            for parameter, fields in parameters.items():
                group = ET.SubElement(node, 'Parameter', Name=parameter)
                for field in fields:
                    attributes = {'Name': field, 'Type': 'string'}
                    if field == 'SubSets':
                        attributes.update(Type='stringWithDefault', Array='6')
                    ET.SubElement(group, 'Attribute', **attributes)
        schema.write(self.native/'gameobjectcategorydata.xml', encoding='utf-8')
        tree = ET.parse(self.native/'gameobjectdata.xml')
        for owner in ['Synthetic_Flag', *(['Synthetic_SecondOwner'] if second_owner else [])]:
            sets = tree.find(f"./Category[@ID='Pony']/GameObject[@ID='{owner}']/Sets/Sets")
            for name in ['Synthetic_DefaultSet', 'Synthetic_Outfit']:
                ET.SubElement(sets, 'Item', Value=name)
        outfits = ET.SubElement(tree.getroot(), 'Category', ID='PonySet')
        for identity, parts in {
            'Synthetic_DefaultSet': {'Body':'body0', 'Mane':'mane0', 'Tail':'tail0'},
            'Synthetic_Outfit': {'Body':'body1', 'Mane':'', 'Tail':''},
        }.items():
            outfit = ET.SubElement(outfits, 'GameObject', ID=identity)
            ET.SubElement(outfit, 'Parts', **parts)
            ET.SubElement(ET.SubElement(outfit, 'PonySet'), 'SubSets')
        parts = ET.SubElement(tree.getroot(), 'Category', ID='PonyPart')
        for identity, slot in [('body0','Body'), ('body1','Body'), ('mane0','Mane'), ('tail0','Tail'), ('tail1','Tail')]:
            part = ET.SubElement(parts, 'GameObject', ID=identity)
            ET.SubElement(part, 'PonyPart', Type=slot, ModelName=identity,
                          LinkedPart='tail1' if identity == 'body1' and linked_tail else '')
        tree.write(self.native/'gameobjectdata.xml', encoding='utf-8')
        self.objects = GameObjectData(self.native/'gameobjectdata.xml', self.native/'shopdata.xml',
                                      self.native/'gameobjectcategorydata.xml')

    def model_document(self, identity):
        data = (self.dist/self.catalogue()['models'][identity]['path']).read_bytes()
        length, kind = struct.unpack_from('<II', data, 12)
        self.assertEqual(kind, 0x4e4f534a)
        return json.loads(data[20:20 + length])

    def model_mesh_names(self, identity):
        return {primitive['extras']['nativeMeshName'] for mesh in self.model_document(identity)['meshes']
                for primitive in mesh['primitives']}


    def prepare_book_and_world_contexts(self):
        """Separate native declarations and event files, still using the flag fixture."""
        write_native_tables(self.native, {
            'Synthetic_Flag': {'model': 'flag', 'animation': 'flag', 'events': 'world'}})
        tree = ET.parse(self.native/'gameobjectdata.xml')
        idle_list = tree.find("./Category/GameObject/Animation/LeftIdleAnimationList")
        for name in ['world_event', 'book_event']:
            ET.SubElement(idle_list, 'Item', Value=name)
        tree.write(self.native/'gameobjectdata.xml', encoding='utf-8')
        self.objects = GameObjectData(self.native/'gameobjectdata.xml', self.native/'shopdata.xml',
                                      self.native/'gameobjectcategorydata.xml')
        # Constructor row zero differs from the later declared Conversation idle.
        (self.native/'flag.csv').write_text(
            'bind,0,0,15\nrest,1,1,15\nwave,0,45,15\n'
            'world_event,0,2,15\nbook_event,0,2,15\n', encoding='utf-8')
        for stem, event in [('world', 'world_event'), ('flag', 'book_event')]:
            (self.native/(stem+'.xml')).write_text('<MeshList/><AnimationList>'
                '<Animation Name="'+event+'"><Frame Index="1"><EyeSet/></Frame></Animation>'
                '</AnimationList>', encoding='utf-8')


    def test_missing_world_xml_matches_an_explicit_empty_controller(self):
        self.prepare_book_and_world_contexts()
        world = self.native/'world.xml'
        world.write_text('<MeshList/><AnimationList/>')
        self.generate()
        expected = self.assert_closed()
        files = {str(path.relative_to(self.dist)): path.read_bytes()
                 for path in (self.dist/'models/3d').rglob('*.glb')}
        world.unlink()

        report = self.generate()

        self.assertEqual(report['generation']['excluded'], {})
        self.assertEqual(self.assert_closed(), expected)
        self.assertTrue(all((self.dist/name).read_bytes() == data for name, data in files.items()))
        self.assertEqual(report['identities']['Synthetic_Flag']['selection']['events']['requested_file'], 'world.xml')

    def test_normal_generation_uses_empty_book_controller_then_reads_restored_xml(self):
        self.prepare_book_and_world_contexts()
        self.generate(display_profile='world-preview')
        world = self.assert_closed()
        references = world['models']['Synthetic_Flag']
        book = self.native/'flag.xml'
        original = book.read_bytes()
        book.unlink()
        report = self.generate()
        self.assertEqual(report['generation']['display_profiles'], {'ponybook-default': 1})
        self.assertEqual(report['generation']['excluded'], {})
        selection = report['identities']['Synthetic_Flag']['selection']
        self.assertEqual(selection['ponybook']['event_source'], 'missing-input-native-empty-controller')
        self.assertEqual(selection['ponybook']['event_file'], 'flag.xml')
        missing_row = self.assert_closed()['models']['Synthetic_Flag']
        self.assertEqual(missing_row['sha256'], references['sha256'])
        self.assertEqual({a['id'] for a in missing_row['animations']}, {'wave', 'rest', 'bind', 'book_event'})
        # Restore the same source bytes without changing generation options or tables.
        book.write_bytes(original)
        restored = self.generate()
        row = self.assert_closed()['models']['Synthetic_Flag']
        self.assertEqual(restored['generation']['display_profiles'], {'ponybook-default': 1})
        selection = restored['identities']['Synthetic_Flag']['selection']
        self.assertNotIn('ponybook_unavailable', selection['display'])
        self.assertEqual(selection['rest']['name'], 'rest')
        self.assertEqual(selection['ponybook']['constructor']['name'], 'bind')
        self.assertEqual(selection['ponybook']['event_source'], 'xml')
        self.assertEqual(row['path'], references['path'])
        # Both verified contexts choose the same idle and visible geometry.
        self.assertEqual(row['sha256'], references['sha256'])
        self.assertEqual({a['id'] for a in row['animations']}, {'wave', 'rest', 'bind'})


    def test_partial_missing_declared_model_excludes_it_without_dangling_catalogue(self):
        self.objects = write_native_tables(self.native, {
            'Synthetic_Flag': {'model':'flag', 'animation':'flag', 'events':'flag'},
            'Synthetic_Missing': {'model':'missing', 'animation':'flag', 'events':'flag'}})
        report = self.generate()
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Missing']['code'], 'unresolved_association')
        self.assertEqual(report['generation']['coverage'], 'partial; only validated basic scenes')

    def test_invalid_present_material_excludes_only_its_model(self):
        copy_rk_with_material(self.native/'flag.rk', self.native/'broken.rk', 'broken')
        (self.native/'broken.rkm').write_text('not a material property')
        self.objects = write_native_tables(self.native, {
            'Synthetic_Broken': {'model':'broken', 'animation':'flag', 'events':'flag'},
            'Synthetic_Flag': {'model':'flag', 'animation':'flag', 'events':'flag'}})
        report = self.generate()
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Broken']['code'], 'NativeInputError')
        self.assertIn('Invalid or duplicate material properties: broken.rkm',
                      report['generation']['excluded']['Synthetic_Broken']['reason'])
        self.assertFalse((self.dist/'models/3d/Synthetic_Broken.glb').exists())

    def test_material_candidates_respect_exact_source_art_precedence(self):
        copy_rk_with_material(self.native/'flag.rk', self.native/'art.rk', 'art.psd')
        valid_material = (self.native/'flag.rkm').read_bytes()
        (self.native/'art.psd.rkm').write_bytes(valid_material)
        (self.native/'art.rkm').write_text('not a material property')
        self.objects = write_native_tables(self.native, {
            'Synthetic_Art': {'model':'art', 'animation':'flag', 'events':'flag'},
            'Synthetic_Flag': {'model':'flag', 'animation':'flag', 'events':'flag'}})
        self.generate()
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Art', 'Synthetic_Flag'})

        (self.native/'art.psd.rkm').write_text('not a material property')
        (self.native/'art.rkm').write_bytes(valid_material)
        report = self.generate()
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Art']['code'], 'NativeInputError')
        self.assertIn('Invalid or duplicate material properties: art.psd.rkm',
                      report['generation']['excluded']['Synthetic_Art']['reason'])

        (self.native/'art.psd.rkm').unlink()
        self.generate()
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Art', 'Synthetic_Flag'})

    def test_source_read_error_excludes_only_affected_identity(self):
        (self.native/'broken.xml').write_bytes((self.native/'flag.xml').read_bytes())
        self.objects = write_native_tables(self.native, {
            'Synthetic_Broken': {'model':'flag', 'animation':'flag', 'events':'broken'},
            'Synthetic_Flag': {'model':'flag', 'animation':'flag', 'events':'flag'}})
        original = Path.read_bytes
        def read(path):
            if path == self.native/'broken.xml':
                raise OSError('synthetic source read error')
            return original(path)
        try:
            with patch.object(Path, 'read_bytes', read):
                report = self.generate()
        except Exception as error:
            self.fail(f'An isolated source read error stopped generation: {error}')
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertIn('synthetic source read error', report['generation']['excluded']['Synthetic_Broken']['reason'])

    def test_invalid_identity_override_excludes_only_affected_identity(self):
        self.objects = write_native_tables(self.native, {
            'Synthetic_Broken': {'model':'flag', 'animation':'flag', 'events':'flag'},
            'Synthetic_Flag': {'model':'flag', 'animation':'flag', 'events':'flag'}})
        try:
            report = self.generate(campaign={'game_object_data': {'Pony': {'Synthetic_Broken': []}}})
        except Exception as error:
            self.fail(f'An isolated source override stopped generation: {error}')
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertIn('Synthetic_Broken', report['generation']['excluded'])

    def test_staging_write_failure_remains_fatal_and_preserves_catalogue(self):
        previous = self.seed_generation()
        original = Path.write_bytes
        def write(path, data):
            if any(part.startswith('.models-3d-') for part in path.parts):
                raise OSError('synthetic staging failure')
            return original(path, data)
        with patch.object(Path, 'write_bytes', write), self.assertRaisesRegex(OSError, 'synthetic staging failure'):
            self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
        self.assertTrue(json.loads(self.report.read_text())['generation']['status'].startswith('failed'))


    def test_all_fail_preserves_previous_catalogue(self):
        previous = self.seed_generation()
        (self.native/'flag.rk').unlink()
        with self.assertRaisesRegex(ValueError, 'No verified basic'):
            self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
        self.assertEqual(json.loads(self.report.read_text())['generation']['converted'], 0)
        self.assertTrue(json.loads(self.report.read_text())['generation']['status'].startswith('failed'))


    def test_shared_animation_reused_without_merging_object_identity(self):
        declarations = {identity: {'model':'flag','animation':'flag','events':'flag'}
                        for identity in ['Synthetic_Flag', 'Synthetic_Other']}
        self.objects = write_native_tables(self.native, declarations)
        report = self.generate()
        wire = json.loads((self.dist/'models_3d.json').read_text())
        self.assertEqual(wire['schemaVersion'], 4)
        self.assertIsInstance(wire['animationAssets'], list)
        self.assertTrue(wire['animationAssets'])
        self.assertTrue(all(isinstance(ref, dict) for ref in wire['animationAssets']))
        self.assertTrue(all(type(index) is int for row in wire['models'].values() for index in row['animations']))
        self.assertEqual(wire['models']['Synthetic_Flag']['animations'],
                         wire['models']['Synthetic_Other']['animations'])
        self.assertEqual(len(wire['animationAssets']), len(wire['models']['Synthetic_Flag']['animations']))
        rows = self.assert_closed()['models']
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows['Synthetic_Flag']['path'], rows['Synthetic_Other']['path'])
        self.assertEqual(rows['Synthetic_Flag']['animations'], rows['Synthetic_Other']['animations'])
        unique = {ref['path'] for row in rows.values() for ref in [row,*row['animations']]}
        self.assertEqual(report['generation']['asset_count'], len(unique))
        self.assertEqual(len(list((self.dist/'models/3d').rglob('*.glb'))), len(unique))


    def test_real_ponyset_glb_keeps_identity_and_applies_linked_slot_over_defaults(self):
        self.prepare_wardrobe()
        report = self.generate(public_identities={'Synthetic_Flag', 'Synthetic_Outfit'})
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag', 'Synthetic_Outfit'})
        self.assertEqual(self.model_mesh_names('Synthetic_Flag'), {'flag', 'body0', 'mane0', 'tail0', 'uncontrolled_trim'})
        self.assertEqual(self.model_mesh_names('Synthetic_Outfit'), {'flag', 'body1', 'mane0', 'tail1', 'uncontrolled_trim'})
        self.assertEqual(self.model_document('Synthetic_Outfit')['extras']['atpModel']['identity'], 'Synthetic_Outfit')
        self.assertEqual(report['identities']['Synthetic_Outfit']['selection']['wardrobe']['selected_slots'],
                         {'Body':'body1', 'Mane':'mane0', 'Tail':'tail1'})
        self.assertEqual(self.catalogue()['models']['Synthetic_Flag']['animations'],
                         self.catalogue()['models']['Synthetic_Outfit']['animations'])
        outfit = self.catalogue()['models']['Synthetic_Outfit']
        self.generate(public_identities={'Synthetic_Outfit'})
        self.assertEqual(self.assert_closed()['models'], {'Synthetic_Outfit': outfit})

    def test_outfit_uses_its_page_owner_without_merging_owner_defaults(self):
        self.prepare_wardrobe(second_owner=True)
        owner = self.objects['Pony']['Synthetic_SecondOwner']
        owner['Conversation']['IdleAnimation'] = 'wave'
        report = self.generate(public_identities={'Synthetic_Outfit'},
            outfit_owners={'Synthetic_Outfit': 'Synthetic_SecondOwner'})
        self.assertEqual(report['generation']['excluded'], {})
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Outfit'})
        row = report['identities']['Synthetic_Outfit']
        self.assertEqual(row['owner'], 'Synthetic_SecondOwner')
        self.assertEqual(row['selection']['rest']['name'], 'wave')
        self.assertEqual(self.model_mesh_names('Synthetic_Outfit'),
                         {'flag', 'body1', 'mane0', 'tail1', 'uncontrolled_trim'})

    def prepare_standalone_parts(self):
        self.prepare_wardrobe(linked_tail=False)
        tree = ET.parse(self.native/'gameobjectdata.xml')
        ponies = tree.find("./Category[@ID='Pony']")
        ponies.remove(ponies.find("./GameObject[@ID='Synthetic_Flag']"))
        ponies.find('./GameObject/Eyes/EyeSubObject_Open').clear()
        outfits = tree.find("./Category[@ID='PonySet']")
        outfits.remove(outfits.find("./GameObject[@ID='Synthetic_DefaultSet']"))
        outfits.find('./GameObject/Parts').attrib.update(Body='body0', Mane='mane0', Tail='tail0')
        for part in tree.findall("./Category[@ID='PonyPart']/GameObject/PonyPart"):
            part.set('ModelName', 'flag')
        tree.write(self.native/'gameobjectdata.xml', encoding='utf-8')
        self.objects = GameObjectData(self.native/'gameobjectdata.xml', self.native/'shopdata.xml',
                                      self.native/'gameobjectcategorydata.xml')

    def test_standalone_parts_preview_keeps_identity_without_creating_ownership(self):
        self.prepare_standalone_parts()
        report = self.generate(public_identities={'Synthetic_Outfit'})
        outfit = self.assert_closed()['models']['Synthetic_Outfit']
        expected_bytes = (self.dist/outfit['path']).read_bytes()
        row = report['identities']['Synthetic_Outfit']
        self.assertEqual(row['owners'], [])
        self.assertNotIn('owner', row)
        self.assertEqual(row['preview_source'], 'Synthetic_Unrelated')
        self.assertEqual(row['preview_relation'], 'standalone-parts-preview')
        self.assertNotIn('wardrobe', outfit)
        self.assertEqual(self.model_mesh_names('Synthetic_Outfit'), {'flag'})
        self.assertEqual(self.model_document('Synthetic_Outfit')['extras']['atpModel']['identity'], 'Synthetic_Outfit')
        self.assertEqual(self.objects['Pony']['Synthetic_Unrelated']['Sets']['Sets'], [])

        reads = []
        original_read = RKModel.read
        def read(model, path):
            reads.append(Path(path).name)
            return original_read(model, path)
        with patch.object(RKModel, 'read', read):
            self.generate()
        self.assertEqual(reads, ['flag.rk'])
        models = self.assert_closed()['models']
        self.assertEqual(models['Synthetic_Outfit'], outfit)
        self.assertEqual((self.dist/outfit['path']).read_bytes(), expected_bytes)
        self.assertEqual(models['Synthetic_Unrelated']['animations'], outfit['animations'])

    def test_standalone_parts_scope_registration_errors_by_native_meshes(self):
        from copy import deepcopy
        self.prepare_standalone_parts()
        original = deepcopy(self.objects)
        data = bytearray((self.native/'flag.rk').read_bytes())
        headers = [struct.unpack_from('<4I', data, 80 + index * 16) for index in range(24)]
        _, offset, _, size = next(header for header in headers if header[0] == 16)
        data[offset:offset + size] = b'other'.ljust(size, b'\0')
        (self.native/'other.rk').write_bytes(data)
        self.generate(public_identities={'Synthetic_Outfit'})
        expected = self.assert_closed()['models']['Synthetic_Outfit']
        expected_bytes = (self.dist/expected['path']).read_bytes()

        for case in ('unrelated', 'matching', 'unknown_model', 'source',
                     'registered_before', 'registered_after', 'registered_subset', 'registered_later_subset'):
            with self.subTest(case=case):
                self.objects = deepcopy(original)
                broken = deepcopy(self.objects['Pony']['Synthetic_Unrelated'])
                broken['Sets']['Sets'] = ['Missing_Set']
                if case == 'source':
                    self.objects['Pony']['Synthetic_Unrelated'] = broken
                else:
                    broken['Model']['MediumLOD'] = {
                        'matching': 'flag', 'unknown_model': 'missing'}.get(case, 'other')
                    if case == 'registered_before':
                        broken['Sets']['Sets'] = ['Synthetic_Outfit', 'Missing_Set']
                    elif case == 'registered_after':
                        broken['Sets']['Sets'] = ['Missing_Set', 'Synthetic_Outfit']
                    elif case in ('registered_subset', 'registered_later_subset'):
                        broken['Sets']['Sets'] = ['Source_Set']
                        children = ['Missing_Set', 'Synthetic_Outfit']
                        if case == 'registered_later_subset':
                            broken['Sets']['Sets'].insert(0, 'Missing_Set')
                            children = ['Synthetic_Outfit']
                        self.objects['PonySet']['Source_Set'] = {
                            'Parts': {'Body': 'body0'}, 'PonySet': {'SubSets': children}}
                    self.objects['Pony']['Synthetic_Broken'] = broken
                if case == 'unrelated':
                    report = self.generate(public_identities={'Synthetic_Outfit'})
                    self.assertEqual(report['generation']['excluded'], {})
                    self.assertEqual(self.assert_closed()['models']['Synthetic_Outfit'], expected)
                    self.assertEqual((self.dist/expected['path']).read_bytes(), expected_bytes)
                    row = report['identities']['Synthetic_Outfit']
                    self.assertEqual(row['preview_source'], 'Synthetic_Unrelated')
                    self.assertNotIn('owner', row)
                else:
                    with self.assertRaisesRegex(ValueError, 'No verified basic'):
                        self.generate(public_identities={'Synthetic_Outfit'})
                    report = json.loads(self.report.read_text())
                    self.assertEqual(report['generation']['excluded']['Synthetic_Outfit']['code'],
                                     'unresolved_association')
                    if case.startswith('registered_'):
                        self.assertEqual(report['identities']['Synthetic_Outfit']['owners'], ['Synthetic_Broken'])

    def test_standalone_parts_require_one_exact_complete_native_scene(self):
        from copy import deepcopy
        self.prepare_standalone_parts()
        source = deepcopy(self.objects)
        for case in ('ambiguous', 'ambiguous_invalid_animation', 'absent', 'partial',
                     'extra_geometry', 'explicit_unregistered_owner'):
            with self.subTest(case=case):
                self.objects = deepcopy(source)
                kwargs = {}
                if case.startswith('ambiguous'):
                    self.objects['Pony']['Synthetic_Second'] = deepcopy(self.objects['Pony']['Synthetic_Unrelated'])
                    if case == 'ambiguous_invalid_animation':
                        self.objects['Pony']['Synthetic_Second']['Animation']['Rig_MediumLOD'] = 'absent'
                elif case in ('absent', 'partial'):
                    ids = ('body0', 'mane0', 'tail0') if case == 'absent' else ('body0',)
                    for identity in ids:
                        self.objects['PonyPart'][identity]['PonyPart']['ModelName'] = 'FLAG'
                elif case == 'extra_geometry':
                    self.objects['Pony']['Synthetic_Unrelated']['Model']['MediumLOD'] = 'wardrobe'
                    for identity in ('body0', 'mane0', 'tail0'):
                        self.objects['PonyPart'][identity]['PonyPart']['ModelName'] = 'body1'
                else:
                    kwargs['outfit_owners'] = {'Synthetic_Outfit': 'Synthetic_Unrelated'}
                report = self.generate(**kwargs)
                self.assertNotIn('Synthetic_Outfit', self.assert_closed()['models'])
                self.assertIn('Synthetic_Outfit', report['generation']['excluded'])

    def test_standalone_parts_do_not_silently_include_later_visible_props(self):
        self.prepare_standalone_parts()
        obj = self.objects['Pony']['Synthetic_Unrelated']
        obj['Model']['MediumLOD'] = 'wardrobe'
        obj['Animation']['LeftIdleAnimationList'] = ['rest']
        for identity in ('body0', 'mane0', 'tail0'):
            self.objects['PonyPart'][identity]['PonyPart']['ModelName'] = 'body1'
        hidden = ('flag', 'body0', 'mane0', 'tail0', 'tail1', 'uncontrolled_trim')
        (self.native/'flag.xml').write_text('<MeshList><wardrobe>' + ''.join(
            f'<SubObject ID="{name}" Name="{name}" DefaultVisible="0"/>' for name in hidden) +
            '</wardrobe></MeshList><AnimationList><Animation Name="wave">'
            '<Frame Index="1"><SubObject ID="uncontrolled_trim" Show="1"/></Frame>'
            '</Animation></AnimationList>')
        report = self.generate()
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Unrelated'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Outfit']['code'], 'incomplete_part_preview')
        self.assertIn('beyond the declared parts', report['generation']['excluded']['Synthetic_Outfit']['reason'])

    def test_standalone_parts_keep_part_and_source_campaign_checks(self):
        self.prepare_standalone_parts()
        for category, identity in [('PonyPart', 'body0'), ('Pony', 'Synthetic_Unrelated')]:
            with self.subTest(category=category):
                with self.assertRaisesRegex(ValueError, 'No verified basic'):
                    self.generate(public_identities={'Synthetic_Outfit'}, campaign={
                        'game_object_data': {category: {identity: {'ModelName': 'changed'}}}})
                report = json.loads(self.report.read_text())
                self.assertEqual(report['generation']['excluded']['Synthetic_Outfit']['code'],
                                 'campaign_visual_override_unverified')

        parts = {}
        for slot in ('Body', 'Mane', 'Tail'):
            identity = 'Source_' + slot
            parts[slot] = identity
            self.objects['PonyPart'][identity] = {
                'PonyPart': {'Type': slot, 'ModelName': 'flag', 'LinkedPart': ''}}
        self.objects['PonySet']['Source_Default'] = {'Parts': parts, 'PonySet': {'SubSets': []}}
        self.objects['Pony']['Synthetic_Unrelated']['Sets']['Sets'] = ['Source_Default']
        with self.assertRaisesRegex(ValueError, 'No verified basic'):
            self.generate(public_identities={'Synthetic_Outfit'}, campaign={'game_object_data': {
                'PonyPart': {'Source_Body': {'ModelName': 'changed'}}}})
        report = json.loads(self.report.read_text())
        self.assertEqual(report['generation']['excluded']['Synthetic_Outfit']['code'],
                         'campaign_visual_override_unverified')

    def test_standalone_static_open_does_not_require_optional_interactions(self):
        self.prepare_standalone_parts()
        obj = self.objects['Pony']['Synthetic_Unrelated']
        obj['Model']['MediumLOD'] = 'wardrobe'
        obj['Eyes'] = {'EyeSubObject_Open': ['flag']}
        obj['Conversation'] = {'IdleAnimation': 'rest', 'InteractionAnimation': ''}
        for identity in ('body0', 'mane0', 'tail0'):
            self.objects['PonyPart'][identity]['PonyPart']['ModelName'] = 'body1'
        hidden = ('body0', 'mane0', 'tail0', 'tail1', 'uncontrolled_trim')
        (self.native/'flag.xml').write_text('<MeshList><wardrobe>' + ''.join(
            f'<SubObject ID="{name}" Name="{name}" DefaultVisible="0"/>' for name in hidden) +
            '</wardrobe></MeshList><AnimationList/>')
        for profile in ('world-preview', 'ponybook-preferred'):
            with self.subTest(profile=profile):
                report = self.generate(public_identities={'Synthetic_Outfit'}, display_profile=profile)
                self.assertEqual(report['generation']['excluded'], {})
                self.assertEqual(self.model_mesh_names('Synthetic_Outfit'), {'body1', 'flag'})
                self.assertNotIn('interactions', self.assert_closed()['models']['Synthetic_Outfit'])


if __name__ == '__main__':
    unittest.main()
