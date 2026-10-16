"""Generate the optional 3D catalogue inside the existing game-data build."""
from collections.abc import Collection, Iterable
import hashlib
import json
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from ..model_associations import discover_associations, write_association_report
from ..model_paths import asset_digest, is_asset_path, model_path
from .converter import ConversionError
from .converter.export import AssetBundle, convert_native
from .converter.native import NativeInputError, material_dependency_names
from .selection import SelectionError, select_native_view
from .interactions import InteractionError, prepare_interactions
from .native_emitter import load_native_emitter
from .additional import prepare_additional_actor, attach_additional_actor


if TYPE_CHECKING:
    from luna_kit.gameobjectdata import GameObjectData
    from luna_kit.typings import DefaultGameCampaignType


def _render_override(campaign: DefaultGameCampaignType, category: str, identity: str) -> bool:
    """Do not invent merge semantics for new visual campaign overrides."""
    data = campaign.get('game_object_data', {})
    if not isinstance(data, dict) or not isinstance(data.get(category, {}), dict):
        raise ValueError('Unsupported campaign game_object_data structure')
    override = data.get(category, {}).get(identity, {})
    if not isinstance(override, dict):
        raise ValueError('Unsupported campaign object override structure')
    fields = {'Model', 'MediumLOD', 'Rig_MediumLOD', 'Animation', 'AnimationEventsXML',
              'Eyes', 'Sets', 'SubSets', 'Parts', 'ModelName', 'LinkedPart',
              'AdditionalObject', 'Conversation', 'IdleAnimation', 'AnimationIdle', 'Type',
              'AnimationWalk', 'AnimationRun', 'AnimationEditorIdle', 'Special_AI', 'UserVector'}
    def visit(value: object) -> bool:
        if isinstance(value, dict):
            return any(k in fields or k.endswith('AnimationList') or visit(v) for k,v in value.items())
        if isinstance(value, list):
            return any(visit(v) for v in value)
        return False
    return visit(override)


def _check_failed_materials(folder: Path, model: dict, failed_paths: set[Path]) -> None:
    """A known extraction failure cannot use absent-RKM or suffix fallback."""
    for material in model['materials']:
        for name in material_dependency_names(material):
            path = (folder/name).resolve()
            if path in failed_paths:
                raise SelectionError('material_extraction_failed',
                    f'RKM extraction failed for {model["file"]}: {name}')
            # Match converter precedence: an existing exact source wins over
            # its optional source-art suffix alternative, including failures.
            if path.is_file():
                break


def _publish_local(stage: Path, dist: Path, catalogue: dict) -> None:
    """Publish this namespace: models may be replaced before the catalogue."""
    owned = dist/'models/3d'
    if not owned.resolve().is_relative_to(dist.resolve()):
        raise ValueError('3D output leaves dist')
    owned.mkdir(parents=True, exist_ok=True)
    fixed_models = {row['path'] for identity, row in catalogue['models'].items()
                    if row['path'] == model_path(identity)}
    for source in sorted((stage/'models/3d').rglob('*.glb')):
        key = source.relative_to(stage).as_posix()
        if asset_digest(key) is None and key not in fixed_models:
            raise ValueError('Invalid staged GLB path')
        destination = dist/key
        if not destination.resolve().is_relative_to(owned.resolve()):
            raise ValueError('3D asset leaves its namespace')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not destination.resolve().is_relative_to(owned.resolve()):
                raise ValueError('3D asset leaves its namespace')
            if destination.read_bytes() != source.read_bytes():
                if key not in fixed_models:
                    raise ValueError('Existing immutable GLB differs: ' + source.name)
                source.replace(destination)
        else:
            source.replace(destination)
    catalogue_path = dist/'models_3d.json'
    if catalogue_path.exists() and not catalogue_path.resolve().is_relative_to(dist.resolve()):
        raise ValueError('3D catalogue leaves dist')
    # Individual fixed models may already have changed if a later write fails.
    # No cross-file transaction or automatic restoration is promised.
    (stage/'models_3d.json').write_text(json.dumps(catalogue, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')) + '\n', encoding='utf-8')
    (stage/'models_3d.json').replace(catalogue_path)


def generate_models(native_objects: GameObjectData, extracted_dir: str | Path, dist_dir: str | Path,
                    report_path: str | Path, *, campaign: DefaultGameCampaignType,
                    public_identities: Collection[str] | None = None,
                    display_profile: str = 'ponybook-preferred', failed_rkms: Iterable[str] = ()) -> dict:
    """Discover and convert supported objects; all other objects get diagnostics.

    Normal callers pass Transformer inputs and public GameData IDs. There is no
    manual RK/ANIM list, downloader, archive reader, upload or event executor.
    """
    if not isinstance(campaign, dict):
        raise ValueError('The current parsed defaultGameCampaign is required')
    folder, dist, report_path = Path(extracted_dir), Path(dist_dir), Path(report_path)
    failed_rkms = set(failed_rkms)
    failed_paths = {(folder/name).resolve() for name in failed_rkms}
    if report_path.resolve().is_relative_to(dist.resolve()):
        raise ValueError('Private 3D report must stay outside public dist')
    report = discover_associations(native_objects, folder)
    report['purpose'] = 'native-association-and-generation'
    report['summary']['discovery_blockers'] = report['summary'].pop('blockers')
    report['conversion_enabled'] = True
    report['campaign'] = {'render_overrides_reviewed': True,
                          'policy': 'unverified visual overrides exclude the affected object'}
    report['source_policy'] = 'Use the existing extractor output; no independent release inventory or downloader'
    report['generation'] = {'status': 'building', 'converted': 0, 'excluded': {},
                            'failed_rkm_extractions': sorted(failed_rkms),
                            'coverage': 'partial; only validated basic scenes'}
    catalogue = {'schemaVersion': 1, 'models': {}}
    referenced = set()
    additional_cache = {}
    eligible = len(report['identities']) if public_identities is None else len(public_identities)
    if public_identities is not None:
        for identity in sorted(set(public_identities) - report['identities'].keys()):
            report['generation']['excluded'][identity] = {
                'code': 'missing_native_identity',
                'reason': 'Public GameData identity is absent from the current native tables'}
    dist.mkdir(parents=True, exist_ok=True)
    # A sibling stage stays on the same filesystem but outside the upload tree.
    with tempfile.TemporaryDirectory(prefix='.models-3d-', dir=dist.parent) as temporary:
        stage = Path(temporary)
        (stage/'models/3d').mkdir(parents=True)
        try:
            for identity, association in report['identities'].items():
                if public_identities is not None and identity not in public_identities:
                    continue
                category = association['category']
                source_complete = False
                try:
                    if not association['references_resolved']:
                        raise SelectionError('unresolved_association', association.get('error', 'RK/ANIM/CSV validation failed'))
                    if _render_override(campaign, category, identity):
                        raise SelectionError('campaign_visual_override_unverified', 'Visual override merge rules require review')
                    owner_id = association['owner'] if category == 'PonySet' else identity
                    selection_category = 'Pony' if category == 'PonySet' else category
                    if category == 'PonySet' and _render_override(campaign, selection_category, owner_id):
                        raise SelectionError('campaign_visual_override_unverified', 'Owner visual override requires review')
                    obj = native_objects[selection_category][owner_id]
                    if failed_paths:
                        models = [association['model']]
                        child = report['identities'][owner_id].get('additional_actor', {})
                        if child.get('model'):
                            models.append(child['model'])
                        for name in models:
                            _check_failed_materials(folder, report['models'][name], failed_paths)
                    prepared_additional = prepare_additional_actor(obj, report['identities'][owner_id],
                        report, folder, additional_cache)
                    model_name = association['model']
                    selection = select_native_view(selection_category, obj, native_objects,
                        model_stem=association['declaration']['model'] if 'declaration' in association else Path(model_name).stem,
                        mesh_names=list(dict.fromkeys(report['models'][model_name]['meshes'])),
                        clips=report['csv_libraries'][association['csv']['file']]['clips'] if 'csv' in association else [],
                        extracted_dir=folder, outfit_id=identity if category == 'PonySet' else None,
                        csv_first_name=report['csv_libraries'][association['csv']['file']].get('first_declared_name'),
                        csv_excluded_names=report['csv_libraries'][association['csv']['file']].get('excluded_clip_names'),
                        display_profile=display_profile,
                        **({'allow_additional_actor': True} if prepared_additional is not None else {}))
                    wardrobe = selection['provenance'].get('wardrobe') or {}
                    for dependency_category, dependency_ids in [('PonySet', wardrobe.get('set_ids', [])),
                                                                 ('PonyPart', wardrobe.get('part_ids', []))]:
                        if any(_render_override(campaign, dependency_category, dep) for dep in dependency_ids):
                            raise SelectionError('campaign_visual_override_unverified', 'Wardrobe dependency override requires review')
                    base_selection = selection
                    interaction_effects = {}
                    effect_provenance = {}
                    def resolve_emitter(attributes: dict[str, str], frame: int) -> dict:
                        if set(attributes) != {'Particle'}:
                            raise ValueError('Unsupported native emitter attributes')
                        name = attributes['Particle']
                        if name not in interaction_effects:
                            profile, png, provenance = load_native_emitter(folder, name)
                            interaction_effects[name] = {'profile': profile, 'png_bytes': png}
                            effect_provenance[name] = provenance
                        return {'frame': frame, 'type': 'Emitter', 'effect': name}
                    try:
                        selection = prepare_interactions(selection, selection_category, obj,
                            model_stem=Path(model_name).stem,
                            mesh_names=report['models'][model_name]['meshes'],
                            clips=report['csv_libraries'][association['csv']['file']]['clips'],
                            effect_resolver=resolve_emitter)
                    except (InteractionError, UnicodeError, ET.ParseError) as error:
                        # The already verified static scene and event-free actions
                        # remain valid; never approve a failed optional controller.
                        selection['provenance']['interactions'] = {
                            'diagnostics': [{'code': 'interaction-profile-unavailable', 'reason': str(error)}]}
                    used_effects = {event['effect'] for rows in selection.get('animation_events', {}).values()
                                    for event in rows if event['type'] == 'Emitter'}
                    interaction_effects = {name: interaction_effects[name] for name in used_effects}
                    if used_effects:
                        selection['provenance']['interactions']['effects'] = {
                            name: effect_provenance[name] for name in used_effects}
                    def convert_selected(selection: dict, effects: dict[str, dict]) -> AssetBundle:
                        return convert_native(folder/model_name, folder/association['animation'],
                        folder/association['csv']['file'], selection['clip_names'], identity=identity,
                        model_filename=identity, animation_filenames=True,
                        visible_meshes=selection['visible_meshes'], rest_name=selection['rest_name'],
                        rest_frame=selection.get('rest_frame'), event_path=selection['event_path'],
                        events_absent=selection.get('events_absent', False),
                        **({'additional_event_paths': selection['additional_event_paths']}
                           if selection.get('additional_event_paths') else {}),
                        **({'user_vector': obj['UserVector']} if obj.get('UserVector') else {}),
                        **({key: selection[key] for key in ('retained_meshes', 'interactions',
                            'model_interactions', 'animation_events', 'event_approvals')} if 'interactions' in selection else {}),
                        **({'interaction_effects': effects} if effects else {}))
                    try:
                        bundle = convert_selected(selection, interaction_effects)
                    except (NativeInputError, ConversionError) as error:
                        if 'interactions' not in selection:
                            raise
                        selection = base_selection
                        selection['provenance']['interactions'] = {'diagnostics': [
                            {'code': 'interaction-assets-unavailable', 'reason': str(error)}]}
                        bundle = convert_selected(selection, {})
                    # Only source preparation/conversion errors are isolated.
                    # Output integrity checks and writes below remain fatal.
                    source_complete = True
                    model_row = bundle.catalogue['models'][identity]
                    bundle_files = dict(bundle.files)
                    additional_provenance = None
                    if prepared_additional is not None:
                        child, additional_provenance = attach_additional_actor(obj, bundle, identity, prepared_additional)
                        model_row = {**model_row, 'additionalActor': child}
                        for key, data in prepared_additional[0].files.items():
                            if key in bundle_files and bundle_files[key] != data:
                                raise ValueError('Conflicting parent/child asset content')
                            bundle_files[key] = data
                    # Complete and validate both actors before staging either.
                    for key, data in bundle_files.items():
                        digest = hashlib.sha256(data).hexdigest()
                        if key == model_path(identity):
                            valid = (key == model_row['path'] and digest == model_row['sha256']
                                     and len(data) == model_row['bytes'])
                        else:
                            valid = asset_digest(key) == digest
                        if not valid:
                            raise ValueError('Converter returned an invalid asset key or digest')
                        path = stage/key
                        if path.exists() and path.read_bytes() != data:
                            raise ValueError('Conflicting generated asset content')
                    for key, data in bundle_files.items():
                        path = stage/key
                        if not path.exists():
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(data)
                        referenced.add(key)
                    catalogue['models'][identity] = model_row
                    catalogue['schemaVersion'] = max(catalogue['schemaVersion'], bundle.catalogue['schemaVersion'])
                    if additional_provenance is not None:
                        catalogue['schemaVersion'] = max(catalogue['schemaVersion'], 2)
                        association['additional_actor'] = {**report['identities'][owner_id]['additional_actor'],
                            'renderer_supported': True, 'converted': True,
                            'conversion_provenance': additional_provenance}
                    association['converted'] = True
                    association['conversion_provenance'] = bundle.provenance
                    association['selection'] = selection['provenance']
                    association['excluded_clips'] = selection['excluded_clips']
                    csv_diagnostics = report['csv_libraries'][association['csv']['file']].get('diagnostics', [])
                    for excluded in association['excluded_clips']:
                        invalid = [row for row in csv_diagnostics if row.get('name') == excluded['name']]
                        if invalid:
                            excluded['reason'] = 'invalid_native_csv_clip'
                            excluded['csv_diagnostics'] = invalid
                    association['blockers'] = []
                    report['generation']['converted'] += 1
                except (SelectionError, NativeInputError, ConversionError) as error:
                    report['generation']['excluded'][identity] = {
                        'code': getattr(error, 'code', type(error).__name__), 'reason': str(error)}
                except (ValueError, OSError) as error:
                    if source_complete:
                        raise
                    report['generation']['excluded'][identity] = {
                        'code': 'native_source_error', 'reason': f'{type(error).__name__}: {error}'}
            if not catalogue['models'] and eligible:
                raise ValueError('No verified basic 3D scenes; previous catalogue was not replaced')
            # Report unreferenced files without deleting them. Only current
            # references are uploaded; fixed model keys represent this version.
            report['generation']['unreferenced_local_assets'] = sorted(
                p.relative_to(dist).as_posix() for p in (dist/'models/3d').rglob('*.glb')
                if is_asset_path(p.relative_to(dist).as_posix()) and p.relative_to(dist).as_posix() not in referenced)
            report['generation']['asset_count'] = len(referenced)
            report['generation']['display_profiles'] = {}
            for row in report['identities'].values():
                if row.get('converted'):
                    profile = row['selection'].get('display', {}).get('selected', 'world-preview')
                    counts = report['generation']['display_profiles']
                    counts[profile] = counts.get(profile, 0) + 1
            report['generation']['status'] = 'validated-local-output'
            report['summary']['converted'] = report['generation']['converted']
            report['summary']['conversion_exclusions'] = len(report['generation']['excluded'])
            # Diagnostics must be writable before switching the catalogue.
            write_association_report(native_objects, folder, report_path, public_dir=dist, report=report)
            _publish_local(stage, dist, catalogue)
        except BaseException:
            report['generation']['status'] = 'failed; catalogue not intentionally replaced'
            write_association_report(native_objects, folder, report_path, public_dir=dist, report=report)
            raise
    return report
