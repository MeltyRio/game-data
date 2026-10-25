"""Native adapter using the existing game-data Luna Kit dependency.

No archive extraction or PonyVista imports live here. The caller supplies a
small, already extracted dependency closure and explicitly selected submesh names.
A selected name keeps every matching native submesh (clothing parts may repeat).
Matrices and transforms remain in the game's coordinate system for core.py.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import struct
from pathlib import Path
from typing import Collection, TYPE_CHECKING

if TYPE_CHECKING:
    from luna_kit.model import RKModel
    from .texture_cache import TextureCache
    from .animation_cache import AnimationCache

import numpy as np
from numpy.typing import NDArray

from .native_animation import read_animation, NATIVE_POSITION_SCALE

LUNA_DEPENDENCY = "game-data uv.lock: luna-kit"
MAX_NATIVE_JOINT_FRAME_SAMPLES = 1_000_000


class NativeInputError(ValueError):
    """The supplied native dependency closure is unsupported or inconsistent."""


def _sibling(folder: Path, name: str) -> Path:
    candidate = (folder / name).resolve()
    if not candidate.is_relative_to(folder.resolve()):
        raise NativeInputError(f"Native dependency leaves its directory: {name}")
    if not candidate.is_file():
        raise NativeInputError(f"Missing native dependency: {candidate.name}")
    return candidate


def material_dependency_names(name: str) -> tuple[str, ...]:
    """Native material stems may retain a source-art extension.

    Preserve exact material lookup first. Only the two source-image suffixes
    confirmed by this catalogue may be removed; no numbered/name substitution.
    """
    exact = name + ".rkm"
    stem = Path(name)
    if stem.suffix.lower() in {".psd", ".tga"}:
        return exact, str(stem.with_suffix(".rkm"))
    return (exact,)


def _material_sibling(folder: Path, name: str, *, allow_missing: bool = False) -> Path | None:
    for candidate in material_dependency_names(name):
        requested = folder / candidate
        path = requested.resolve()
        if not path.is_relative_to(folder.resolve()):
            raise NativeInputError(f"Native dependency leaves its directory: {name}")
        if path.is_file():
            return path
        if path.exists() or requested.is_symlink():
            raise NativeInputError(f"Native dependency is not a file: {candidate}")
    if allow_missing:
        return None
    raise NativeInputError(f"Missing native dependency: {name}.rkm")


def _validate_model_layout(model: RKModel) -> None:
    """Check converter requirements; Luna validates the binary layout."""
    # Luna also reads unskinned/empty models; this exporter needs geometry,
    # materials, named submeshes, skinning and one supported UV attribute.
    for tag in (1, 2, 3, 4, 7, 13, 16, 17):
        section = model.section_headers.get(tag)
        if section is None or not section.count:
            raise NativeInputError(f"Unsupported RK section layout: {tag}")
    if model.section_headers[4].count % 3:
        raise NativeInputError("Unsupported RK face index layout")
    if sum(attribute[0] in (1026, 1030) for attribute in model.attributes) != 1:
        raise NativeInputError("Unsupported RK position/UV layout")


def read_model_mesh_names(path: str | Path) -> list[str]:
    """Read discovery metadata; selected scenes still require full load_model validation."""
    from luna_kit.model import RKModel

    try:
        model = RKModel()
        with Path(path).open('rb') as stream:
            model.header = model._read_header(stream)
            model.section_headers = model._read_sections_header(stream)
            return [mesh.name for mesh in model._read_submesh_info(stream)]
    except (ValueError, AssertionError, IndexError, struct.error) as error:
        raise NativeInputError(f'RK metadata parse failed: {Path(path).name}: {error}') from error


def load_model(path: str | Path) -> RKModel:
    """Read one RK for both native appearance selection and conversion."""
    from luna_kit.model import RKModel

    try:
        model = RKModel(str(path))
    except (ValueError, AssertionError, IndexError, struct.error, ZeroDivisionError) as error:
        raise NativeInputError(f'RK parse failed: {Path(path).name}: {error}') from error
    _validate_model_layout(model)
    if [bone.index for bone in model.bones] != list(range(len(model.bones))):
        raise NativeInputError('Non-contiguous native bone indices')
    if any(bone.parentIndex >= bone.index or bone.parentIndex < -1 for bone in model.bones):
        raise NativeInputError('Unsupported or cyclic bone hierarchy')
    return model


def _rkm_fields(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [row for row in csv.reader(handle, delimiter="=") if row]
    if any(len(row) != 2 for row in rows) or len({row[0] for row in rows}) != len(rows):
        raise NativeInputError(f"Invalid or duplicate material properties: {path.name}")
    return dict(rows)


def _vector_x(fields: dict[str, str], *, cpu: bool) -> float:
    # The reviewed assets encode only the first scalar. Native prefix lookup
    # plus sscanf reads that x; other encodings need separate format evidence.
    keys = {key for key in fields if key.startswith("UserVector")}
    if keys - {"UserVector0_0"}:
        raise NativeInputError("Unsupported native UserVector encoding")
    try:
        value = float(fields["UserVector0_0"]) if keys else (1.0 if cpu else 0.0)
    except (TypeError, ValueError) as error:
        raise NativeInputError("Invalid native UserVector scalar") from error
    if not math.isfinite(value):
        raise NativeInputError("Invalid native UserVector scalar")
    return value


def _hair_material(folder: Path, fields: dict[str, str], controller: dict[str, float] | None,
                   first_material: str) -> tuple[dict, list[Path], str]:
    if not isinstance(controller, dict) or set(controller) != {"UserVectorSpeed", "RangeMin", "RangeMax"}:
        raise NativeInputError("Missing or unsupported native UserVector declaration")
    values = [controller[key] for key in ("UserVectorSpeed", "RangeMin", "RangeMax")]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        raise NativeInputError("Invalid native UserVector declaration")
    speed, lower, upper = map(float, values)
    if speed < 0 or lower > upper:
        raise NativeInputError("Invalid native UserVector range or speed")
    first_path = _material_sibling(folder, first_material)
    first_fields = _rkm_fields(first_path)
    # The native updater writes the clock into material zero as well. Ordinary
    # rkshader does not consume it; a horn/unknown first shader is not equivalent.
    if (first_fields.get("Shader") or "rkshader") not in {"rkshader", "rkponyhair0"}:
        raise NativeInputError("Unsupported first material for native UserVector clock")
    result = {"initial_gpu_x": _vector_x(fields, cpu=False)}
    if speed > 0:
        result["clock"] = {"version": 1, "initial": _vector_x(first_fields, cpu=True),
                           "speed": speed, "min": lower, "max": upper}
    return result, [first_path], first_path.name


def _remove_collapsed_zero_weight_triangles(
    primitives: list[dict], materials: list[dict], weights: np.ndarray,
) -> tuple[list[dict], list[dict], dict | None]:
    """Omit only triangles proven to collapse to one point in native rkshader.

    Its four explicit weights accumulate from zero; the final homogeneous lane
    is one. Three all-zero vertices therefore occupy the same model origin for
    every finite palette/WVP. Mixed triangles do not have this property.
    """
    import numpy as np
    zero = np.all(weights == 0, axis=1)
    if not any(np.any(zero[p["indices"]]) for p in primitives):
        return primitives, materials, None
    removed, retained = [], []
    for primitive_index, primitive in enumerate(primitives):
        flags = zero[primitive["indices"]]
        collapsed = flags.all(axis=1)
        if np.any(flags.any(axis=1) & ~collapsed):
            raise NativeInputError("Zero-weight vertices in mixed triangles require unsupported native skinning semantics")
        if collapsed.any():
            if materials[primitive["material"]]["resolved_shader"] != "rkshader":
                raise NativeInputError("Zero-weight triangles require the reviewed ordinary shader")
            removed.append({"selected_primitive_index": primitive_index,
                            "mesh": primitive["name"],
                            "triangle_ordinals": np.flatnonzero(collapsed).tolist(),
                            "indices": primitive["indices"][collapsed].tolist(),
                            "primitive_removed": bool(collapsed.all())})
        if not collapsed.all():
            retained.append({**primitive, "indices": primitive["indices"][~collapsed]})
    if not retained:
        raise NativeInputError("Native zero-weight triangles leave no renderable model geometry")
    used_materials = sorted({p["material"] for p in retained})
    remap = {old: new for new, old in enumerate(used_materials)}
    retained = [{**p, "material": remap[p["material"]]} for p in retained]
    evidence = {"rule": "reviewed-rkshader-gpu-all-zero-triangle-collapse-v1",
                "removed_triangle_count": sum(len(row["indices"]) for row in removed),
                "removed": removed}
    return retained, [materials[i] for i in used_materials], evidence


def _csv_clips(path: Path, frame_count: int, required: set[str], frame_boundary: str = 'reject') -> tuple[list[dict], int, list[dict]]:
    from .csv import CsvError, read_clips
    try:
        return read_clips(path, frame_count, required, frame_boundary=frame_boundary)
    except CsvError as error:
        raise NativeInputError(str(error)) from error


def _verify_event_source(event_path: str | Path | None, events_absent: bool, names: set[str],
                         event_approvals: dict[Path, list[str]] | None = None, *,
                         model_stem: str = "", mesh_names: list[str] | None = None) -> tuple[dict, Path | None]:
    """Validate an explicit event source; absence must be asserted by discovery."""
    if bool(event_path) == bool(events_absent):
        raise NativeInputError("Supply an event XML or explicitly confirm its native source absence")
    if events_absent:
        return {"status": "source-absence-confirmed-by-caller", "checked_clips": sorted(names)}, None
    import re
    from xml.etree import ElementTree as ET
    path = Path(event_path).resolve()
    if not path.is_file():
        raise NativeInputError(f"Missing native event XML: {path.name}")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
            raise NativeInputError("DTD/entity declarations are unsupported in native event XML")
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
        # Game files can contain both MeshList and AnimationList at top level.
        root = ET.fromstring("<NativeEventSource>" + text + "</NativeEventSource>")
    except (UnicodeError, ET.ParseError) as error:
        raise NativeInputError(f"Invalid native event XML: {path.name}") from error
    if any(node.tag not in {"MeshList", "AnimationList"} for node in root):
        raise NativeInputError("Unsupported native event document structure")
    for animation_list in root.iter("AnimationList"):
        if any(node.tag != "Animation" for node in animation_list):
            raise NativeInputError("Unsupported native animation declaration")
    from ..interactions import parse_events
    from ..visibility import compile_subobject_event
    controls = parse_events(root)['controls'] if event_approvals is not None else {}
    approved = (event_approvals or {}).get(path, [])
    for node in root.iter("Animation"):
        if node.get("Name") in names and set(node.attrib) != {"Name"}:
            raise NativeInputError("Unsupported selected native animation attributes")
        if node.get("Name") in names and len(node):
            if node.get('Name') not in approved:
                raise NativeInputError(f"Selected animation requires unsupported native events: {node.get('Name')}")
            declaration_events = []
            for frame in node:
                if frame.tag != 'Frame' or set(frame.attrib) != {'Index'} or not re.fullmatch(r'\d+', frame.get('Index', '')):
                    raise NativeInputError('Unsupported approved native event frame')
                for event in frame:
                    attrs = event.attrib
                    valid = (event.tag == 'EyeSet' and set(attrs) == {'Open', 'EnableBlink'}
                             and attrs['Open'] in {'Open', 'Closed', 'Happy', 'Frown', 'None'}
                             and attrs['EnableBlink'] in {'0', '1'}) or (
                             event.tag == 'Emitter' and set(attrs) == {'Particle'}
                             and attrs['Particle'] == 'mlp_arena_pony_defeat_02') or (
                             event.tag == 'SubObject' and set(attrs) == {'ID', 'Show'}
                             and attrs['Show'] in {'0', '1'})
                    if not valid or len(event) or (event.text or '').strip():
                        raise NativeInputError('Unsupported approved native event: ' + event.tag)
                    key = (int(frame.get('Index')), event.tag, dict(attrs))
                    if key in declaration_events:
                        raise NativeInputError('Duplicate native event in one animation declaration')
                    declaration_events.append(key)
    evidence = {"status": "parsed-source-xml", "name": path.name,
                "checked_clips": sorted(names)}
    if event_approvals is not None:
        mapped = {}
        for node in root.iter('Animation'):
            if node.get('Name') not in names:
                continue
            rows = mapped.setdefault(node.get('Name'), [])
            for frame in node:
                for event in frame:
                    row = {'frame': int(frame.get('Index')), 'type': event.tag}
                    if event.tag == 'EyeSet':
                        row.update(state=event.get('Open'), enableBlink=event.get('EnableBlink') == '1')
                    elif event.tag == 'SubObject':
                        row = compile_subobject_event(event.attrib, row['frame'], controls, model_stem, mesh_names or [])
                    else:
                        row['effect'] = event.get('Particle')
                    if row not in rows:
                        rows.append(row)
        evidence['approved_events'] = mapped
    return evidence, path


def load_native(
    rk_path: str | Path,
    anim_path: str | Path,
    csv_path: str | Path,
    selected_names: Collection[str],
    *,
    visible_meshes: Collection[str],
    rest_name: str | None = None,
    rest_frame: int | None = None,
    event_path: str | Path | None = None,
    events_absent: bool = False,
    user_vector: dict[str, float] | None = None,
    frame_boundary: str = 'reject',
    additional_event_paths: Collection[str | Path] = (),
    retained_meshes: Collection[str] | None = None,
    event_approvals: dict[Path, list[str]] | None = None,
    texture_cache: TextureCache | None = None,
    animation_cache: AnimationCache | None = None,
    rk_model: RKModel | None = None,
) -> dict:
    """Read native files without axis changes, retargeting, or visibility guesses.

    ``bind_global`` uses column-vector matrices. ANIM translations/quaternions
    are global model-space, not parent-local. CSV end frames are inclusive.
    Version 5.2 / frame types 0, 1 and 4 are supported with exact length checks.
    ``rest_name`` names a native CSV clip whose first frame is the rest pose.
    Alternatively, ``rest_frame`` explicitly selects a native frame when the
    source declares no idle clip. The two inputs are mutually exclusive.
    Texture bytes are PNG-encoded by Pillow from Luna's decoded native image.
    Event XML or an explicit native-source absence declaration is required.
    Selected actions with events are rejected unless the caller supplies exact
    source-path approvals for supported records. The export stage also compares
    every compiled event to these parsed records. Ordinary rkshader and the
    source-verified default-discard horn material are supported. Hair UV
    scrolling requires the original object's explicit ``user_vector`` metadata.
    ``frame_boundary='native-clamp'`` explicitly samples upper-out-of-range
    CSV intervals using the native last-frame rule, retaining declared duration.
    Its per-clip and total joint-frame export budgets are checked before expansion.
    The default remains strict rejection; the caller establishes controller scope.
    ``rk_model`` may reuse the result of ``load_model(rk_path)`` from appearance
    selection within the same conversion. It is not modified here. The caller
    must not supply a task/actor uniform override; those controllers are not
    represented by a standalone skeletal clip.
    """
    import numpy as np
    try:
        from luna_kit.model.rk import parse_rkm
        from luna_kit.pvr import PVR
        from luna_kit.utils import image_has_alpha, put_alpha
    except ImportError as error:
        raise NativeInputError("The existing game-data Luna Kit model dependencies are unavailable") from error

    rk_path, anim_path, csv_path = map(lambda value: Path(value).resolve(), (rk_path, anim_path, csv_path))
    for path in (rk_path, anim_path, csv_path):
        if not path.is_file():
            raise NativeInputError(f"Missing native input: {path.name}")
    animation_digest = (animation_cache.digest(anim_path) if animation_cache is not None
                        else hashlib.sha256(anim_path.read_bytes()).hexdigest())
    try:
        animation = animation_cache.read_animation(anim_path) if animation_cache is not None else read_animation(anim_path)
    except ValueError as error:
        raise NativeInputError(str(error)) from error
    major, minor, frame_type = animation.header.major, animation.header.minor, animation.header.frame_type
    frames = animation.header.frame_count
    model = rk_model if rk_model is not None else load_model(rk_path)
    if len(model.bones) != animation.header.bone_count:
        raise NativeInputError("Model and animation bone counts differ")
    for bone in model.bones:
        matrix = np.asarray(bone.matrix_4x4, dtype=np.float64)
        if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
            raise NativeInputError(f"Invalid bind matrix: {bone.name}")

    if (rest_name is None) == (rest_frame is None):
        raise NativeInputError("Specify exactly one native rest clip or rest frame")
    if rest_frame is not None and (isinstance(rest_frame, bool) or not isinstance(rest_frame, int) or not 0 <= rest_frame < frames):
        raise NativeInputError("Explicit native rest frame is out of range")
    selected = list(dict.fromkeys(selected_names))
    required_names = set(selected) | ({rest_name} if rest_name is not None else set())
    declarations, duplicates, csv_diagnostics = _csv_clips(csv_path, frames, required_names, frame_boundary)
    by_name = {item["name"]: item for item in declarations}
    if not selected:
        raise NativeInputError("Select at least one native animation")
    for name in required_names:
        if name not in by_name:
            raise NativeInputError(f"Unknown native animation: {name}")

    if frame_boundary == 'native-clamp':
        # Count every emitted clip plus the model's single rest sample, before
        # allocating any expanded indices/transforms. The source file is intact.
        samples = 1 + sum(by_name[name]['end'] - by_name[name]['start'] + 1 for name in selected)
        if samples * animation.header.bone_count > MAX_NATIVE_JOINT_FRAME_SAMPLES:
            raise NativeInputError(f'Native sample export budget exceeds {MAX_NATIVE_JOINT_FRAME_SAMPLES} joint-frame samples')

    event_source, checked_event_path = _verify_event_source(event_path, events_absent, required_names, event_approvals,
        model_stem=rk_path.stem, mesh_names=[mesh.name for mesh in model.meshes])
    if isinstance(additional_event_paths, (str, bytes, Path)):
        raise NativeInputError("Additional event sources must be a collection of explicit paths")
    extra_event_sources, extra_event_files = [], []
    for source in dict.fromkeys(Path(p).resolve() for p in additional_event_paths):
        if source == checked_event_path:
            continue
        evidence, checked = _verify_event_source(source, False, required_names, event_approvals,
            model_stem=rk_path.stem, mesh_names=[mesh.name for mesh in model.meshes])
        extra_event_sources.append(evidence)
        extra_event_files.append(checked)

    mesh_names = [mesh.name for mesh in model.meshes]
    visible = set(visible_meshes)
    if not visible or not visible.issubset(mesh_names):
        raise NativeInputError("Explicit visible submesh selection is missing or invalid")
    retained = visible if retained_meshes is None else set(retained_meshes)
    if not visible <= retained or not retained <= set(mesh_names):
        raise NativeInputError('Invalid explicit retained submesh selection')
    retained_indices = {i for i, name in enumerate(mesh_names) if name in retained}
    positions = np.array([[vert.pos.x, vert.pos.y, vert.pos.z] for vert in model.verts], dtype=np.float64)
    texcoords = np.array([[vert.u, vert.v] for vert in model.verts], dtype=np.float64)
    joints = np.array([[item.bone for item in vert.bones] for vert in model.verts], dtype=np.int64)
    weights = np.array([[item.weight for item in vert.bones] for vert in model.verts], dtype=np.float64)
    if joints.shape != (len(positions), 4) or weights.shape != joints.shape:
        raise NativeInputError("Only native four-influence skinning is supported")
    if not np.isfinite(positions).all() or not np.isfinite(texcoords).all() or not np.isfinite(weights).all():
        raise NativeInputError("Non-finite native vertex data")
    if (weights < 0).any() or ((joints < 0) | (joints >= len(model.bones))).any():
        raise NativeInputError("Invalid native joint or weight")

    dependencies = [rk_path, anim_path, csv_path]
    if checked_event_path is not None:
        dependencies.append(checked_event_path)
    dependencies.extend(extra_event_files)
    materials, material_ids, primitives = [], {}, []
    material_sources = []
    for native_mesh_index, mesh in enumerate(model.meshes):
        if native_mesh_index not in retained_indices:
            continue
        if not (0 <= mesh.material_index < len(model.materials)):
            raise NativeInputError(f"Invalid material on submesh {mesh.name}")
        if mesh.material_index not in material_ids:
            material = model.materials[mesh.material_index]
            rkm_path = _material_sibling(rk_path.parent, material.name, allow_missing=True)
            has_rkm = rkm_path is not None
            # 11.4.3c loader 0x2d62d0 uses the original material name as a
            # texture request and keeps constructor state when RKM is absent.
            # Luna already implements this lookup; do not invent a source RKM
            # or copy its dataclass cull/depth defaults into native fields.
            if not has_rkm:
                rkm_path = (rk_path.parent / (material.name + ".rkm")).resolve()
            raw_fields = _rkm_fields(rkm_path) if has_rkm else {}
            try:
                properties = parse_rkm(str(rkm_path))
            except (TypeError, ValueError) as error:
                raise NativeInputError(f"Unsupported material property: {rkm_path.name}") from error
            material_sources.append({"name": rkm_path.name, "status": "parsed-source-rkm"} if has_rkm else {
                "name": rkm_path.name, "status": "native-default-without-rkm",
                "rule": "reviewed-native-original-name-texture-default-material",
                "client_version": "11.4.3c",
            })
            resolved_shader = properties.Shader or "rkshader"
            hair_uv = None
            if resolved_shader == "rkponyhair0":
                hair_uv, sources, first_material = _hair_material(
                    rkm_path.parent, raw_fields, user_vector, model.materials[0].name)
                dependencies.extend(sources)
                material_sources[-1].update(shader_rule="native-user-vector-uv-offset",
                                            clock_source_material=first_material,
                                            user_vector=dict(user_vector))
            elif resolved_shader == "rkshader_horn":
                if any(name.startswith("UserVector") for name in raw_fields):
                    raise NativeInputError("Explicit native horn UserVector requires its controller")
                material_sources[-1]["shader_rule"] = "native-undeclared-user-vector-gpu-zero-discard"
            elif resolved_shader != "rkshader":
                raise NativeInputError(f"Unsupported native shader: {resolved_shader}")
            if properties.BlendMode not in {"none", "opaque", "alpha"}:
                raise NativeInputError(f"Unsupported native blending: {properties.BlendMode}")
            if properties.ClampMode not in {"RK_CLAMP", "RK_REPEAT"}:
                raise NativeInputError(f"Unsupported texture addressing: {properties.ClampMode}")
            suffix = ".png" if properties.NoCompress else ".pvr"
            texture_path = _sibling(rkm_path.parent, properties.DiffuseTexture + suffix)
            alpha_path = texture_path.with_name(texture_path.stem + ".alpha" + texture_path.suffix)
            texture_dependencies = [texture_path]
            if not properties.NoCompress and alpha_path.is_file():
                alpha_path = _sibling(rkm_path.parent, str(alpha_path.relative_to(rkm_path.parent)))
                texture_dependencies.append(alpha_path)
            else:
                alpha_path = None
            def decode_texture() -> tuple[bytes, int, int]:
                if properties.NoCompress:
                    from PIL import Image
                    data = texture_path.read_bytes()
                    try:
                        with Image.open(io.BytesIO(data)) as image:
                            rgba = image.convert("RGBA")
                    except (OSError, ValueError, SyntaxError) as error:
                        raise NativeInputError(f"Native PNG parse failed: {texture_path.name}: {error}") from error
                else:
                    try:
                        # Resolve sidecars here so Luna cannot read dependencies
                        # outside the checked closure or recurse into alpha-of-alpha.
                        image = PVR(str(texture_path), external_alpha=False).image
                        if image is None:
                            raise NativeInputError(f"Texture decoder returned no image: {texture_path.name}")
                        if alpha_path is not None and not image_has_alpha(image):
                            alpha = PVR(str(alpha_path), external_alpha=False).image
                            if alpha is None:
                                raise NativeInputError(f"Texture decoder returned no image: {alpha_path.name}")
                            image = put_alpha(image, alpha)
                    except (AssertionError, struct.error, NotImplementedError, ValueError) as error:
                        raise NativeInputError(f"Native PVR parse failed: {texture_path.name}: {error}") from error
                    rgba = image.convert("RGBA")
                output = io.BytesIO()
                rgba.save(output, format="PNG")
                return output.getvalue(), rgba.width, rgba.height
            if texture_cache is None:
                png, width, height = decode_texture()
            else:
                png, width, height = texture_cache.get_or_decode(
                    texture_dependencies,
                    settings=f"native-png-v1/no-compress={properties.NoCompress}/external-alpha=false/embedded-preferred/RGBA/pillow-default",
                    decode=decode_texture,
                )
            material_ids[mesh.material_index] = len(materials)
            materials.append({"name": material.name, "rkm_fields": raw_fields, "png_bytes": png,
                              "width": width, "height": height, "resolved_shader": resolved_shader,
                              "native_default_shader": not bool(properties.Shader)})
            if resolved_shader == "rkshader_horn":
                materials[-1]["native_horn_default_discard"] = True
            if hair_uv is not None:
                materials[-1]["native_hair_uv"] = hair_uv
                if "clock" in hair_uv:
                    materials[-1]["native_hair_clock_source"] = first_material
            dependencies.extend(([rkm_path] if has_rkm else []) + texture_dependencies)
        triangles = np.array([[tri.x, tri.y, tri.z] for tri in mesh.triangles], dtype=np.int64)
        if triangles.ndim != 2 or triangles.shape[1] != 3 or not len(triangles):
            raise NativeInputError(f"Invalid or empty native triangles: {mesh.name}")
        if triangles.min() < 0 or triangles.max() >= len(positions):
            raise NativeInputError(f"Native face index out of range: {mesh.name}")
        primitive = {"name": mesh.name, "indices": triangles, "material": material_ids[mesh.material_index]}
        if retained_meshes is not None:
            primitive.update(native_mesh_index=native_mesh_index, default_visible=mesh.name in visible)
        primitives.append(primitive)

    primitives, materials, zero_weight_evidence = _remove_collapsed_zero_weight_triangles(
        primitives, materials, weights)

    def transforms(start: int, end: int) -> dict[str, NDArray[np.float64]]:
        if end >= frames:
            # CSV start/end remain untouched. Repeated output samples model
            # native sampling of the last real source frame, not invented data.
            indices = np.minimum(np.arange(start, end + 1, dtype=np.int64), frames - 1)
            return {"translations": animation.translations[indices],
                    "rotations_xyzw": animation.rotations_xyzw[indices]}
        return {"translations": animation.translations[start:end + 1],
                "rotations_xyzw": animation.rotations_xyzw[start:end + 1]}

    clips = [{**by_name[name], "event_source_status": event_source["status"], **transforms(by_name[name]["start"], by_name[name]["end"])} for name in selected]
    if event_approvals is not None:
        for clip in clips:
            events = []
            for source in [event_source, *extra_event_sources]:
                for event in source.get('approved_events', {}).get(clip['name'], []):
                    if event not in events:
                        events.append(event)
            clip['events'] = sorted(events, key=lambda row: row['frame'])
    declared_rest_frame = by_name[rest_name]["start"] if rest_name is not None else rest_frame
    rest_frame = min(declared_rest_frame, frames - 1)
    result = {"name": model.name, "positions": positions, "texcoords": texcoords, "joints": joints,
            "weights": weights, "primitives": primitives, "materials": materials,
            "bones": [{"name": bone.name, "parent": bone.parentIndex,
                       "bind_global": np.array(bone.matrix_4x4, dtype=np.float64)} for bone in model.bones],
            "clips": clips, "rest": {"name": rest_name, "frame": rest_frame, **transforms(rest_frame, rest_frame)},
            "provenance": {"animation_sha256": animation_digest, "luna_dependency": LUNA_DEPENDENCY, "luna_reader": "luna_kit.model.RKModel", "animation_reader": "luna_kit.model.Anim.raw_frames", "anim_format": {"major": major, "minor": minor, "type": frame_type},
                           "event_source": event_source, "duplicate_csv_rows_merged": duplicates, "unselected_csv_diagnostics": csv_diagnostics,
                           "rest_source": "native-csv-first-frame" if rest_name is not None else "caller-selected-native-frame",
                           "native_position_scale": NATIVE_POSITION_SCALE, "material_sources": material_sources,
                           "default_shader_materials": [item["name"] for item in materials if item["native_default_shader"]],
                           "files": [{"name": path.name} for path in dict.fromkeys(dependencies)]}}
    if extra_event_sources:
        result['provenance']['additional_event_sources'] = extra_event_sources
    if frame_boundary == 'native-clamp':
        result["provenance"]["native_frame_boundary"] = {
            "policy": "native-clamp", "client_version": "11.4.3c",
            "source_frame_count": frames,
            "clips": [{"name": name, "declared_start": by_name[name]['start'],
                       "declared_end": by_name[name]['end'],
                       "clamped_samples": max(0, by_name[name]['end'] - max(by_name[name]['start'], frames) + 1)}
                      for name in selected],
            "rest": {"name": rest_name, "declared_frame": declared_rest_frame, "sampled_frame": rest_frame}}
    if zero_weight_evidence is not None:
        result["provenance"]["zero_weight_triangles"] = zero_weight_evidence
    clocks = [{"material": item["name"], "source_material": item["native_hair_clock_source"],
               **item["native_hair_uv"]["clock"]} for item in materials
              if "clock" in item.get("native_hair_uv", {})]
    if clocks:
        result["provenance"]["material_uv_clock"] = clocks
    return result
