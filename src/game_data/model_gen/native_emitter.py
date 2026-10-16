"""The reviewed PonyBook dizzy emitter, using Luna for native materials/textures.

This is deliberately not a general particle importer. Other event names, sound,
scene callbacks, controllers and position modes require their own evidence.
The original event anchors this finite world-2D burst at the model origin, not
at a guessed head bone. The viewer billboards that plane around the same origin.
"""
from __future__ import annotations

import hashlib
import io
import math
import struct
from pathlib import Path
from xml.etree import ElementTree as ET

from .converter.native import NativeInputError, _sibling

DIZZY_EMITTER = 'mlp_arena_pony_defeat_02'


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise NativeInputError(message)


def _node(node: ET.Element | None, tag: str, fields: list[str]) -> list[float]:
    _check(node is not None and node.tag == tag and not len(node)
           and set(node.attrib) == set(fields), 'Unsupported native emitter ' + tag)
    try:
        values = [float(node.attrib[key]) for key in fields]
    except ValueError as error:
        raise NativeInputError('Invalid native emitter number') from error
    _check(all(math.isfinite(value) and abs(value) <= 4096 for value in values),
           'Non-finite or excessive native emitter value')
    return values


def parse_dizzy_particle(raw: bytes) -> tuple[str, dict]:
    """Retain native numeric values; reject layouts not verified by this player."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise NativeInputError('Malformed native emitter XML') from error
    _check(root.tag == 'ParticleTemplate' and not root.attrib and
           [node.tag for node in root] == ['Texture', 'Timing', 'Force', 'Min', 'Max'],
           'Unsupported native emitter template')
    texture = root.find('Texture')
    _check(texture is not None and not len(texture) and
           set(texture.attrib) == {'name', 'u1', 'v1', 'u2', 'v2'},
           'Unsupported native emitter texture')
    material = texture.get('name')
    _check(bool(material) and Path(material).name == material and '/' not in material and '\\' not in material,
           'Invalid native emitter material name')
    texture.attrib.pop('name')
    uv = _node(texture, 'Texture', ['u1', 'v1', 'u2', 'v2'])
    _check(all(0 <= n <= 1 for n in uv) and uv[0] < uv[2] and uv[1] < uv[3],
           'Invalid native emitter UV')
    rate, lifetime, count, warp, speed = _node(root.find('Timing'), 'Timing',
        ['EmissionRate', 'Lifetime', 'ParticleCount', 'TimeWarp', 'SpeedMultiplier'])
    _check(rate == -1 and count.is_integer() and 0 < count <= 128 and
           0 < lifetime <= 10 and 0 < warp <= 4 and 0 < speed <= 4,
           'Unsupported native emitter timing')
    force = _node(root.find('Force'), 'Force', ['x', 'y', 'z', 'w'])
    _check(force == [0, 0, 0, 0], 'Unreviewed dizzy emitter force')
    result = dict(rate=rate, count=int(count), lifetime=lifetime, timeWarp=warp,
                  speed=speed, force=force, uv=uv)
    for side in ('Min', 'Max'):
        parent = root.find(side)
        _check(not parent.attrib and [node.tag for node in parent] == ['Delta0', 'Delta1', 'Delta2'],
               'Unsupported native emitter segments')
        rows, portions, modes = [], [], []
        for node in parent:
            _check(not node.attrib and [child.tag for child in node] ==
                   ['Position', 'Rotation', 'Colour', 'Size', 'Time'],
                   'Unsupported native emitter segment fields')
            xyz_mode = _node(node.find('Position'), 'Position', ['x', 'y', 'z', 's'])
            _check(xyz_mode[2] == 0, 'Unreviewed native emitter depth')
            rows.append(xyz_mode[:3] + _node(node.find('Rotation'), 'Rotation', ['rot']) +
                        _node(node.find('Colour'), 'Colour', ['r', 'g', 'b', 'a']) +
                        _node(node.find('Size'), 'Size', ['x', 'y']))
            portions.extend(_node(node.find('Time'), 'Time', ['portion']))
            modes.append(xyz_mode[3])
        _check(modes == [5, 6, 6] and portions[0] == 0 and all(x > 0 for x in portions[1:]),
               'Unreviewed dizzy emitter position/time profile')
        _check(all(all(0 <= n <= 1 for n in row[4:8]) and all(n > 0 for n in row[8:]) for row in rows),
               'Invalid native emitter colour/size')
        if side == 'Max':
            _check(result['portions'] == portions, 'Mismatched native emitter segment durations')
            _check(all(a <= b for low, high in zip(result['min'], rows) for a, b in zip(low, high)),
                   'Reversed native emitter ranges')
        result.update({side.lower(): rows, 'portions': portions, 'positionModes': [5, 6, 6]})
    return material, result


def _read_native_emitter(folder: str | Path, name: str) -> tuple[dict, bytes, dict[str, str]]:
    """Return (JSON profile without texture index, PNG bytes, private sources)."""
    from PIL import Image
    from luna_kit.model.rk import parse_rkm
    from luna_kit.pvr import PVR
    from luna_kit.utils import image_has_alpha, put_alpha

    _check(name == DIZZY_EMITTER, 'Unsupported native emitter: ' + str(name))
    folder = Path(folder)
    source = _sibling(folder, name + '.particle')
    raw = source.read_bytes()
    material_name, particle = parse_dizzy_particle(raw)
    material_path = _sibling(folder, material_name + '.rkm')
    material = parse_rkm(str(material_path))
    _check(material.Shader == 'rkshader_particle' and material.BlendMode == 'add' and
           not material.DepthTest and not material.DepthWrite and material.ClampMode == 'RK_REPEAT',
           'Unsupported native emitter material state')
    _check(not material.UseMipmaps and not material.MaterialTemplateType and not material.UserVector0_0,
           'Unreviewed native emitter material controller')
    path = _sibling(folder, material.DiffuseTexture + ('.png' if material.NoCompress else '.pvr'))
    sources = [source, material_path, path]
    if material.NoCompress:
        with Image.open(path) as image:
            _check(image.format == 'PNG', 'Emitter texture must be PNG')
            image = image.convert('RGBA')
    else:
        image = PVR(str(path), external_alpha=False).image
        _check(image is not None, 'Missing native emitter decoded image')
        alpha_path = path.with_name(path.stem + '.alpha.pvr')
        if alpha_path.exists() or alpha_path.is_symlink():
            alpha_path = _sibling(folder, alpha_path.name)
            sources.append(alpha_path)
            if not image_has_alpha(image):
                alpha = PVR(str(alpha_path), external_alpha=False).image
                _check(alpha is not None and alpha.size == image.size, 'Invalid emitter alpha image')
                image = put_alpha(image, alpha)
        image = image.convert('RGBA')
    output = io.BytesIO()
    image.save(output, format='PNG')
    profile = {'version': 1, 'coordinateSpace': 'native-world-2d', 'anchor': 'model-origin',
               'particle': {**particle, 'blend': 'add'}}
    provenance = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    return profile, output.getvalue(), provenance


def load_native_emitter(folder: str | Path, name: str) -> tuple[dict, bytes, dict[str, str]]:
    """Treat a failed optional effect closure as unsupported, never as no event."""
    try:
        return _read_native_emitter(folder, name)
    except NativeInputError:
        raise
    except (OSError, ValueError, TypeError, AssertionError, struct.error, NotImplementedError) as error:
        raise NativeInputError('Native emitter dependency could not be decoded: ' + str(error)) from error
