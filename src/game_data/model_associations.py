"""Audit native 3D references before enabling automatic GLB generation.

Consumes Luna GameObjectData already owned by Transformer. This is deliberately
not a converter or a claim that an extracted folder is a complete game release.
The report belongs in raw/, never in the public dist/ or models_3d.json.
"""
from collections import Counter
import hashlib
import json
import struct
from pathlib import Path
import tempfile


class AssociationError(ValueError):
    pass


def _stem(value):
    # The inspected native fields contain flat resource stems. A changed source
    # must be reviewed rather than flattened, case-folded or searched by prefix.
    if (not isinstance(value, str) or not value or value in {'.', '..'}
            or any(c in value for c in '/\\\0') or any(ord(c) < 32 for c in value)):
        raise AssociationError('Missing or unsafe native resource stem')
    return value


def _file(folder, name):
    _stem(name)
    path = folder / name
    if not path.is_file():
        raise AssociationError('Missing extracted dependency: ' + name)
    if path.resolve().parent != folder.resolve():
        raise AssociationError('Dependency leaves the extracted directory: ' + name)
    # Do not silently select different case variants on case-insensitive hosts.
    variants = [p.name for p in folder.iterdir() if p.name.casefold() == name.casefold()]
    if variants != [name]:
        raise AssociationError('Ambiguous or non-exact dependency spelling: ' + name)
    return path


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _model(path):
    from luna_kit.model import RKModel
    from .model_gen.converter.native import _validate_model_layout
    # Luna assumes required sections exist; reject unsupported layouts per resource.
    _validate_model_layout(path)
    model = RKModel(str(path))
    bones = model.bones
    if not bones or [b.index for b in bones] != list(range(len(bones))):
        raise AssociationError('Invalid native bone indices')
    if any(b.parentIndex < -1 or b.parentIndex >= b.index for b in bones):
        raise AssociationError('Unsupported or cyclic native hierarchy')
    return {'bone_count': len(bones), 'parents': [b.parentIndex for b in bones],
            'bone_names': [b.name for b in bones],
            'meshes': [m.name for m in model.meshes]}


def _animation(path):
    # Reuse Luna's header schema, without invoking its fixed type-4 frame
    # decoder on type-0/type-1 records. Frame conversion remains a separate gate.
    import dataclasses_struct as dcs
    from luna_kit.model.anim import Header
    size = dcs.get_struct_size(Header)
    with path.open('rb') as stream:
        raw = stream.read(size)
    if len(raw) != size:
        raise AssociationError('Truncated ANIM header')
    header = Header.from_packed(raw)
    strides = {0: 32, 1: 22, 4: 11}
    if (header.magic != b'RKFORMAT' or (header.version_major, header.version_minor) != (5, 2)
            or header.frame_type not in strides):
        raise AssociationError('Unsupported ANIM header/layout')
    if (not header.bone_count or not header.frame_count or
            path.stat().st_size != size + header.bone_count * header.frame_count * strides[header.frame_type]):
        raise AssociationError('ANIM byte length does not match its header')
    name = header.name.split(b'\0', 1)[0].decode('ascii').strip()
    _stem(name)
    return {'bone_count': header.bone_count, 'frame_count': header.frame_count,
            'frame_type': header.frame_type, 'embedded_name': name}


def _clips(path, frame_count):
    # Resolve the explicit native library's CSV before validating its rows.
    from .model_gen.converter.csv import CsvError, read_clips
    try:
        metadata = {}
        clips, duplicates, diagnostics = read_clips(path, frame_count, allow_partial=True,
                                                   source_metadata=metadata)
        return {'clips': clips, 'duplicate_rows_merged': duplicates, 'diagnostics': diagnostics,
                'excluded_clip_names': sorted({row['name'] for row in diagnostics if row['name']}),
                **metadata}
    except CsvError as error:
        raise AssociationError(str(error)) from error


def _sets(pony, sets):
    from .model_gen.wardrobe import WardrobeError, registered_sets
    try:
        return registered_sets(pony, sets)
    except WardrobeError as error:
        raise AssociationError(str(error)) from error


def _parts(outfit, parts):
    from .model_gen.wardrobe import WardrobeError, resolve_set_parts
    try:
        return resolve_set_parts(outfit, parts)[1]
    except WardrobeError as error:
        raise AssociationError(str(error)) from error


def discover_associations(game_objects, extracted_dir):
    """Return traceable declarations and blockers; never mark a GLB successful.

    Paths are resolved only from explicit fields. Bone count is a rejection
    check after association, never a discovery method or proof of retargeting.
    External library stem + .anim/.csv follows the inspected native loader;
    see docs/3d-resource-associations.md for evidence and version boundaries.
    """
    folder = Path(extracted_dir)
    rows, model_cache, anim_cache, csv_cache = {}, {}, {}, {}
    ponies, pets = game_objects.get('Pony', {}), game_objects.get('PonyPet', {})
    sets, parts = game_objects.get('PonySet', {}), game_objects.get('PonyPart', {})
    owners, set_errors = {}, {}
    for identity, pony in ponies.items():
        try:
            for outfit in _sets(pony, sets):
                owners.setdefault(outfit, []).append(identity)
        except AssociationError as error:
            set_errors[identity] = str(error)

    def inspect(name, cache, reader):
        if name not in cache:
            try:
                path = _file(folder, name)
                cache[name] = {'file': name, 'sha256': _sha(path), **reader(path)}
            except (ValueError, OSError, AssertionError, IndexError, struct.error, ZeroDivisionError) as error:
                cache[name] = {'file': name, 'error': str(error)}
        if 'error' in cache[name]:
            raise AssociationError(cache[name]['error'])
        return cache[name]

    def inspect_csv(stem, library):
        key = (stem + '.csv', library['frame_count'])
        if key not in csv_cache:
            try:
                path = _file(folder, key[0])
                csv_cache[key] = {'file': key[0], 'sha256': _sha(path),
                                 **_clips(path, library['frame_count'])}
            except (ValueError, OSError) as error:
                csv_cache[key] = {'file': key[0], 'error': str(error)}
        return csv_cache[key]

    def additional_actor(declaration):
        # The retained native create path uses AdditionalObject.Model for both
        # model and library. Animation is retained as source data only: current
        # declarations contain both clip names and non-clip labels in that field.
        actor = {'source_field': 'AdditionalObject.Model',
                 'declaration': dict(declaration), 'relation': 'independent-actor',
                 'references_resolved': False, 'renderer_supported': False,
                 'native_playback': {'initial_library_index': 0,
                                     'after_completion': 'random-library-index',
                                     'evidence_client_version': '11.4.2a'}}
        try:
            stem = _stem(declaration['Model'])
            actor['resource_stem'] = stem
            model = inspect(stem + '.rk', model_cache, _model)
            library = inspect(stem + '.anim', anim_cache, _animation)
            actor['model'], actor['animation'] = model['file'], library['file']
            if model['bone_count'] != library['bone_count']:
                raise AssociationError('Additional actor model/ANIM bone counts differ')
            actor['bone_count_check'] = 'equal; association comes from native loading, not bone counts'
            csv = inspect_csv(stem, library)
            actor['csv'] = {k: v for k, v in csv.items()
                            if k not in {'clips', 'diagnostics', 'excluded_clip_names'}}
            if 'error' in csv:
                raise AssociationError(csv['error'])
            actor['csv']['clip_count'] = len(csv['clips'])
            actor['csv']['diagnostic_count'] = len(csv['diagnostics'])
            # Do not map native numeric indices onto filtered/deduplicated rows.
            # Discovery records sources; generation validates the full controller.
            actor['references_resolved'] = True
        except (ValueError, OSError) as error:
            actor['error'] = str(error)
        return actor

    for category, objects in [('Pony', ponies), ('PonyPet', pets)]:
        for identity, obj in objects.items():
            row = {'category': category, 'references_resolved': False, 'converted': False,
                   'blockers': ['source_release_closure_unverified', 'visibility_unverified',
                                'event_source_unverified', 'frame_conversion_not_run']}
            if identity in rows:
                raise AssociationError('Identity reused across 3D categories: ' + identity)
            rows[identity] = row
            try:
                if category == 'Pony':
                    model, anim = obj.get('Model', {}).get('MediumLOD'), obj.get('Animation', {}).get('Rig_MediumLOD')
                    row['fields'] = {'model': 'Model.MediumLOD', 'animation': 'Animation.Rig_MediumLOD'}
                    row['idle'] = {'field': 'Conversation.IdleAnimation',
                                   'value': obj.get('Conversation', {}).get('IdleAnimation'),
                                   'context': 'conversation; viewer initial pose still requires validation'}
                    row['events'] = obj.get('Animation', {}).get('AnimationEventsXML')
                    row['eyes'] = obj.get('Eyes')  # None and explicit empty arrays are different.
                    row['sets'] = _sets(obj, sets)
                    row['additional_object'] = obj.get('AdditionalObject')
                    additional = row['additional_object'] or {}
                    if additional.get('Model'):
                        row['additional_actor'] = additional_actor(additional)
                        row['blockers'].append('additional_actor_conversion_not_run')
                        if not row['additional_actor']['references_resolved']:
                            row['blockers'].append('additional_actor_dependency_unresolved')
                else:
                    model, anim = obj.get('Object', {}).get('Model'), obj.get('Object', {}).get('Animation')
                    row['fields'] = {'model': 'Object.Model', 'animation': 'Object.Animation'}
                    row['idle'] = {'field': 'Object.AnimationIdle', 'value': obj.get('Object', {}).get('AnimationIdle')}
                    row['events'] = None
                model, anim = _stem(model), _stem(anim)
                row['declaration'] = {'model': model, 'animation': anim}
                info = inspect(model + '.rk', model_cache, _model)
                library = inspect(anim + '.anim', anim_cache, _animation)
                row['model'], row['animation'] = info['file'], library['file']
                if info['bone_count'] != library['bone_count']:
                    raise AssociationError('Declared model/ANIM bone counts differ')
                row['bone_count_check'] = 'equal; ANIM does not provide bone names or hierarchy'
                row['model_animation_resolved'] = True
                # The native loader appends .anim and .csv to the same caller
                # stem. Embedded ANIM names are authoring metadata and can point
                # to another library; never select that library as a fallback.
                row['csv_source'] = {'field': row['fields']['animation'],
                                     'rule': 'declared-library-stem-plus-csv',
                                     'embedded_name': library['embedded_name']}
                key = (anim + '.csv', library['frame_count'])
                inspect_csv(anim, library)
                row['csv'] = {k: v for k, v in csv_cache[key].items()
                              if k not in {'clips', 'diagnostics', 'excluded_clip_names'}}
                if 'clips' in csv_cache[key]:
                    row['csv']['clip_count'] = len(csv_cache[key]['clips'])
                    row['csv']['diagnostic_count'] = len(csv_cache[key]['diagnostics'])
                    row['csv']['excluded_clip_count'] = len(csv_cache[key]['excluded_clip_names'])
                if 'error' in row['csv']:
                    row['blockers'].append('invalid_or_missing_csv')
                else:
                    row['references_resolved'] = True
                idle = row['idle']['value']
                if not idle:
                    row['blockers'].append('initial_pose_not_declared')
                elif 'clips' in csv_cache[key] and idle not in {c['name'] for c in csv_cache[key]['clips']}:
                    row['blockers'].append('declared_idle_not_in_csv')
            except (ValueError, OSError) as error:
                row['error'] = str(error)
                row['blockers'].append('unresolved_dependency')

    for identity, outfit in sets.items():
        row = {'category': 'PonySet', 'owners': sorted(owners.get(identity, [])),
               'references_resolved': False, 'converted': False, 'blockers': ['outfit_visibility_unverified']}
        if identity in rows:
            raise AssociationError('Identity reused across 3D categories: ' + identity)
        rows[identity] = row
        try:
            row['parts'] = _parts(outfit, parts)
            if len(row['owners']) != 1 or set_errors:
                # A malformed owner's set graph can conceal another owner.
                raise AssociationError('No unique, complete native outfit ownership')
            owner = rows[row['owners'][0]]
            if not owner['references_resolved']:
                raise AssociationError('Outfit owner has unresolved resources')
            meshes = model_cache[owner['model']]['meshes']
            # The native mesh-name controller is a no-op for absent names.
            # Preserve that declaration (including empty-tail parts), never
            # choose a similarly named mesh or restore the default part.
            row['absent_declared_meshes'] = sorted({part['mesh'] for part in row['parts'].values()
                                                   if part['mesh'] not in meshes})
            row['model'], row['animation'] = owner['model'], owner['animation']
            row['csv'] = dict(owner['csv'])
            row['declaration'] = dict(owner['declaration'])
            row['owner'] = row['owners'][0]
            row['references_resolved'] = True
            row['blockers'] += owner['blockers']
        except (ValueError, OSError) as error:
            row['error'] = str(error)
            row['blockers'].append('unresolved_outfit')

    schema = getattr(game_objects, 'CATEGORY_DATA', {})
    return {'schemaVersion': 1, 'purpose': 'association-audit-only', 'conversion_enabled': False,
            'summary': {'identities': len(rows), 'references_resolved': sum(r['references_resolved'] for r in rows.values()),
                        'model_animation_resolved': sum(r.get('model_animation_resolved', False) for r in rows.values()),
                        'converted': 0, 'categories': dict(Counter(r['category'] for r in rows.values())),
                        'additional_actors': sum('additional_actor' in r for r in rows.values()),
                        'additional_actor_references_resolved': sum(
                            r.get('additional_actor', {}).get('references_resolved', False) for r in rows.values()),
                        'blockers': dict(Counter(b for r in rows.values() for b in set(r['blockers'])))},
            'eye_schema': schema.get('Pony', {}).get('Eyes'), 'set_graph_errors': set_errors,
            'models': model_cache, 'animation_libraries': anim_cache,
            'csv_libraries': {name: data for (name, _frames), data in csv_cache.items()}, 'identities': rows}


def write_association_report(game_objects, extracted_dir, output, *, public_dir, report=None):
    """Atomically write a local audit, refusing paths inside public dist/."""
    folder, output = Path(extracted_dir), Path(output)
    if output.resolve().is_relative_to(Path(public_dir).resolve()):
        raise AssociationError('Association audit must stay outside the public output directory')
    if report is None:
        report = discover_associations(game_objects, folder)
    report['source_tables'] = {name: _sha(folder / name) for name in
                             ['gameobjectdata.xml', 'gameobjectcategorydata.xml', 'shopdata.xml']}
    campaign = folder / 'defaultGameCampaign.json'
    report.setdefault('campaign', {})['present'] = campaign.is_file()
    report['campaign'].setdefault('render_overrides_reviewed', False)
    if campaign.is_file():
        report['campaign']['sha256'] = _sha(campaign)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(report, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            stream.write('\n')
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(output)
    finally:
        temp.unlink(missing_ok=True)
    return report['summary']
