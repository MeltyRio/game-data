"""Validated RK ANIM 5.2 records, independent of archive and character identity.

Type 4's packed layout follows the public Luna reader (GPL-3.0).
The game-data uv.lock is the sole Luna dependency pin.
Types 0/1 are decoded from documented local byte/bind evidence; see
README.md. No source-specific decoder imports are required.
"""
from dataclasses import dataclass
from pathlib import Path
import struct
import numpy as np

NATIVE_POSITION_SCALE = 0.031250953674316406
HEADER = struct.Struct('<8sII64sIII')
RECORD_BYTES = {0: 32, 1: 22, 4: 11}


@dataclass(frozen=True)
class AnimationHeader:
    major: int
    minor: int
    bone_count: int
    frame_count: int
    frame_type: int


@dataclass(frozen=True)
class NativeAnimation:
    header: AnimationHeader
    translations: np.ndarray
    rotations_xyzw: np.ndarray


def read_animation_header(path: str | Path) -> AnimationHeader:
    path = Path(path)
    with path.open('rb') as handle:
        raw = handle.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise ValueError('Truncated animation header')
    magic, major, minor, _name, bones, frames, kind = HEADER.unpack(raw)
    if magic != b'RKFORMAT' or (major, minor) != (5, 2) or kind not in RECORD_BYTES:
        raise ValueError(f'Unsupported animation format: {major}.{minor} / type {kind}')
    if not bones or not frames or path.stat().st_size != HEADER.size + bones * frames * RECORD_BYTES[kind]:
        raise ValueError('Animation byte length does not match its declared frame layout')
    return AnimationHeader(major, minor, bones, frames, kind)


def read_animation(path: str | Path) -> NativeAnimation:
    path = Path(path)
    header = read_animation_header(path)
    raw = path.read_bytes()
    shape = (header.frame_count, header.bone_count)
    kind = header.frame_type
    if kind == 0:
        records = np.frombuffer(raw, dtype='<f4', offset=HEADER.size).reshape(*shape, 8)
        # The known 32-byte native record includes a unit fourth position lane.
        # Do not silently discard a future file's scale/homogeneous semantics.
        if not np.all(records[..., 7] == 1):
            raise ValueError('Unsupported non-unit type 0 animation transform lane')
        translations = records[..., 4:7].astype(np.float64)
        rotations = records[..., :4].astype(np.float64)
    elif kind == 1:
        dtype = np.dtype([('position', '<f4', 3), ('w', '<f4'), ('xyz', '<i2', 3)])
        records = np.frombuffer(raw, dtype=dtype, offset=HEADER.size).reshape(shape)
        translations = records['position'].astype(np.float64)
        rotations = np.empty((*shape, 4), dtype=np.float64)
        rotations[..., :3] = records['xyz'].astype(np.float64) / 32767
        rotations[..., 3] = records['w']
    else:
        dtype = np.dtype([('position', '<i2', 3), ('w', '<i2'), ('xyz', 'i1', 3)])
        records = np.frombuffer(raw, dtype=dtype, offset=HEADER.size).reshape(shape)
        translations = records['position'].astype(np.float64) * NATIVE_POSITION_SCALE
        rotations = np.empty((*shape, 4), dtype=np.float64)
        rotations[..., :3] = records['xyz'].astype(np.float64) / 127
        rotations[..., 3] = records['w'].astype(np.float64) / 32767
    if not np.isfinite(translations).all() or not np.isfinite(rotations).all():
        raise ValueError('Non-finite native animation transform')
    if np.any(np.linalg.norm(rotations, axis=-1) <= .5):
        raise ValueError('Degenerate native animation quaternion')
    # Clip and rest poses share these frame arrays; keep slices read-only.
    translations.flags.writeable = False
    rotations.flags.writeable = False
    return NativeAnimation(header, translations, rotations)
