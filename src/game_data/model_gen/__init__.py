"""Generate the optional 3D catalogue inside the existing game-data build."""
import hashlib
import json
from pathlib import Path
import tempfile

from ..model_associations import discover_associations, write_association_report
from ..model_paths import asset_digest, is_asset_path, model_path
from .converter import ConversionError
from .converter.export import convert_native
from .converter.native import NativeInputError
from .selection import SelectionError, select_native_view
from .additional import prepare_additional_actor, attach_additional_actor


def _render_override(campaign, category, identity):
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
    def visit(value):
        if isinstance(value, dict):
            return any(k in fields or k.endswith('AnimationList') or visit(v) for k,v in value.items())
        if isinstance(value, list):
            return any(visit(v) for v in value)
        return False
    return visit(override)


def _publish_local(stage, dist, catalogue):
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


def generate_models(native_objects, extracted_dir, dist_dir, report_path, *, campaign,
                    public_identities=None, display_profile='ponybook-preferred'):
    """Discover and convert supported objects; all other objects get diagnostics.

    Normal callers pass Transformer inputs and public GameData IDs. There is no
    manual RK/ANIM list, downloader, archive reader, upload or event executor.
    """
    if not isinstance(campaign, dict):
        raise ValueError('The current parsed defaultGameCampaign is required')
    folder, dist, report_path = Path(extracted_dir), Path(dist_dir), Path(report_path)
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
                    prepared_additional = prepare_additional_actor(obj, report['identities'][owner_id],
                        report, folder, additional_cache)
                    model_name = association['model']
                    selection = select_native_view(selection_category, obj, native_objects,
                        model_stem=association['declaration']['model'] if 'declaration' in association else Path(model_name).stem,
                        mesh_names=report['models'][model_name]['meshes'],
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
                    bundle = convert_native(folder/model_name, folder/association['animation'],
                        folder/association['csv']['file'], selection['clip_names'], identity=identity,
                        model_filename=identity, animation_filenames=True,
                        visible_meshes=selection['visible_meshes'], rest_name=selection['rest_name'],
                        rest_frame=selection.get('rest_frame'), event_path=selection['event_path'],
                        events_absent=selection.get('events_absent', False),
                        **({'additional_event_paths': selection['additional_event_paths']}
                           if selection.get('additional_event_paths') else {}),
                        **({'user_vector': obj['UserVector']} if obj.get('UserVector') else {}))
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
                    if (bundle.provenance or {}).get('material_uv_clock'):
                        catalogue['schemaVersion'] = 2
                    if additional_provenance is not None:
                        catalogue['schemaVersion'] = 2
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
