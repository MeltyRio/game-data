"""Adapt Luna's decoded ANIM frames to the converter's RK 5.2 contract.

The Luna revision is resolved in game-data's uv.lock. Format limits and
quaternion validity remain converter policy; binary decoding belongs to Luna.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict
import dataclasses_struct as dcs
import numpy as np
from numpy.typing import NDArray
from luna_kit.model import Anim
from luna_kit.model.anim import Header

NATIVE_POSITION_SCALE = 0.031250953674316406
RECORD_BYTES = {0: 32, 1: 22, 4: 11}


class NativeClip(TypedDict):
    name: str
    start: int
    end: int
    fps: float
    translations: NDArray[np.float64]
    rotations_xyzw: NDArray[np.float64]


@dataclass(frozen=True)
class NativeAnimation:
    header: Header
    translations: NDArray[np.float64]
    rotations_xyzw: NDArray[np.float64]


def read_animation_header(path: str | Path) -> Header:
    path = Path(path)
    size = dcs.get_struct_size(Header)
    with path.open('rb') as handle:
        raw = handle.read(size)
    if len(raw) != size:
        raise ValueError('Truncated animation header')
    header = Header.from_packed(raw)
    major, minor, kind = header.version_major, header.version_minor, header.frame_type
    if header.magic != b'RKFORMAT' or (major, minor) != (5, 2) or kind not in RECORD_BYTES:
        raise ValueError(f'Unsupported animation format: {major}.{minor} / type {kind}')
    if (not header.bone_count or not header.frame_count or
            path.stat().st_size != size + header.bone_count * header.frame_count * RECORD_BYTES[kind]):
        raise ValueError('Animation byte length does not match its declared frame layout')
    return header


def read_animation(path: str | Path) -> NativeAnimation:
    path = Path(path)
    header = read_animation_header(path)
    # A binary stream avoids Luna's optional neighbouring-CSV lookup.
    with path.open('rb') as stream:
        frames = Anim(stream).raw_frames
    translations = frames['position'].copy()
    rotations = frames['quaternion'][..., [1, 2, 3, 0]]  # WXYZ to XYZW.
    if np.any(np.linalg.norm(rotations, axis=-1) <= .5):
        raise ValueError('Degenerate native animation quaternion')
    # Clip and rest poses share these frame arrays; keep slices read-only.
    translations.flags.writeable = False
    rotations.flags.writeable = False
    return NativeAnimation(header, translations, rotations)
