"""Resolve native 3D declarations before model conversion.

Consumes Luna GameObjectData already owned by Transformer. This is deliberately
not a converter or a claim that an extracted folder is a complete game release.
The report belongs in raw/, never in the public dist/ or models_3d.json.
"""
from collections import Counter
from collections.abc import Callable, Mapping
import json
import struct
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from luna_kit.gameobjectdata import GameObject, GameObjectData


# Native TwilightPrincess_3_Transformation uses this model after gaining wings.
# Keep the original declaration for its controller and animation-library context.
DISPLAY_MODELS = {'Pony_Twilight_Sparkle': 'pony_type01_twilightprincess_lod1'}


class AssociationError(ValueError):
    pass


def _stem(value: object) -> str:
    # The inspected native fields contain flat resource stems. A changed source
    # must be reviewed rather than flattened, case-folded or searched by prefix.
    if (not isinstance(value, str) or not value or value in {'.', '..'}
            or any(c in value for c in '/\\\0') or any(ord(c) < 32 for c in value)):
        raise AssociationError('Missing or unsafe native resource stem')
    return value


def _file(folder: Path, name: str, name_index: dict[str, list[str]]) -> Path:
    _stem(name)
    path = folder / name
    if not path.is_file():
        raise AssociationError('Missing extracted dependency: ' + name)
    if path.resolve().parent != folder.resolve():
        raise AssociationError('Dependency leaves the extracted directory: ' + name)
    # Do not silently select different case variants on case-insensitive hosts.
    if not name_index:
        for entry in folder.iterdir():
            name_index.setdefault(entry.name.casefold(), []).append(entry.name)
    variants = name_index.get(name.casefold(), [])
    if variants != [name]:
        raise AssociationError('Ambiguous or non-exact dependency spelling: ' + name)
    return path


def _animation(path: Path) -> dict:
    from .model_gen.converter.native_animation import read_animation_header
    try:
        header = read_animation_header(path)
    except ValueError as error:
        raise AssociationError(str(error)) from error
    name = header.name.split(b'\0', 1)[0].decode('ascii').strip()
    _stem(name)
    return {'bone_count': header.bone_count, 'frame_count': header.frame_count,
            'frame_type': header.frame_type, 'embedded_name': name}


def _clips(path: Path, frame_count: int) -> dict:
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


def _parts(outfit: GameObject, parts: dict[str, GameObject]) -> dict[str, dict[str, str]]:
    from .model_gen.wardrobe import WardrobeError, resolve_set_parts
    try:
        return resolve_set_parts(outfit, parts)[1]
    except WardrobeError as error:
        raise AssociationError(str(error)) from error


def discover_associations(game_objects: GameObjectData, extracted_dir: str | Path, *,
                          outfit_owners: Mapping[str, str] | None = None) -> dict:
    """Return traceable declarations and blockers; never mark a GLB successful.

    Paths are resolved only from explicit fields. RK parsing, layout and bone
    compatibility checks belong to conversion, not this declaration pass.
    External library stem + .anim/.csv follows the inspected native loader;
    see docs/3d-resource-associations.md for evidence and version boundaries.
    """
    from .model_gen.wardrobe import inspect_registered_sets

    folder = Path(extracted_dir)
    outfit_owners = outfit_owners or {}
    rows, model_cache, anim_cache, csv_cache = {}, {}, {}, {}
    # Extracted inputs stay fixed during one discovery; refresh on the next call.
    name_index: dict[str, list[str]] = {}
    ponies, pets = game_objects.get('Pony', {}), game_objects.get('PonyPet', {})
    sets, parts = game_objects.get('PonySet', {}), game_objects.get('PonyPart', {})
    owners, set_errors = {}, {}

    def inspect(name: str, cache: dict[str, dict], reader: Callable[[Path], dict] | None = None) -> dict:
        if name not in cache:
            try:
                path = _file(folder, name, name_index)
                cache[name] = {'file': name, **(reader(path) if reader is not None else {})}
            except (ValueError, OSError, struct.error) as error:
                cache[name] = {'file': name, 'error': str(error)}
        if 'error' in cache[name]:
            raise AssociationError(cache[name]['error'])
        return cache[name]

    def inspect_csv(stem: str, library: dict) -> dict:
        key = (stem + '.csv', library['frame_count'])
        if key not in csv_cache:
            try:
                path = _file(folder, key[0], name_index)
                csv_cache[key] = {'file': key[0], **_clips(path, library['frame_count'])}
            except (ValueError, OSError) as error:
                csv_cache[key] = {'file': key[0], 'error': str(error)}
        return csv_cache[key]

    def additional_actor(declaration: dict) -> dict:
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
            model = inspect(stem + '.rk', model_cache)
            library = inspect(stem + '.anim', anim_cache, _animation)
            actor['model'], actor['animation'] = model['file'], library['file']
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
            if category == 'Pony':
                row['sets'], set_error = inspect_registered_sets(obj, sets)
                for outfit in row['sets']:
                    owners.setdefault(outfit, []).append(identity)
                if set_error is not None:
                    set_errors[identity] = str(set_error)
            try:
                if category == 'Pony':
                    model, anim = obj.get('Model', {}).get('MediumLOD'), obj.get('Animation', {}).get('Rig_MediumLOD')
                    row['fields'] = {'model': 'Model.MediumLOD', 'animation': 'Animation.Rig_MediumLOD'}
                    row['idle'] = {'field': 'Conversation.IdleAnimation',
                                   'value': obj.get('Conversation', {}).get('IdleAnimation'),
                                   'context': 'conversation; viewer initial pose still requires validation'}
                    row['events'] = obj.get('Animation', {}).get('AnimationEventsXML')
                    row['eyes'] = obj.get('Eyes')  # None and explicit empty arrays are different.
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
                model = _stem(model)
                # Keep the selected RK available to standalone-part candidate searches,
                # even when this Pony's library or wardrobe registration is invalid.
                row['model'] = DISPLAY_MODELS.get(identity, model) + '.rk'
                inspect(row['model'], model_cache)
                anim = _stem(anim)
                row['declaration'] = {'model': model, 'animation': anim}
                if identity in set_errors:
                    raise AssociationError(set_errors[identity])
                library = inspect(anim + '.anim', anim_cache, _animation)
                row['animation'] = library['file']
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
            owner_scope = {outfit_owners[identity]} if identity in outfit_owners else set(ponies)
            if owner_scope.intersection(set_errors):
                raise AssociationError('Incomplete native outfit registration in the requested owner scope')
            candidates = owner_scope.intersection(row['owners'])
            if len(candidates) != 1:
                raise AssociationError('No verified native outfit owner for the requested context')
            owner_id = candidates.pop()
            owner = rows[owner_id]
            if not owner['references_resolved']:
                raise AssociationError('Outfit owner has unresolved resources')
            row['model'], row['animation'] = owner['model'], owner['animation']
            row['csv'] = dict(owner['csv'])
            row['declaration'] = dict(owner['declaration'])
            row['owner'] = owner_id
            if identity in outfit_owners:
                row['owner_source'] = 'public.costume.pony'
            row['references_resolved'] = True
            row['blockers'] += owner['blockers']
        except (ValueError, OSError) as error:
            row['error'] = str(error)
            row['blockers'].append('unresolved_outfit')

    schema = getattr(game_objects, 'CATEGORY_DATA', {})
    return {'schemaVersion': 1, 'purpose': 'association-audit-only', 'conversion_enabled': False,
            'summary': {'identities': len(rows), 'references_resolved': sum(r['references_resolved'] for r in rows.values()),
                        'converted': 0, 'categories': dict(Counter(r['category'] for r in rows.values())),
                        'additional_actors': sum('additional_actor' in r for r in rows.values()),
                        'additional_actor_references_resolved': sum(
                            r.get('additional_actor', {}).get('references_resolved', False) for r in rows.values()),
                        'blockers': dict(Counter(b for r in rows.values() for b in set(r['blockers'])))},
            'eye_schema': schema.get('Pony', {}).get('Eyes'), 'set_graph_errors': set_errors,
            'models': model_cache, 'animation_libraries': anim_cache,
            'csv_libraries': {name: data for (name, _frames), data in csv_cache.items()}, 'identities': rows}


def write_association_report(game_objects: GameObjectData, extracted_dir: str | Path, output: str | Path, *,
                             public_dir: str | Path, report: dict | None = None) -> dict:
    """Atomically write a local audit, refusing paths inside public dist/."""
    folder, output = Path(extracted_dir), Path(output)
    if output.resolve().is_relative_to(Path(public_dir).resolve()):
        raise AssociationError('Association audit must stay outside the public output directory')
    if report is None:
        report = discover_associations(game_objects, folder)
    report['source_tables'] = ['gameobjectdata.xml', 'gameobjectcategorydata.xml', 'shopdata.xml']
    campaign = folder / 'defaultGameCampaign.json'
    report.setdefault('campaign', {})['present'] = campaign.is_file()
    report['campaign'].setdefault('render_overrides_reviewed', False)
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
