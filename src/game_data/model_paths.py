"""GLB key forms shared by conversion, staging and sync."""
import re

_SAFE_NAME = re.compile(r'[A-Za-z0-9_-]+')
_FIXED_MODEL = re.compile(r'models/3d/[A-Za-z0-9_-]+\.glb')
_ASSET = re.compile(r'models/3d/([a-f0-9]{64})(?:/([A-Za-z0-9_-]+))?\.glb')


def safe_asset_name(name):
    return isinstance(name, str) and _SAFE_NAME.fullmatch(name) is not None


def asset_path(digest, name=None):
    if name is not None and not safe_asset_name(name):
        raise ValueError('Unsupported native asset filename')
    suffix = digest if name is None else digest + '/' + name
    return 'models/3d/' + suffix + '.glb'


def model_path(identity):
    if not safe_asset_name(identity):
        raise ValueError('Unsupported native model filename')
    return 'models/3d/' + identity + '.glb'


def is_asset_path(path):
    return (isinstance(path, str) and
            (_FIXED_MODEL.fullmatch(path) is not None or _ASSET.fullmatch(path) is not None))


def asset_digest(path):
    match = _ASSET.fullmatch(path) if isinstance(path, str) else None
    return match.group(1) if match else None


def asset_path_matches(path, digest, name=None, *, fixed_model=False):
    # Legacy flat paths are valid even when their identity was never a filename.
    return (path == asset_path(digest) or
            safe_asset_name(name) and (path == asset_path(digest, name) or
                                       fixed_model and path == model_path(name)))
