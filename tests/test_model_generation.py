"""Real source discovery, conversion and local publication with synthetic data."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from bootstrap import ROOT
from native_fixture import write_fixture, write_native_tables
from game_data import model_gen
from luna_kit.gameobjectdata import GameObjectData


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
        return json.loads((self.dist/'models_3d.json').read_text())

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
        self.assertFalse(report['identities']['Synthetic_Broken']['references_resolved'])
        self.assertTrue(report['identities']['Synthetic_Flag']['converted'])

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

    def prepare_pet(self):
        schema = ET.parse(self.native/'gameobjectcategorydata.xml')
        category = ET.SubElement(schema.getroot(), 'GameObjectCategory', Name='PonyPet')
        group = ET.SubElement(category, 'Parameter', Name='Object')
        for name in ['Model', 'Animation', 'AnimationIdle', 'AnimationWalk', 'AnimationRun', 'AnimationEditorIdle']:
            ET.SubElement(group, 'Attribute', Name=name, Type='string')
        schema.write(self.native/'gameobjectcategorydata.xml', encoding='utf-8')
        tree = ET.parse(self.native/'gameobjectdata.xml')
        category = ET.SubElement(tree.getroot(), 'Category', ID='PonyPet')
        pet = ET.SubElement(category, 'GameObject', ID='Synthetic_Pet')
        ET.SubElement(pet, 'Object', Model='flag', Animation='flag', AnimationIdle='',
                      AnimationWalk='wave', AnimationRun='', AnimationEditorIdle='')
        tree.write(self.native/'gameobjectdata.xml', encoding='utf-8')
        self.objects = GameObjectData(self.native/'gameobjectdata.xml', self.native/'shopdata.xml',
                                      self.native/'gameobjectcategorydata.xml')

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

    def test_normal_generation_prefers_book_and_reports_the_selected_profile(self):
        self.prepare_book_and_world_contexts()
        report = self.generate()
        row = self.assert_closed()['models']['Synthetic_Flag']
        self.assertEqual(report['generation']['display_profiles'], {'ponybook-default': 1})
        self.assertEqual(report['generation']['excluded'], {})
        selection = report['identities']['Synthetic_Flag']['selection']
        self.assertEqual(selection['display']['requested'], 'ponybook-preferred')
        self.assertEqual(selection['display']['selected'], 'ponybook-default')
        self.assertEqual(selection['rest']['name'], 'bind')
        self.assertEqual(selection['ponybook']['idle']['name'], 'rest')
        self.assertEqual({a['id'] for a in row['animations']}, {'wave', 'rest', 'bind'})
        self.assertEqual({e['name'] for e in report['identities']['Synthetic_Flag']['excluded_clips']},
                         {'world_event', 'book_event'})
        saved = json.loads(self.report.read_text())
        self.assertEqual(saved['generation']['display_profiles'], {'ponybook-default': 1})
        self.assertEqual(saved['identities']['Synthetic_Flag']['selection'], selection)

    def test_normal_generation_keeps_verified_world_until_exact_book_xml_is_restored(self):
        self.prepare_book_and_world_contexts()
        self.generate(display_profile='world-preview')
        world = self.assert_closed()
        world_catalogue = (self.dist/'models_3d.json').read_bytes()
        references = world['models']['Synthetic_Flag']
        world_bytes = {ref['path']: (self.dist/ref['path']).read_bytes()
                       for ref in [references, *references['animations']]}
        book = self.native/'flag.xml'
        original = book.read_bytes()
        book.unlink()
        report = self.generate()
        self.assertEqual(report['generation']['display_profiles'], {'world-preview': 1})
        self.assertEqual(report['generation']['excluded'], {})
        selection = report['identities']['Synthetic_Flag']['selection']
        self.assertEqual(selection['display']['selected'], 'world-preview')
        self.assertEqual(selection['display']['ponybook_unavailable']['code'], 'missing_event_source')
        self.assertIn('flag.xml', selection['display']['ponybook_unavailable']['reason'])
        self.assertNotIn('ponybook', selection)
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), world_catalogue)
        self.assertEqual({name: (self.dist/name).read_bytes() for name in world_bytes}, world_bytes)
        # Restore the same source bytes without changing generation options or tables.
        book.write_bytes(original)
        restored = self.generate()
        row = self.assert_closed()['models']['Synthetic_Flag']
        self.assertEqual(restored['generation']['display_profiles'], {'ponybook-default': 1})
        selection = restored['identities']['Synthetic_Flag']['selection']
        self.assertNotIn('ponybook_unavailable', selection['display'])
        self.assertEqual(selection['rest']['name'], 'bind')
        self.assertEqual(row['path'], references['path'])
        self.assertNotEqual(row['sha256'], references['sha256'])
        self.assertEqual({a['id'] for a in row['animations']}, {'wave', 'rest', 'bind'})

    def test_normal_generation_passes_both_contexts_to_real_converter_validation(self):
        self.prepare_book_and_world_contexts()
        report = self.generate()
        self.assert_closed()
        provenance = report['identities']['Synthetic_Flag']['conversion_provenance']
        self.assertEqual(provenance['event_source']['name'], 'world.xml')
        self.assertEqual([s['name'] for s in provenance['additional_event_sources']], ['flag.xml'])
        for name in ['world.xml', 'flag.xml']:
            expected = {'name': name, 'sha256': hashlib.sha256((self.native/name).read_bytes()).hexdigest()}
            self.assertIn(expected, provenance['files'])
        previous = (self.dist/'models_3d.json').read_bytes()
        convert = model_gen.convert_native
        # Inject a changed source after selection, at the real converter boundary.
        # Both contexts must still be checked there, and no invalid catalogue published.
        for name in ['world.xml', 'flag.xml']:
            source = self.native/name
            original = source.read_bytes()

            def change_before_conversion(*args, **kwargs):
                source.write_text('<AnimationList><Animation Name="bind">'
                    '<Frame Index="0"><EyeSet/></Frame></Animation></AnimationList>', encoding='utf-8')
                return convert(*args, **kwargs)

            try:
                with self.subTest(source=name), patch.object(model_gen, 'convert_native',
                                                            side_effect=change_before_conversion):
                    with self.assertRaisesRegex(ValueError, 'No verified basic'):
                        self.generate()
                    self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
                    failed = json.loads(self.report.read_text())
                    self.assertEqual(failed['generation']['converted'], 0)
                    reason = failed['generation']['excluded']['Synthetic_Flag']
                    self.assertEqual(reason['code'], 'NativeInputError')
                    self.assertIn('unsupported native events: bind', reason['reason'])
            finally:
                source.write_bytes(original)

    def test_real_luna_tables_to_model_and_standalone_animation(self):
        report = self.generate(public_identities={'Synthetic_Flag'})
        row = self.assert_closed()['models']['Synthetic_Flag']
        self.assertEqual(report['generation']['converted'], 1)
        self.assertTrue(report['identities']['Synthetic_Flag']['converted'])
        self.assertEqual(row['animations'][0]['id'], 'wave')
        self.assertEqual(row['animations'][0]['duration'], 46/15)
        self.assertEqual(report['identities']['Synthetic_Flag']['selection']['rest']['name'], 'rest')
        self.assertEqual(report['generation']['excluded'], {})
        self.assertIn('source_tables', json.loads(self.report.read_text()))

    def test_generation_summary_preserves_discovery_counts_without_stale_blockers(self):
        report = self.generate(public_identities={'Synthetic_Flag'})
        self.assertEqual(report['generation']['converted'], 1)
        self.assertEqual(report['identities']['Synthetic_Flag']['blockers'], [])
        self.assertNotIn('blockers', report['summary'])
        self.assertEqual(report['summary']['discovery_blockers']['frame_conversion_not_run'], 1)
        saved = json.loads(self.report.read_text())
        self.assertEqual(saved['summary'], report['summary'])
        self.assertEqual(saved['identities']['Synthetic_Flag']['blockers'], [])

    def test_partial_missing_declared_model_excludes_it_without_dangling_catalogue(self):
        self.objects = write_native_tables(self.native, {
            'Synthetic_Flag': {'model':'flag', 'animation':'flag', 'events':'flag'},
            'Synthetic_Missing': {'model':'missing', 'animation':'flag', 'events':'flag'}})
        report = self.generate()
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Missing']['code'], 'unresolved_association')
        self.assertEqual(report['generation']['coverage'], 'partial; only validated basic scenes')

    def test_corrupt_texture_excludes_only_affected_object(self):
        data = bytearray((self.native/'flag.rk').read_bytes())
        for index in range(24):
            tag, offset, _, _ = struct.unpack_from('<4I', data, 80 + index * 16)
            if tag == 2:
                data[offset:offset + 64] = b'broken'.ljust(64, b'\0')
        (self.native/'broken.rk').write_bytes(data)
        (self.native/'broken.rkm').write_text((self.native/'flag.rkm').read_text().replace(
            'DiffuseTexture=flag', 'DiffuseTexture=broken'))
        (self.native/'broken.png').write_bytes(b'not a PNG')
        self.objects = write_native_tables(self.native, {
            'Synthetic_Flag': {'model':'flag', 'animation':'flag', 'events':'flag'},
            'Synthetic_Broken': {'model':'broken', 'animation':'flag', 'events':'flag'}})
        report = self.generate()
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Broken']['code'], 'NativeInputError')

    def test_unsupported_or_invalid_pvr_excludes_only_affected_object(self):
        data = bytearray((self.native/'flag.rk').read_bytes())
        for index in range(24):
            tag, offset, _, _ = struct.unpack_from('<4I', data, 80 + index * 16)
            if tag == 2:
                data[offset:offset + 64] = b'broken'.ljust(64, b'\0')
        (self.native/'broken.rk').write_bytes(data)
        (self.native/'broken.rkm').write_text((self.native/'flag.rkm').read_text()
            .replace('DiffuseTexture=flag', 'DiffuseTexture=broken')
            .replace('NoCompress=true', 'NoCompress=false'))
        self.objects = write_native_tables(self.native, {
            'Synthetic_Flag': {'model':'flag', 'animation':'flag', 'events':'flag'},
            'Synthetic_Broken': {'model':'broken', 'animation':'flag', 'events':'flag'}})
        # Format 0 is a valid PVR format unsupported by current Luna; RGBA8 with
        # no pixel payload exercises its separate malformed-image ValueError.
        for label, pixel_format, channel_bits in [
            ('unsupported', struct.pack('<I', 0), bytes(4)),
            ('truncated', b'rgba', bytes([8] * 4)),
        ]:
            with self.subTest(label=label):
                header = struct.pack('<4sI4s4s9I', b'PVR\x03', 0, pixel_format,
                    channel_bits, 0, 0, 2, 2, 1, 1, 1, 1, 0)
                (self.native/'broken.pvr').write_bytes(header)
                report = self.generate()
                self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
                exclusion = report['generation']['excluded']['Synthetic_Broken']
                self.assertEqual(exclusion['code'], 'NativeInputError')
                self.assertIn('Native PVR parse failed', exclusion['reason'])

    def test_all_fail_preserves_previous_catalogue(self):
        previous = self.seed_generation()
        (self.native/'flag.rk').unlink()
        with self.assertRaisesRegex(ValueError, 'No verified basic'):
            self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
        self.assertEqual(json.loads(self.report.read_text())['generation']['converted'], 0)
        self.assertTrue(json.loads(self.report.read_text())['generation']['status'].startswith('failed'))

    def test_invalid_declared_action_is_excluded_without_invalidating_rest(self):
        (self.native/'flag.csv').write_text('rest,0,0,15\nwave,0,45,15\nwave,0,99,15\n')
        report = self.generate()
        row = self.assert_closed()['models']['Synthetic_Flag']
        self.assertEqual([clip['id'] for clip in row['animations']], ['rest'])
        excluded = report['identities']['Synthetic_Flag']['excluded_clips'][0]
        self.assertEqual(excluded['name'], 'wave')
        self.assertEqual(excluded['reason'], 'invalid_native_csv_clip')
        self.assertEqual(excluded['csv_diagnostics'], [
            {'line': 3, 'name': 'wave', 'reason': 'out-of-range frames or invalid FPS'}])
        self.assertEqual(report['csv_libraries']['flag.csv']['excluded_clip_names'], ['wave'])

    def test_invalid_rest_never_uses_other_valid_action_as_fallback(self):
        previous = self.seed_generation()
        (self.native/'flag.csv').write_text('rest,0,0,15\nrest,0,0,nan\nwave,0,45,15\n')
        with self.assertRaisesRegex(ValueError, 'No verified basic'):
            self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['generation']['excluded']['Synthetic_Flag']['code'], 'declared_idle_not_in_csv')

    def test_generated_key_digest_conflict_aborts_before_catalogue_switch(self):
        previous = self.seed_generation()
        convert = model_gen.convert_native
        def invalid_bundle(*args, **kwargs):
            bundle = convert(*args, **kwargs)
            files = dict(bundle.files)
            key = next(iter(files))
            files[key] += b'corruption'
            return replace(bundle, files=files)
        with patch.object(model_gen, 'convert_native', side_effect=invalid_bundle):
            with self.assertRaisesRegex(ValueError, 'invalid asset'):
                self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)

    def test_existing_immutable_hash_conflict_refuses_overwrite(self):
        previous = self.seed_generation()
        ref = self.catalogue()['models']['Synthetic_Flag']['animations'][0]
        (self.dist/ref['path']).write_bytes(b'externally corrupted file')
        with self.assertRaisesRegex(ValueError, 'Existing immutable GLB differs'):
            self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
        self.assertEqual((self.dist/ref['path']).read_bytes(), b'externally corrupted file')

    def test_catalogue_replace_failure_preserves_old_catalogue_and_reports_failure(self):
        previous = self.seed_generation()
        original = Path.replace
        def fail_catalogue(source, target):
            if Path(target) == self.dist/'models_3d.json':
                raise OSError('injected catalogue replace failure')
            return original(source, target)
        with patch.object(Path, 'replace', new=fail_catalogue):
            with self.assertRaisesRegex(OSError, 'injected catalogue'):
                self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
        self.assertTrue(json.loads(self.report.read_text())['generation']['status'].startswith('failed'))

    def test_private_report_rejected_inside_dist_before_generation(self):
        with self.assertRaisesRegex(ValueError, 'Private 3D report'):
            model_gen.generate_models(self.objects, self.native, self.dist, self.dist/'audit.json', campaign={})
        self.assertFalse(self.dist.exists())

    def test_no_private_inputs_or_provenance_published_and_ordinary_file_unchanged(self):
        self.dist.mkdir()
        (self.dist/'ordinary.txt').write_bytes(b'preserve this unrelated asset')
        self.generate()
        self.assertEqual((self.dist/'ordinary.txt').read_bytes(), b'preserve this unrelated asset')
        actual = {p.relative_to(self.dist).as_posix() for p in self.dist.rglob('*') if p.is_file()}
        self.assertTrue(all(name in {'ordinary.txt','models_3d.json'} or model_gen.is_asset_path(name)
                            for name in actual))
        serialized = (self.dist/'models_3d.json').read_text()
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn('provenance', serialized)

    def test_shared_animation_reused_without_merging_object_identity(self):
        declarations = {identity: {'model':'flag','animation':'flag','events':'flag'}
                        for identity in ['Synthetic_Flag', 'Synthetic_Other']}
        self.objects = write_native_tables(self.native, declarations)
        report = self.generate()
        rows = self.assert_closed()['models']
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows['Synthetic_Flag']['path'], rows['Synthetic_Other']['path'])
        self.assertEqual(rows['Synthetic_Flag']['animations'], rows['Synthetic_Other']['animations'])
        unique = {ref['path'] for row in rows.values() for ref in [row,*row['animations']]}
        self.assertEqual(report['generation']['asset_count'], len(unique))
        self.assertEqual(len(list((self.dist/'models/3d').rglob('*.glb'))), len(unique))

    def test_only_public_game_data_identities_are_published(self):
        self.objects = write_native_tables(self.native, {identity: {'model':'flag','animation':'flag','events':'flag'}
                            for identity in ['Synthetic_Flag', 'Synthetic_Private']})
        report = self.generate(public_identities={'Synthetic_Flag'})
        self.assertEqual(set(self.catalogue()['models']), {'Synthetic_Flag'})
        self.assertFalse(report['identities']['Synthetic_Private']['converted'])

    def test_all_public_ids_missing_from_raw_preserves_previous_catalogue(self):
        previous = self.seed_generation()
        with self.assertRaisesRegex(ValueError, 'No verified basic'):
            self.generate(public_identities={'Synthetic_Missing'})
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['generation']['excluded']['Synthetic_Missing']['code'], 'missing_native_identity')
        self.assertEqual(report['generation']['converted'], 0)

    def test_partial_public_id_absence_is_explicit_without_guessing_resources(self):
        report = self.generate(public_identities={'Synthetic_Flag', 'Synthetic_Missing'})
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Missing']['code'], 'missing_native_identity')
        self.assertEqual(report['generation']['converted'], 1)

    def test_visual_campaign_override_excludes_only_affected_identity(self):
        self.objects = write_native_tables(self.native, {identity: {'model':'flag','animation':'flag','events':'flag'}
                            for identity in ['Synthetic_Flag', 'Synthetic_Override']})
        campaign = {'game_object_data': {'Pony': {'Synthetic_Override': {'Model': {'MediumLOD':'other'}}}}}
        report = self.generate(campaign=campaign)
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Override']['code'],
                         'campaign_visual_override_unverified')

    def test_special_ai_campaign_override_excludes_affected_actor(self):
        self.objects = write_native_tables(self.native, {identity: {'model':'flag','animation':'flag','events':'flag'}
                            for identity in ['Synthetic_Flag', 'Synthetic_Override']})
        schema = ET.parse(self.native/'gameobjectcategorydata.xml')
        pony = schema.getroot().find("./GameObjectCategory[@Name='Pony']")
        ET.SubElement(ET.SubElement(pony, 'Parameter', Name='AI'),
                      'Attribute', Name='Special_AI', Type='int')
        schema.write(self.native/'gameobjectcategorydata.xml', encoding='utf-8')
        tree = ET.parse(self.native/'gameobjectdata.xml')
        for obj in tree.findall("./Category[@ID='Pony']/GameObject"):
            ET.SubElement(obj, 'AI', Special_AI='0')
        tree.write(self.native/'gameobjectdata.xml', encoding='utf-8')
        self.objects = GameObjectData(self.native/'gameobjectdata.xml', self.native/'shopdata.xml',
                                      self.native/'gameobjectcategorydata.xml')
        self.assertIs(type(self.objects['Pony']['Synthetic_Override']['AI']['Special_AI']), int)
        campaign = {'game_object_data': {'Pony': {'Synthetic_Override': {'AI': {'Special_AI': 2}}}}}
        report = self.generate(campaign=campaign)
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
        self.assertEqual(report['generation']['excluded']['Synthetic_Override']['code'],
                         'campaign_visual_override_unverified')

    def test_nonvisual_campaign_override_does_not_change_model_output(self):
        previous = self.seed_generation()
        self.generate(campaign={'game_object_data': {'Pony': {'Synthetic_Flag': {'PurchasePrice':42}}}})
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)

    def test_repeat_generation_stable_and_stale_resources_only_reported(self):
        previous = self.seed_generation()
        stale = self.dist/('models/3d/'+'f'*64+'.glb')
        stale.write_bytes(b'old source-owned file retained, never uploaded by current catalogue')
        report = self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
        self.assertEqual(report['generation']['unreferenced_local_assets'], [stale.relative_to(self.dist).as_posix()])
        self.assertTrue(stale.exists())

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

    def test_outfit_without_link_only_overrides_explicit_body(self):
        self.prepare_wardrobe(linked_tail=False)
        self.generate(public_identities={'Synthetic_Outfit'})
        # tail1 is not registered by a part reference without the link, so it is
        # ordinary RK geometry. The declared default tail remains selected.
        self.assertEqual(self.model_mesh_names('Synthetic_Outfit'),
                         {'flag', 'body1', 'mane0', 'tail0', 'tail1', 'uncontrolled_trim'})

    def test_campaign_part_visual_changes_exclude_dependents_but_keep_unrelated_actor(self):
        self.prepare_wardrobe()
        for field, value in [('ModelName', 'different_mesh'), ('Type', 'Mane'), ('LinkedPart', '')]:
            with self.subTest(field=field):
                campaign = {'game_object_data': {'PonyPart': {'body1': {'PonyPart': {field: value}}}}}
                report = self.generate(campaign=campaign,
                    public_identities={'Synthetic_Flag', 'Synthetic_Outfit', 'Synthetic_Unrelated'})
                self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Unrelated'})
                for identity in ['Synthetic_Flag', 'Synthetic_Outfit']:
                    self.assertEqual(report['generation']['excluded'][identity]['code'], 'campaign_visual_override_unverified')

    def test_ambiguous_outfit_owner_is_not_guessed_even_for_identical_rigs(self):
        self.prepare_wardrobe(second_owner=True)
        report = self.generate(public_identities={'Synthetic_Outfit', 'Synthetic_Unrelated'})
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Unrelated'})
        self.assertEqual(report['identities']['Synthetic_Outfit']['owners'], ['Synthetic_Flag', 'Synthetic_SecondOwner'])
        self.assertEqual(report['generation']['excluded']['Synthetic_Outfit']['code'], 'unresolved_association')

    def test_pet_action_campaign_overrides_exclude_only_the_affected_pet(self):
        self.prepare_pet()
        public = {'Synthetic_Flag', 'Synthetic_Pet'}
        self.generate(public_identities=public)
        self.assertEqual(set(self.assert_closed()['models']), public)
        for field in ['AnimationWalk', 'AnimationRun', 'AnimationEditorIdle']:
            with self.subTest(field=field):
                campaign = {'game_object_data': {'PonyPet': {
                    'Synthetic_Pet': {'Object': {field: 'changed_native_action'}}}}}
                report = self.generate(campaign=campaign, public_identities=public)
                self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Flag'})
                self.assertEqual(report['generation']['converted'], 1)
                self.assertEqual(report['generation']['excluded']['Synthetic_Pet']['code'],
                                 'campaign_visual_override_unverified')
                self.assertFalse(report['identities']['Synthetic_Pet']['converted'])
                self.assertTrue(report['identities']['Synthetic_Flag']['converted'])

    def test_pet_empty_idle_uses_verified_original_first_csv_name(self):
        self.prepare_pet()
        report = self.generate(public_identities={'Synthetic_Pet'})
        self.assertEqual(set(self.assert_closed()['models']), {'Synthetic_Pet'})
        self.assertEqual(report['csv_libraries']['flag.csv']['first_declared_name'], 'rest')
        self.assertEqual(report['identities']['Synthetic_Pet']['selection']['rest']['name'], 'rest')
        self.assertEqual(self.model_mesh_names('Synthetic_Pet'), {'flag'})

    def test_pet_empty_idle_never_skips_invalid_or_blank_original_first_record(self):
        self.prepare_pet()
        self.generate(public_identities={'Synthetic_Pet'})
        previous = (self.dist/'models_3d.json').read_bytes()
        for text, code in [
            ('broken,0,99,15\nrest,0,0,15\nwave,0,45,15\n', 'declared_idle_not_in_csv'),
            ('rest,0,0,15\nwave,0,45,15\nrest,1,1,15\n', 'declared_idle_not_in_csv'),
            ('\nrest,0,0,15\nwave,0,45,15\n', 'original_first_csv_clip_unverified'),
        ]:
            with self.subTest(text=text):
                (self.native/'flag.csv').write_text(text)
                with self.assertRaisesRegex(ValueError, 'No verified basic'):
                    self.generate(public_identities={'Synthetic_Pet'})
                self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
                report = json.loads(self.report.read_text())
                self.assertEqual(report['generation']['excluded']['Synthetic_Pet']['code'], code)


    def test_named_generation_keeps_legacy_catalogue_assets_reachable(self):
        real = model_gen.convert_native
        def legacy(*args, **options):
            options.pop('model_filename', None)
            options.pop('animation_filenames', None)
            return real(*args, **options)
        with patch.object(model_gen, 'convert_native', side_effect=legacy):
            self.generate()
        old = self.catalogue()['models']['Synthetic_Flag']
        old_files = {ref['path']: (self.dist/ref['path']).read_bytes() for ref in [old, *old['animations']]}
        report = self.generate()
        new = self.assert_closed()['models']['Synthetic_Flag']
        for before, after, name in [(old, new, 'Synthetic_Flag'), *[(a,b,a['id']) for a,b in zip(old['animations'],new['animations'])]]:
            self.assertEqual(after['path'], (f'models/3d/{name}.glb' if name == 'Synthetic_Flag'
                                           else f"models/3d/{after['sha256']}/{name}.glb"))
            self.assertEqual((self.dist/after['path']).read_bytes(), old_files[before['path']])
            self.assertEqual((self.dist/before['path']).read_bytes(), old_files[before['path']])
        self.assertEqual(set(report['generation']['unreferenced_local_assets']), set(old_files))

    def test_fixed_revisions_replace_previous_object_asset(self):
        self.generate()
        old = self.catalogue()['models']['Synthetic_Flag']
        before = (self.dist/old['path']).read_bytes()
        source = self.native/'flag.anim'
        data = bytearray(source.read_bytes());struct.pack_into('<f', data, 92+16, 3.)
        source.write_bytes(data)
        self.generate()
        new = self.assert_closed()['models']['Synthetic_Flag']
        self.assertNotEqual(old['sha256'], new['sha256'])
        self.assertEqual(Path(old['path']).name, 'Synthetic_Flag.glb')
        self.assertEqual(Path(new['path']).name, 'Synthetic_Flag.glb')
        self.assertEqual(new['path'], old['path'])
        self.assertNotEqual((self.dist/old['path']).read_bytes(), before)
        self.assertEqual(hashlib.sha256((self.dist/new['path']).read_bytes()).hexdigest(), new['sha256'])

    def test_fixed_catalogue_failure_can_leave_replaced_model_with_old_catalogue(self):
        previous = self.seed_generation()
        old = self.catalogue()['models']['Synthetic_Flag']
        before = (self.dist/old['path']).read_bytes()
        source = self.native/'flag.anim'
        data = bytearray(source.read_bytes()); struct.pack_into('<f', data, 92+16, 3.)
        source.write_bytes(data)
        replace_file = Path.replace
        def fail_catalogue(source, target):
            if Path(target) == self.dist/'models_3d.json':
                raise OSError('injected final catalogue failure')
            return replace_file(source, target)
        with patch.object(Path, 'replace', new=fail_catalogue):
            with self.assertRaisesRegex(OSError, 'injected final catalogue'):
                self.generate()
        self.assertEqual((self.dist/'models_3d.json').read_bytes(), previous)
        after = (self.dist/old['path']).read_bytes()
        self.assertNotEqual(after, before)
        self.assertNotEqual(hashlib.sha256(after).hexdigest(), old['sha256'])
        self.assertTrue(json.loads(self.report.read_text())['generation']['status'].startswith('failed'))

    def test_named_invalid_object_is_diagnosed_without_blocking_valid_identity(self):
        self.objects = write_native_tables(self.native, {
            'Synthetic_Flag': {'model':'flag','animation':'flag','events':'flag'},
            'Bad/Identity': {'model':'flag','animation':'flag','events':'flag'}})
        report = self.generate()
        self.assertEqual(set(self.catalogue()['models']), {'Synthetic_Flag'})
        self.assertIn('filename', report['generation']['excluded']['Bad/Identity']['reason'])


if __name__ == '__main__':
    unittest.main()
