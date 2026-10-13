"""Small intermediate representation and validated coordinate conversion.

Column vectors in native coordinates (head toward negative Y). Global poses
become local TRS; the GLB skeleton root applies Rx(pi) to glTF Y-up. No archive,
webpage, save, or PonyVista dependencies.
"""
from dataclasses import dataclass
import hashlib
import json
import numpy as np


class ConversionError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ConversionError(message)


@dataclass
class Rig:
    key: str
    parents: list[int]
    names: list[str]


@dataclass
class Model:
    name: str
    rig: Rig
    positions: np.ndarray
    texcoords: np.ndarray
    joints: np.ndarray
    weights: np.ndarray
    bind_global: np.ndarray
    rest_global: np.ndarray
    primitives: list[dict]
    materials: list[dict]
    scale: float = 1.0


@dataclass
class Clip:
    name: str
    rig: Rig
    global_frames: np.ndarray
    fps: float


def rig_from_native(native):
    parents = [bone['parent'] for bone in native['bones']]
    anim_source = native.get('provenance', {}).get('animation_sha256')
    require(isinstance(anim_source, str) and len(anim_source) == 64
            and all(c in '0123456789abcdef' for c in anim_source), 'Missing or invalid native animation digest')
    # This token certifies a declared source library plus its indexed hierarchy,
    # never mere equal names or bone counts. Bind matrices are model-specific.
    signature = ['native-global-to-gltf-rx-pi-v1', anim_source, parents]
    key = hashlib.sha256(json.dumps(signature, separators=(',', ':')).encode()).hexdigest()
    names = [bone['name'] for bone in native['bones']]
    return Rig(key, parents, names)


def pose_matrices(translations, rotations):
    t, q = np.asarray(translations, dtype=float), np.asarray(rotations, dtype=float)
    require(t.shape[:-1] == q.shape[:-1] and t.shape[-1] == 3 and q.shape[-1] == 4, 'Invalid pose shape')
    require(np.isfinite(t).all() and np.isfinite(q).all(), 'Non-finite pose')
    lengths = np.linalg.norm(q, axis=-1, keepdims=True)
    require(np.all(lengths > .5), 'Degenerate quaternion')
    x, y, z, w = np.moveaxis(q / lengths, -1, 0)
    m = np.zeros((*t.shape[:-1], 4, 4))
    m[..., 0, 0] = 1 - 2*(y*y + z*z); m[..., 0, 1] = 2*(x*y-z*w); m[..., 0, 2] = 2*(x*z+y*w)
    m[..., 1, 0] = 2*(x*y+z*w); m[..., 1, 1] = 1-2*(x*x+z*z); m[..., 1, 2] = 2*(y*z-x*w)
    m[..., 2, 0] = 2*(x*z-y*w); m[..., 2, 1] = 2*(y*z+x*w); m[..., 2, 2] = 1-2*(x*x+y*y)
    m[..., :3, 3] = t; m[..., 3, 3] = 1
    return m


def local_trs(globals_, parents):
    values = np.asarray(globals_, dtype=float)
    require(values.shape[-3:] == (len(parents), 4, 4), 'Pose/bone layout mismatch')
    require(np.isfinite(values).all(), 'Non-finite pose matrix')
    result = values.copy()
    for bone, parent in enumerate(parents):
        require(-1 <= parent < bone, 'Unsupported parent index/cycle')
        if parent >= 0:
            result[..., bone, :, :] = np.linalg.solve(values[..., parent, :, :], values[..., bone, :, :])
    require(np.allclose(result[..., 3, :], [0, 0, 0, 1], atol=1e-6), 'Non-affine pose')
    rotations = result[..., :3, :3]
    require(np.allclose(np.swapaxes(rotations, -1, -2) @ rotations, np.eye(3), atol=2e-5)
            and np.allclose(np.linalg.det(rotations), 1, atol=2e-5), 'Pose has scale, reflection or shear; unsupported in this prototype')
    quaternions = []
    for r in rotations.reshape(-1, 3, 3):
        # Symmetric Davenport matrix; largest eigenvector is XYZW quaternion.
        qmat = np.array([[r[0,0]-r[1,1]-r[2,2], r[0,1]+r[1,0], r[0,2]+r[2,0], r[2,1]-r[1,2]],
                         [r[0,1]+r[1,0], r[1,1]-r[0,0]-r[2,2], r[1,2]+r[2,1], r[0,2]-r[2,0]],
                         [r[0,2]+r[2,0], r[1,2]+r[2,1], r[2,2]-r[0,0]-r[1,1], r[1,0]-r[0,1]],
                         [r[2,1]-r[1,2], r[0,2]-r[2,0], r[1,0]-r[0,1], np.trace(r)]]) / 3
        _, vectors = np.linalg.eigh(qmat)
        q = vectors[:, -1]
        if q[3] < 0: q = -q
        quaternions.append(q)
    q = np.asarray(quaternions).reshape((*rotations.shape[:-2], 4))
    t = result[..., :3, 3]
    require(np.allclose(pose_matrices(t, q), result, atol=2e-5), 'TRS round-trip mismatch')
    return t, q


def clip_from_native(row, rig):
    """Convert the declared global native poses to the model's exact rig."""
    frames = pose_matrices(row['translations'], row['rotations_xyzw'])
    require(frames.shape[-3] == len(rig.parents), 'Native clip/bone count mismatch')
    return Clip(row['name'], rig, frames, row['fps'])


def from_native(native, *, identity, scale=1.0):
    for primitive in native['primitives']:
        indices = np.asarray(primitive['indices'])
        require(indices.ndim == 2 and indices.shape[1] == 3 and len(indices) > 0
                and (np.issubdtype(indices.dtype, np.integer) or np.issubdtype(indices.dtype, np.floating))
                and np.isfinite(indices).all() and np.equal(indices, np.floor(indices)).all()
                and indices.min() >= 0 and indices.max() < len(native['positions']),
                'Invalid native triangle indices')
    used = sorted({int(v) for p in native['primitives'] for v in np.asarray(p['indices']).reshape(-1)})
    remap = {old: new for new, old in enumerate(used)}
    weights = np.asarray(native['weights'])[used].copy()
    sums = weights.sum(axis=1)
    require(np.isfinite(weights).all() and (weights >= 0).all(), 'Invalid native skin weights')
    require(np.all(np.abs(sums-1) <= 4/65535 + 1e-9), 'Visible weights are not normalized within native quantization tolerance')
    rig = rig_from_native(native)
    # Normalize only native UNORM16 residue, never infer missing weights.
    weights /= sums[:, None]
    joints = np.asarray(native['joints'])[used].copy()
    joints[weights == 0] = 0
    positions = np.asarray(native['positions'])[used].copy()
    bind = np.array([b['bind_global'] for b in native['bones']])
    rest = pose_matrices(native['rest']['translations'], native['rest']['rotations_xyzw'])[0]
    primitives = [{**p, 'indices': np.array([[remap[int(v)] for v in tri] for tri in p['indices']])} for p in native['primitives']]
    model = Model(identity, rig, positions, np.asarray(native['texcoords'])[used], joints, weights, bind, rest,
                  primitives, native['materials'], scale)
    clips = [clip_from_native(row, rig) for row in native['clips']]
    return model, clips
