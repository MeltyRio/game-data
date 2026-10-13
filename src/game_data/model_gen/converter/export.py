"""Convert resolved native inputs to an in-memory model and animation bundle."""
from dataclasses import dataclass
import hashlib

from . import from_native, model_glb, animation_glb
from .native import NativeInputError, load_native
from ...model_paths import asset_path, model_path, safe_asset_name


@dataclass(frozen=True)
class AssetBundle:
    catalogue: dict
    files: dict[str, bytes]
    provenance: dict | None = None


def convert_native(rk_path, anim_path, csv_path, selected_names, *, identity,
                   visible_meshes, rest_name=None, rest_frame=None,
                   event_path=None, events_absent=False, user_vector=None, frame_boundary='reject', additional_event_paths=(),
                   model_filename=None, animation_filenames=False) -> AssetBundle:
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
    native = load_native(rk_path, anim_path, csv_path, selected_names, visible_meshes=visible_meshes,
                         rest_name=rest_name, rest_frame=rest_frame,
                         event_path=event_path, events_absent=events_absent, user_vector=user_vector,
                         frame_boundary=frame_boundary, additional_event_paths=additional_event_paths)
    model, clips = from_native(native, identity=identity)
    files = {}

    def asset(data, name=None, *, fixed_model=False):
        if name is not None and not safe_asset_name(name):
            raise NativeInputError("Unsupported native animation filename: " + str(name))
        digest = hashlib.sha256(data).hexdigest()
        path = model_path(name) if fixed_model else asset_path(digest, name)
        files[path] = data
        return {"path": path, "sha256": digest, "bytes": len(data), "rigKey": model.rig.key}

    row = asset(model_glb(model), model_filename, fixed_model=model_filename is not None)
    row["animations"] = [{**asset(animation_glb(clip), clip.name if animation_filenames else None), "id": clip.name, "name": clip.name,
                          "duration": len(clip.global_frames) / clip.fps} for clip in clips]
    version = 2 if native["provenance"].get("material_uv_clock") else 1
    return AssetBundle({"schemaVersion": version, "models": {identity: row}}, files, native["provenance"])
