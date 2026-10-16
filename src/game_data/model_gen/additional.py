"""One declared AdditionalObject actor; no task or general scene graph.

The generic native actor uses its Model stem for RK/ANIM/CSV, all default
meshes, and an independent index-zero then random-index playback controller.
The native world bases are adapted to an uncompressed, stationary 3D preview.
See docs/3d-additional-actors.md for the source evidence and coordinate policy.
"""
import json
import math
from pathlib import Path
import struct
from typing import TYPE_CHECKING

import numpy as np

from .converter.core import pose_matrices
from .converter.export import convert_native
from .converter.csv import read_clips
from .converter.native_animation import read_animation_header
from .selection import SelectionError

if TYPE_CHECKING:
    from luna_kit.gameobjectdata import GameObject
    from .converter.export import AssetBundle


CONTROLLER = 'native-first-then-random-v1'
CLIENT_VERSION = '11.4.3c'
CLIENT_SHA256 = 'd4fc2e7650ae866735d4a9a1da4b900262bdcf188f1d34fb9c0ebe6b08609c29'


def _require(condition: object, detail: str) -> None:
    if not condition:
        raise SelectionError('additional_actor_unverified', detail)


def prepare_additional_actor(obj: GameObject, owner_association: dict, report: dict, folder: Path,
                             cache: dict[str, tuple[AssetBundle, dict]]) -> tuple[AssetBundle, dict] | None:
    """Validate the entire numeric-index child library before permitting it.

    Cache ownership is one generate_models call. An excluded owner never writes
    the prepared child assets. Ordinary actors do not enter this path.
    """
    declaration = obj.get('AdditionalObject') or {}
    if not declaration.get('Model') and not declaration.get('Animation'):
        return None
    _require(not set(declaration) - {'Model', 'Animation', 'OffsetX', 'OffsetY', 'Scale', 'LockRotate'},
             'Unknown AdditionalObject fields')
    source = owner_association.get('additional_actor') or {}
    _require(source.get('references_resolved') is True and source.get('declaration') == declaration,
             'AdditionalObject requires its exact resolved native association')
    stem = source['resource_stem']
    _require(source.get('model') == stem + '.rk' and source.get('animation') == stem + '.anim'
             and source.get('csv', {}).get('file') == stem + '.csv', 'AdditionalObject source stem mismatch')
    # Discovery stays strict. This independently established native controller
    # uses clamped source sampling, so revalidate its entire original CSV with
    # the same shared parser and an explicit policy, never filtered audit rows.
    metadata = {}
    try:
        header = read_animation_header(folder/source['animation'])
        clips, duplicates, diagnostics = read_clips(folder/source['csv']['file'], header.frame_count,
            source_metadata=metadata, frame_boundary='native-clamp')
    except (ValueError, OSError) as error:
        raise SelectionError('additional_actor_unverified', f'Invalid AdditionalObject library: {error}') from error
    _require(clips and not diagnostics and not duplicates and metadata['empty_record_count'] == 0
             and metadata['raw_record_count'] == len(clips)
             and metadata['first_declared_line'] == 1
             and metadata['first_declared_name'] == clips[0]['name'],
             'AdditionalObject requires every original CSV index to be valid and unambiguous')
    # The declaration is only model placement. Do not interpret Animation as a
    # clip name: real declarations also use non-clip labels in that field.
    if stem not in cache:
        identity = 'AdditionalObject:' + stem
        bundle = convert_native(folder/source['model'], folder/source['animation'],
            folder/source['csv']['file'], [clip['name'] for clip in clips], identity=identity,
            visible_meshes=report['models'][source['model']]['meshes'],
            rest_name=clips[0]['name'], events_absent=True, frame_boundary='native-clamp',
            animation_filenames=True)
        cache[stem] = (bundle, bundle.catalogue['models'][identity])
    return cache[stem]


def _native_rest_root(bundle: AssetBundle, identity: str, root_name: str | None) -> tuple[int, np.ndarray]:
    """Recover an exact native joint ordinal/global pose from our verified GLB.

    The serializer preserves native bone names and emits local joint transforms
    below ModelRoot. Accumulating those joints without ModelRoot's Rx(pi)
    recovers native coordinates; no RK/ANIM parser or name heuristic is added.
    """
    _require(isinstance(root_name, str) and bool(root_name), 'A declared Model.RootBone is required')
    row = bundle.catalogue['models'][identity]
    data = bundle.files[row['path']]
    size, kind = struct.unpack_from('<II', data, 12)
    _require(kind == 0x4e4f534a, 'Expected generated GLB JSON')
    document = json.loads(data[20:20 + size])
    rig = document['extras']['atpRig']
    names = document['extras']['atpModel']['boneNames']
    matches = [index for index, name in enumerate(names) if name == root_name]
    _require(len(matches) == 1, 'Model.RootBone must resolve to one exact native joint')
    ordinal = matches[0]
    _require(rig['key'] == row['rigKey'] and len(names) == len(rig['parents']) == len(rig['jointNodes']),
             'Generated owner rig metadata differs')
    coordinate_root = document['nodes'][0]
    _require(coordinate_root.get('scale') == [1., 1., 1.] and coordinate_root.get('rotation') == [1, 0, 0, 0],
             'AdditionalObject requires the current scale-one Rx(pi) model coordinates')
    nodes = [document['nodes'][node] for node in rig['jointNodes']]
    _require(all(node.get('scale', [1, 1, 1]) == [1, 1, 1] and 'matrix' not in node for node in nodes),
             'Unsupported generated joint transform')
    local = pose_matrices(np.array([[node['translation'] for node in nodes]], dtype=np.float64),
                          np.array([[node['rotation'] for node in nodes]], dtype=np.float64))[0]
    for index, parent in enumerate(rig['parents']):
        _require(type(parent) is int and -1 <= parent < index, 'Invalid generated joint hierarchy')
        if parent >= 0:
            local[index] = local[parent] @ local[index]
    return ordinal, local[ordinal, :3, 3]


def _native_y(angle: float) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, 0., -s], [0., 1., 0.], [s, 0., c]])


def attach_additional_actor(obj: GameObject, owner_bundle: AssetBundle, identity: str,
                            prepared: tuple[AssetBundle, dict]) -> tuple[dict, dict]:
    """Return the public child reference and private native placement evidence."""
    child_bundle, child_row = prepared
    declaration = obj['AdditionalObject']
    owner = obj.get('Model') or {}
    values = [owner.get('Scale'), declaration.get('Scale'), declaration.get('OffsetX'), declaration.get('OffsetY')]
    _require(all(type(value) in (int, float) and math.isfinite(value) for value in values),
             'AdditionalObject scales/offsets must be finite native numbers')
    owner_scale, child_scale, offset_x, offset_y = values
    _require(owner_scale > 0 and child_scale > 0, 'AdditionalObject scales must be positive')
    locked = declaration.get('LockRotate')
    _require(type(locked) is bool, 'AdditionalObject.LockRotate must be a native boolean')
    root_ordinal, root = _native_rest_root(owner_bundle, identity, owner.get('RootBone'))
    # Float32 constants read from the reviewed native client. Its rotation
    # convention has the opposite angle sign to Three.js's standard matrices.
    angle = 0.0872664675116539
    c, s = math.cos(angle), math.sin(angle)
    ground_x = np.array([[1., 0., 0.], [0., c, s], [0., -s, c]])
    heading = _native_y(0.19634954631328583)
    rotation = ground_x @ _native_y(0.7853981852531433) @ heading
    child_rotation = _native_y(1.5707963705062866) if locked else heading
    native = np.eye(4)
    native[:3, :3] = (child_scale / owner_scale) * rotation.T @ child_rotation
    native[:3, 3] = np.array([root[0], 0., root[2]]) + rotation.T @ np.array(
        [offset_x - 4., offset_y + 64., 0.]) / owner_scale
    # Both current GLBs use scale=1 and Rx(pi). Scale occurs once in the
    # relative world bases, never by rescaling the already serialized mesh.
    coordinates = np.diag([1., -1., -1., 1.])
    matrix = coordinates @ native @ coordinates
    _require(np.isfinite(matrix).all() and abs(np.linalg.det(matrix[:3, :3])) > 1e-12,
             'Invalid AdditionalObject relative transform')
    public = {'model': child_row, 'transform': matrix.T.reshape(-1).tolist(),
              'parentRoot': root_ordinal, 'controller': CONTROLLER}
    private = {'controller': CONTROLLER, 'client_version': CLIENT_VERSION,
               'client_sha256': CLIENT_SHA256, 'child_source': child_bundle.provenance,
               'root_field': 'Model.RootBone', 'parent_root': root_ordinal,
               'rest_root_native': root.tolist(), 'owner_scale': owner_scale,
               'child_scale': child_scale, 'offset': [offset_x, offset_y], 'lock_rotate': locked,
               'coordinates': 'stationary constructor heading; native world offsets; map depth compression removed',
               'root_motion': 'add current minus rest owner-root X/Z in GLTF coordinates each frame'}
    return public, private
