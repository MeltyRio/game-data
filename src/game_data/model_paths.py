"""GLB key forms shared by conversion, staging and sync."""
import re
from typing import TypeGuard

_SAFE_NAME = re.compile(r'[A-Za-z0-9_-]+')
_FIXED_MODEL = re.compile(r'models/3d/[A-Za-z0-9_-]+\.glb')
_ASSET = re.compile(r'models/3d/([a-f0-9]{64})(?:/([A-Za-z0-9_-]+))?\.glb')
_WARDROBE = re.compile(r'models/3d/[A-Za-z0-9_-]+_wardrobe_([a-f0-9]{64})\.glb')
_ANIMATION = re.compile(r'models/3d/anim/([a-f0-9]{64})/([A-Za-z0-9_-]+)\.glb')


def safe_asset_name(name: object) -> TypeGuard[str]:
    return isinstance(name, str) and _SAFE_NAME.fullmatch(name) is not None


def asset_path(digest: str, name: str | None = None) -> str:
    if name is not None and not safe_asset_name(name):
        raise ValueError('Unsupported native asset filename')
    suffix = digest if name is None else digest + '/' + name
    return 'models/3d/' + suffix + '.glb'


def animation_path(digest: str, name: str | None = None) -> str:
    if name is None:
        return asset_path(digest)
    if not safe_asset_name(name):
        raise ValueError('Unsupported native animation filename')
    return 'models/3d/anim/' + digest + '/' + name + '.glb'


def model_path(identity: str) -> str:
    if not safe_asset_name(identity):
        raise ValueError('Unsupported native model filename')
    return 'models/3d/' + identity + '.glb'


def wardrobe_path(digest: str, identity: str) -> str:
    return model_path(identity + '_wardrobe_' + digest)


def is_asset_path(path: object) -> bool:
    return (isinstance(path, str) and
            (_FIXED_MODEL.fullmatch(path) is not None or _ASSET.fullmatch(path) is not None
             or _ANIMATION.fullmatch(path) is not None))


def asset_digest(path: object) -> str | None:
    match = (_ASSET.fullmatch(path) or _ANIMATION.fullmatch(path) or _WARDROBE.fullmatch(path)) if isinstance(path, str) else None
    return match.group(1) if match else None


def asset_path_matches(path: object, digest: str, name: str | None = None, *,
                       fixed_model: bool = False, wardrobe: bool = False) -> bool:
    if wardrobe:
        return path == asset_path(digest) or safe_asset_name(name) and path == wardrobe_path(digest, name)
    # Legacy flat paths are valid even when their identity was never a filename.
    return (path == asset_path(digest) or
            safe_asset_name(name) and (path == asset_path(digest, name) or
                                       not fixed_model and path == animation_path(digest, name) or
                                       fixed_model and path == model_path(name)))
