"""Adapt Luna's decoded ANIM frames to the converter's RK 5.2 contract.

The Luna revision is resolved in game-data's uv.lock. Format limits and
quaternion validity remain converter policy; binary decoding belongs to Luna.
"""
from dataclasses import dataclass
from pathlib import Path
import struct
import numpy as np
from numpy.typing import NDArray
from luna_kit.model import Anim

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
    translations: NDArray[np.float64]
    rotations_xyzw: NDArray[np.float64]


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
