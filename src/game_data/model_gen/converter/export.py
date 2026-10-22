"""Convert resolved native inputs to an in-memory model and animation bundle."""
from collections.abc import Collection
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from . import ConversionError, from_native, model_glb, animation_glb
from .native import NativeInputError, load_native, load_model
from .animation_cache import AnimationCache
from .texture_cache import TextureCache
from ...model_paths import asset_path, model_path, safe_asset_name

if TYPE_CHECKING:
    from luna_kit.model import RKModel


@dataclass(frozen=True)
class AssetBundle:
    catalogue: dict
    files: dict[str, bytes]
    provenance: dict | None = None


def _preview_native(native: dict, retained: Collection[str], *, indexed: bool) -> dict:
    """Keep the original preview's material order and compact vertex payload."""
    primitives, materials, remap = [], [], {}
    for primitive in native['primitives']:
        if primitive['name'] not in retained:
            continue
        material = primitive['material']
        if material not in remap:
            remap[material] = len(materials)
            materials.append(native['materials'][material])
        row = {**primitive, 'material': remap[material]}
        if not indexed:
            row.pop('native_mesh_index', None)
            row.pop('default_visible', None)
        primitives.append(row)
    return {**native, 'primitives': primitives, 'materials': materials}


class VisibilityCurve(TypedDict):
    meshName: str
    times: list[float]
    values: list[bool]


def visibility_curves(primitives: list[dict], eyes: dict[str, list[int]], events: list[dict], fps: float) -> list[VisibilityCurve]:
    """Resolve native writes once, in source order, into named second-based tracks."""
    eye_meshes = {index for group in eyes.values() for index in group}
    controlled = {index for event in events if event['type'] == 'SubObject' for index in event['meshes']}
    if any(event['type'] == 'EyeSet' for event in events):
        controlled.update(eye_meshes)
    curves: list[VisibilityCurve] = []
    for primitive in primitives:
        index = primitive.get('native_mesh_index')
        if index not in controlled:
            continue
        curve: VisibilityCurve = {'meshName': primitive['name'], 'times': [0], 'values': [primitive['default_visible']]}
        for event in events:
            if event['type'] == 'SubObject' and index in event['meshes']:
                visible = event['visible']
            elif event['type'] == 'EyeSet' and index in eye_meshes:
                visible = event['state'] != 'None' and index in eyes[event['state']]
            else:
                continue
            time = event['frame'] / fps
            if curve['times'][-1] == time:
                curve['values'][-1] = visible
            else:
                curve['times'].append(time)
                curve['values'].append(visible)
        curves.append(curve)
    return curves


def convert_native(rk_path: str | Path, anim_path: str | Path, csv_path: str | Path,
                   selected_names: Collection[str], *, identity: str,
                   visible_meshes: Collection[str], rest_name: str | None = None,
                   rest_frame: int | None = None, event_path: str | Path | None = None,
                   events_absent: bool = False, user_vector: dict[str, float] | None = None,
                   frame_boundary: str = 'reject', additional_event_paths: Collection[str | Path] = (),
                   model_filename: str | None = None, animation_filenames: bool = False,
                   retained_meshes: Collection[str] | None = None, interactions: dict | None = None,
                   model_interactions: dict | None = None, animation_events: dict[str, list[dict]] | None = None,
                   event_approvals: dict[Path, list[str]] | None = None,
                   interaction_effects: dict[str, dict] | None = None,
                   animation_cache: AnimationCache | None = None,
                   texture_cache: TextureCache | None = None,
                   rk_model: RKModel | None = None, wardrobe: dict | None = None) -> AssetBundle:
    """Convert one explicit source association; all texture data stays in GLB.

    Mesh, rest and event-source arguments have the same meaning as in
    ``load_native``. No game identity lookup or skeleton retargeting occurs.
    The caller must obtain the model/library association from native metadata.
    Filename options only change asset keys, never GLB bytes. Defaults retain
    legacy flat keys; fixed model keys require the exact object identity and
    named animation keys use the unchanged native clip name.
    """
    if not isinstance(identity, str) or not identity or any(ord(c) < 32 for c in identity):
        raise ValueError("A nonempty model identity is required")
    if model_filename is not None and (model_filename != identity or not safe_asset_name(model_filename)):
        raise NativeInputError("Model filename must be the exact safe object identity")
    if type(animation_filenames) is not bool:
        raise ValueError("animation_filenames must be a boolean")
    options = dict(visible_meshes=visible_meshes, rest_name=rest_name, rest_frame=rest_frame,
                   event_path=event_path, events_absent=events_absent, user_vector=user_vector,
                   frame_boundary=frame_boundary, additional_event_paths=additional_event_paths,
                   retained_meshes=retained_meshes, event_approvals=event_approvals,
                   animation_cache=animation_cache, texture_cache=texture_cache, rk_model=rk_model)
    wardrobe_native, wardrobe_error = None, None
    if wardrobe is not None:
        # Decode RK/ANIM/textures once for the union; the ordinary preview is
        # compacted back to exactly its old geometry and material ordering.
        options['rk_model'] = rk_model if rk_model is not None else load_model(rk_path)
        indices = {i for part in wardrobe['parts'].values() for i in part['meshes']}
        meshes = options['rk_model'].meshes
        if any(type(i) is not int or not 0 <= i < len(meshes) for i in indices):
            raise NativeInputError('Invalid owner wardrobe mesh index')
        union = set(retained_meshes or visible_meshes) | {meshes[i].name for i in indices}
        try:
            controlled = set((model_interactions or {}).get('subObjects', []))
            for values in (model_interactions or {}).get('eyes', {}).values():
                controlled.update(values)
            if indices & controlled:
                raise NativeInputError('Wardrobe meshes overlap native eye/prop controls')
            wardrobe_native = load_native(rk_path, anim_path, csv_path, selected_names,
                                          **{**options, 'retained_meshes': union})
        except (ValueError, OSError) as error:
            # Optional clothing cannot make an existing basic preview disappear.
            wardrobe_error = str(error)
    if wardrobe_native is None:
        native = load_native(rk_path, anim_path, csv_path, selected_names, **options)
    else:
        native = _preview_native(wardrobe_native, set(retained_meshes or visible_meshes),
                                 indexed=retained_meshes is not None)
    model, clips = from_native(native, identity=identity, include_clips=animation_cache is None)
    if interactions is not None:
        if model_interactions is None or retained_meshes is None:
            raise NativeInputError('Interactive models require an explicit mesh profile')
        ids = {row['name'] for row in native['clips']}
        if (type(interactions.get('version')) is not int or interactions['version'] != 1
                or set(interactions) not in ({'version', 'clickAnimations'}, {'version', 'clickAnimations', 'dizzyAnimation'})
                or not isinstance(interactions.get('clickAnimations'), list)
                or any(name not in ids for name in interactions['clickAnimations'])
                or len(interactions['clickAnimations']) != len(set(interactions['clickAnimations']))
                or ('dizzyAnimation' in interactions and interactions['dizzyAnimation'] not in ids)
                or set(animation_events or {}) - ids):
            raise NativeInputError('Invalid source-bound interaction animation identity')
        mesh_indices = {p['native_mesh_index'] for p in model.primitives}
        if (set(model_interactions) - {'version', 'eyes', 'subObjects'} or type(model_interactions.get('version')) is not int
                or model_interactions['version'] != 1):
            raise NativeInputError('Invalid model interaction profile')
        eyes = model_interactions.get('eyes')
        if eyes is not None and (not isinstance(eyes, dict) or set(eyes) != {'Open', 'Closed', 'Happy', 'Frown'}
                or any(not isinstance(values, list) or any(type(i) is not int or i not in mesh_indices for i in values)
                       or len(values) != len(set(values)) for values in eyes.values())):
            raise NativeInputError('Native eye index is absent from retained geometry')
        subobjects = model_interactions.get('subObjects', [])
        if (not isinstance(subobjects, list) or any(type(i) is not int or i not in mesh_indices for i in subobjects)
                or len(subobjects) != len(set(subobjects))):
            raise NativeInputError('Native SubObject index is absent from retained geometry')
        model.interactions = model_interactions
        model.interaction_effects = interaction_effects
        for source_clip in native['clips']:
            events = (animation_events or {}).get(source_clip['name'], [])
            if events != source_clip.get('events', []):
                raise NativeInputError('Compiled events differ from the checked native XML: ' + source_clip['name'])
            for event in events:
                if not 0 <= event['frame'] < len(source_clip['translations']):
                    raise NativeInputError('Native event frame is outside the selected clip')
                if event['type'] == 'SubObject' and (not event['meshes'] or
                        any(index not in subobjects for index in event['meshes']) or type(event['visible']) is not bool):
                    raise NativeInputError('Native SubObject event target is absent')
                if event['type'] == 'Emitter' and event['effect'] not in (interaction_effects or {}):
                    raise NativeInputError('Native emitter dependency is absent')
                if event['type'] == 'EyeSet' and (eyes is None or
                        any(eyes.values()) and event['state'] != 'None' and not eyes[event['state']]):
                    raise NativeInputError('Native event eye state is absent')
    elif animation_events or model_interactions or interaction_effects or event_approvals:
        raise NativeInputError('Native events require the explicit interaction profile')
    files = {}

    def asset(data: bytes, name: str | None = None, *, fixed_model: bool = False) -> dict:
        if name is not None and not safe_asset_name(name):
            raise NativeInputError("Unsupported native animation filename: " + str(name))
        digest = hashlib.sha256(data).hexdigest()
        path = model_path(name) if fixed_model else asset_path(digest, name)
        files[path] = data
        return {"path": path, "sha256": digest, "bytes": len(data), "rigKey": model.rig.key}

    row = asset(model_glb(model), model_filename, fixed_model=model_filename is not None)
    if wardrobe_native is not None:
        try:
            wardrobe_model, _ = from_native(wardrobe_native, identity=identity, include_clips=False)
            retained_indices = {p['native_mesh_index'] for p in wardrobe_model.primitives}
            wardrobe_model.wardrobe = {**wardrobe, 'parts': {
                pid: {**part, 'meshes': [i for i in part['meshes'] if i in retained_indices]}
                for pid, part in wardrobe['parts'].items()}}
            wardrobe_model.interactions = model.interactions or {'version': 1}
            wardrobe_model.interaction_effects = model.interaction_effects
            row['wardrobe'] = asset(model_glb(wardrobe_model))
        except (ValueError, OSError) as error:
            wardrobe_error = str(error)
    if wardrobe_error is not None:
        native['provenance']['wardrobe_unavailable'] = wardrobe_error
    if animation_cache is None:
        row["animations"] = [{**asset(animation_glb(clip), clip.name if animation_filenames else None), "id": clip.name, "name": clip.name,
                              "duration": len(clip.global_frames) / clip.fps} for clip in clips]
    else:
        row['animations'] = []
        for source_clip in native['clips']:
            try:
                if animation_filenames and not safe_asset_name(source_clip['name']):
                    raise NativeInputError('Unsupported native animation filename')
                ref, data = animation_cache.compile(native, source_clip, model.rig, named=animation_filenames)
            except (ConversionError, ValueError) as error:
                native['provenance'].setdefault('animation_exclusions', []).append(
                    {'name': source_clip['name'], 'reason': str(error)})
                continue
            files[ref['path']] = data
            row['animations'].append(ref)
    source_clips = {clip['name']: clip for clip in native['clips']}
    for ref in row['animations']:
        events = (animation_events or {}).get(ref['id'], [])
        if events:
            ref['eventBinding'] = {
                'version': 2,
                'events': [event for event in events if event['type'] != 'SubObject'],
                'visibility': visibility_curves(model.primitives, (model.interactions or {}).get('eyes', {}),
                                                events, source_clips[ref['id']]['fps']),
            }
    if interactions is not None:
        available = {clip['id'] for clip in row['animations']}
        row['interactions'] = {**interactions,
            'clickAnimations': [name for name in interactions['clickAnimations'] if name in available]}
        if row['interactions'].get('dizzyAnimation') not in available:
            row['interactions'].pop('dizzyAnimation', None)
    version = (4 if "wardrobe" in row or any(clip.get("eventBinding") for clip in row["animations"]) or (model_interactions or {}).get("subObjects")
               else 3 if interactions is not None else 2 if native["provenance"].get("material_uv_clock") else 1)
    return AssetBundle({"schemaVersion": version, "models": {identity: row}}, files, native["provenance"])
