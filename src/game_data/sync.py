from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
import math
import mimetypes
import os
from pathlib import Path
import re
from typing import Literal, TYPE_CHECKING, TypedDict

from PIL import Image
import botocore.exceptions
from botocore.exceptions import ClientError
from rich.progress import Progress

from .data_types.common_types import GameObjectId
from .data_types.GameDataTypes import (
    CATEGORY_NAMES,
    CategoryName,
    GameObjects,
    GameVersion,
    GenericObjectType,
)
from .console import COLUMNS, console, track
from .s3 import get_s3_client
from .utils import json_dumps_compact
from .model_paths import is_asset_path, asset_path_matches, model_path, safe_asset_name

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

MAX_WORKERS = 10
HEADERS_TO_CHECK = ['CacheControl', 'ContentType']


def get_image_metadata(file: bytes) -> dict[Literal['width', 'height'], str]:
    image = Image.open(io.BytesIO(file))
    return {
        'width': str(image.width),
        'height': str(image.height),
    }

def upload_file(
    client: 'S3Client',
    bucket: str,
    data: bytes,
    key: str,
    content_type: str | None = None,
    override: bool = False,
    cache_control: str | None = None,
    *,
    strict: bool = False,
):
    metadata = {}

    extra_args = {}

    if content_type:
        extra_args['ContentType'] = content_type
        if content_type.startswith('image/'):
            metadata.update(get_image_metadata(data))
    
    if content_type == 'application/json':
        json_data = json.loads(data)
        data = json_dumps_compact(json_data).encode('utf-8')
    
    if cache_control:
        extra_args['CacheControl'] = cache_control
        
    hash = hashlib.sha256(data).hexdigest()

    metadata['hash'] = hash

    upload = True
    update = False
    current_metadata: dict[str, str] = metadata

    if override:
        metadata['override'] = 'true'
    else:
        try:
            object = client.head_object(Bucket = bucket, Key = key)
            current_metadata = object['Metadata']
            if strict and current_metadata.get('override') == 'true' and current_metadata.get('hash') != hash:
                raise ValueError(f'Cannot publish 3D resource over a different remote override: {key}')
            if object['Metadata'].get('override') == 'true' or object['Metadata'].get('hash') == hash:
                upload = False
            
            for header in HEADERS_TO_CHECK:
                if object.get(header) != extra_args.get(header):
                    update = True
            
        except botocore.exceptions.ClientError as e:
            if e.response.get('Error', {}).get('Code', '') == '404':
                upload = True
            else:
                raise
    
    if upload:
        try:
            client.put_object(
                Bucket = bucket,
                Key = key,
                Body = data,
                Metadata = metadata,
                **extra_args,
            )
        except Exception as e:
            e.add_note(f'File: {key}')
            if strict:
                raise
            console.print_exception()
    elif update:
        console.print(f'Updating: {key}')
        try:
            client.copy_object(
                Bucket = bucket,
                Key = key,
                CopySource = {'Bucket': bucket, 'Key': key},
                Metadata = current_metadata,
                MetadataDirective = 'REPLACE',
                **extra_args,
            )
        except Exception as e:
            e.add_note(f'File: {key}')
            if strict:
                raise
            console.print_exception()
    
    return hash

def upload_single_file(
    client: 'S3Client',
    bucket: str,
    filepath: Path,
    key: str,
):
    file_data = filepath.read_bytes()
    content_type, _ = mimetypes.guess_type(filepath)

    cache_control = 'max-age=2592000'

    return upload_file(
        client = client,
        bucket = bucket,
        data = file_data,
        key = key,
        content_type = content_type,
        cache_control = cache_control,
    )


def prepare_model_upload(dist_folder: Path):
    """Validate the local 3D reference closure before making any cloud calls.

    Historical content-addressed GLBs can stay in the owned namespace. They
    are neither uploaded nor deleted unless the current catalogue uses them.
    """
    catalogue_path = dist_folder/'models_3d.json'
    model_dir = dist_folder/'models/3d'
    if model_dir.exists() and not model_dir.resolve().is_relative_to(dist_folder.resolve()):
        raise ValueError('3D resources leave the build directory')
    local_files = [path for path in model_dir.rglob('*') if path.is_file()] if model_dir.exists() else []
    if not catalogue_path.exists():
        if local_files:
            raise ValueError('3D files exist without models_3d.json')
        return None, {}
    if not catalogue_path.resolve().is_relative_to(dist_folder.resolve()):
        raise ValueError('3D catalogue leaves the build directory')
    for path in local_files:
        if not is_asset_path(path.relative_to(dist_folder).as_posix()):
            raise ValueError('Unexpected file in the owned 3D namespace')
    data = catalogue_path.read_bytes()
    def invalid_constant(value):
        raise ValueError('Non-finite value in 3D catalogue: ' + value)
    catalogue = json.loads(data, parse_constant=invalid_constant)
    if (not isinstance(catalogue, dict) or type(catalogue.get('schemaVersion')) is not int
            or catalogue['schemaVersion'] not in (1, 2, 3) or not isinstance(catalogue.get('models'), dict)):
        raise ValueError('Unsupported 3D catalogue')
    assets = {}
    def asset(ref, name=None, *, fixed_model=False):
        if not isinstance(ref, dict):
            raise ValueError('Invalid 3D asset reference')
        digest, rig, size = ref.get('sha256'), ref.get('rigKey'), ref.get('bytes')
        if (not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
                or not isinstance(rig, str) or not re.fullmatch(r'[a-f0-9]{64}', rig)
                or type(size) is not int or not 0 < size <= 9007199254740991
                or not asset_path_matches(ref.get('path'), digest, name, fixed_model=fixed_model)):
            raise ValueError('Invalid 3D asset identity, size or path')
        key = ref['path']
        value = {'sha256': digest, 'bytes': size, 'rigKey': rig,
                 'cache_control': 'no-cache' if fixed_model and safe_asset_name(name) and key == model_path(name)
                                  else 'max-age=2592000'}
        if key in assets and assets[key] != value:
            raise ValueError('Conflicting shared 3D asset reference: ' + key)
        if key not in assets:
            path = dist_folder/key
            if not path.is_file() or not path.resolve().is_relative_to(model_dir.resolve()):
                raise ValueError('Missing or external 3D dependency: ' + key)
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if path.stat().st_size != size or actual != digest:
                raise ValueError('3D dependency size or digest mismatch: ' + key)
            assets[key] = value
    def actor(model, *, identity=None, require_playable=False):
        asset(model, identity, fixed_model=identity is not None)
        animations = model.get('animations')
        if not isinstance(animations, list) or (require_playable and not animations):
            raise ValueError('Invalid 3D animation list')
        ids = set()
        for animation in animations:
            asset(animation, animation.get('id') if isinstance(animation, dict) else None)
            name, label, duration = animation.get('id'), animation.get('name'), animation.get('duration')
            if (not isinstance(name, str) or not name or name in ids
                    or not isinstance(label, str) or not label
                    or type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0
                    or (require_playable and duration == 0)
                    or animation['rigKey'] != model['rigKey']):
                raise ValueError('Invalid or incompatible 3D animation')
            ids.add(name)
        if 'interactions' in model:
            profile = model['interactions']
            if (catalogue['schemaVersion'] != 3 or not isinstance(profile, dict)
                    or set(profile) not in ({'version', 'clickAnimations'}, {'version', 'clickAnimations', 'dizzyAnimation'})
                    or type(profile.get('version')) is not int or profile['version'] != 1
                    or not isinstance(profile.get('clickAnimations'), list)
                    or any(not isinstance(name, str) or name not in ids for name in profile['clickAnimations'])
                    or len(profile['clickAnimations']) != len(set(profile['clickAnimations']))
                    or ('dizzyAnimation' in profile and profile['dizzyAnimation'] not in ids)):
                raise ValueError('Invalid native interaction catalogue profile')

    def additional_actor(child):
        if (not isinstance(child, dict) or 'additionalActor' in child
                or child.get('controller') != 'native-first-then-random-v1'
                or type(child.get('parentRoot')) is not int
                or not 0 <= child['parentRoot'] <= 9007199254740991):
            raise ValueError('Invalid 3D additional actor controller or parent joint')
        model = child.get('model')
        if not isinstance(model, dict) or 'additionalActor' in model:
            raise ValueError('Nested or invalid 3D additional actor model')
        matrix = child.get('transform')
        if (not isinstance(matrix, list) or len(matrix) != 16
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in matrix)
                or any(abs(matrix[index]) > 1e-6 for index in (3, 7, 11))
                or abs(matrix[15] - 1) > 1e-6):
            raise ValueError('Invalid 3D additional actor affine transform')
        # Column-major attachment matrix: a positive uniform scale and rotation.
        # Normalize before dot products to avoid squaring large finite values.
        columns = [matrix[offset:offset+3] for offset in (0, 4, 8)]
        scales = [math.hypot(*column) for column in columns]
        if (any(not math.isfinite(scale) or scale <= 0 for scale in scales)
                or any(not math.isclose(scale, scales[0], rel_tol=1e-6) for scale in scales[1:])):
            raise ValueError('Invalid 3D additional actor uniform scale')
        a, b, c = [[value/scale for value in column] for column, scale in zip(columns, scales)]
        dot = lambda left, right: sum(x*y for x, y in zip(left, right))
        determinant = (a[0]*(b[1]*c[2]-b[2]*c[1]) - a[1]*(b[0]*c[2]-b[2]*c[0])
                       + a[2]*(b[0]*c[1]-b[1]*c[0]))
        if (any(abs(value) > 1e-6 for value in (dot(a, b), dot(a, c), dot(b, c)))
                or abs(determinant - 1) > 1e-6):
            raise ValueError('Invalid 3D additional actor rotation')
        actor(model, require_playable=True)

    for identity, model in catalogue['models'].items():
        if not isinstance(identity, str) or not identity or any(ord(c) < 32 for c in identity):
            raise ValueError('Invalid 3D model identity')
        actor(model, identity=identity)
        if 'additionalActor' in model:
            if catalogue['schemaVersion'] not in (2, 3):
                raise ValueError('3D additional actors require catalogue schema 2')
            additional_actor(model['additionalActor'])
    return data, assets


def upload_model_asset(client, bucket, dist_folder, key, ref):
    # Check the actual uploaded bytes too, not just the earlier preflight read.
    path = dist_folder/key
    model_dir = (dist_folder/'models/3d').resolve()
    if not model_dir.is_relative_to(dist_folder.resolve()) or not path.resolve().is_relative_to(model_dir):
        raise ValueError('3D resource left the build directory after validation: ' + key)
    data = path.read_bytes()
    if len(data) != ref['bytes'] or hashlib.sha256(data).hexdigest() != ref['sha256']:
        raise ValueError('3D resource changed after validation: ' + key)
    return upload_file(client, bucket, data, key, 'model/gltf-binary',
                       cache_control=ref['cache_control'], strict=True)


def sync(dist_folder: str | Path, bucket: str):
    dist_folder = Path(dist_folder)
    model_catalogue, model_assets = prepare_model_upload(dist_folder)
    generate_diff(dist_folder, bucket)

    console.print('Uploading files')

    client = get_s3_client(bucket, max_workers = MAX_WORKERS)

    upload_tasks: list[tuple[Path, str]] = []

    for file_path in dist_folder.rglob('*'):
        if file_path.is_file():
            key = file_path.relative_to(dist_folder).as_posix()
            if key in {'models_3d.json', 'manifest.json'} or key.startswith('models/3d/'):
                continue
            upload_tasks.append((file_path, key))


    executor = ThreadPoolExecutor(max_workers = MAX_WORKERS)

    manifest: dict[str, str] = {}

    try:
        # Reuse this uploader and executor. Every binary result is observed
        # before a catalogue or root manifest can advertise the new generation.
        # Fixed model keys can already be replaced if another upload fails.
        model_futures = [executor.submit(upload_model_asset, client, bucket, dist_folder, key, ref)
                         for key, ref in model_assets.items()]
        model_error = None
        for future in as_completed(model_futures):
            try:
                future.result()
            except Exception as error:
                model_error = model_error or error
        if model_error is not None:
            raise model_error

        futures = {
            executor.submit(upload_single_file, client, bucket, local, key): key
            for local, key in upload_tasks
        }
        
        with Progress(
            *COLUMNS,
            console = console,
        ) as progress:
            progress_task = progress.add_task('Uploading...', total = len(upload_tasks))
            for future in as_completed(futures):
                progress.advance(progress_task)
                key = futures[future]

                try:
                    if key.endswith('.json'):
                        manifest[key] = future.result()
                except Exception:
                    console.print_exception()
        
    except KeyboardInterrupt:
        executor.shutdown(wait = True, cancel_futures = True)
        console.print('[red]Operation canceled[/]')
        if model_catalogue is not None:
            raise
    finally:
        executor.shutdown(wait = True)
    
    if model_catalogue is not None:
        manifest['models_3d.json'] = upload_file(
            client, bucket, model_catalogue, 'models_3d.json', 'application/json',
            cache_control='max-age=2592000', strict=True,
        )

    console.print('Uploading manifest')

    client.put_object(
        Bucket = bucket,
        Key = 'manifest.json',
        Body = json_dumps_compact(manifest).encode('utf-8'),
        ContentType = 'application/json',
        CacheControl = 'no-cache',
    )


"""
{
    '11.1.1a': {
        '11.1.1.20': {
            'game_objects': {
                'added': ['Pony_Twilight'],
                'changed': ['Pony_Twilight'],
                'removed': ['Pony_Twilight'],
            }
        }
    }
}
"""

type Changelog = dict[
    str,
    dict[
        str,
        dict[
            Literal['game_objects'],
            dict[
                CategoryName,
                dict[
                    Literal['added', 'changed', 'removed'],
                    list[str]
                ]
            ]
        ]
    ]
]


def generate_diff(dist_folder: str | Path, bucket: str):
    dist_folder = Path(dist_folder)
    
    client = get_s3_client(bucket)

    with open(dist_folder/'game_version.json', 'r', encoding = 'utf-8') as file:
        current_version = GameVersion.model_validate_json(file.read())

    try:
        last_version = GameVersion.model_validate_json(
            client.get_object(
                Bucket = bucket,
                Key = 'game_version.json',
            )['Body'].read(),
        )
    except ClientError:
        last_version = current_version
    
    changelog: Changelog = {}

    try:
        changelog = json.load(
            client.get_object(
                Bucket = bucket,
                Key = 'changelog.json',
            )['Body']
        )
    except ClientError:
        pass

    if current_version != last_version:
        console.print('Generating diff')

        try:
            last_game_objects = GameObjects.model_validate_json(
                client.get_object(
                    Bucket = bucket,
                    Key = 'game_objects.json',
                )['Body'].read(),
            )
        except ClientError:
            return

        with open(dist_folder/'game_objects.json', 'r', encoding = 'utf-8') as file:
            current_game_objects = GameObjects.model_validate_json(file.read())
        
        game_objects_diff: dict[CategoryName, dict[Literal['added', 'changed', 'removed'], list[str]]] = {}
        
        for category in CATEGORY_NAMES:
            if not hasattr(last_game_objects, category) or not hasattr(current_game_objects, category):
                continue

            last_objects: dict[GameObjectId, GenericObjectType] = getattr(last_game_objects, category).objects
            current_objects: dict[GameObjectId, GenericObjectType] = getattr(current_game_objects, category).objects

            added = set(current_objects) - set(last_objects)
            removed = set(last_objects) - set(current_objects)

            changed = set[GameObjectId]()

            for gameObject in current_objects.values():
                if gameObject.id not in last_objects:
                    continue

                if gameObject != last_objects[gameObject.id]:
                    changed.add(gameObject.id)
                
            game_objects_diff[category] = {
                'added': list(added),
                'changed': list(changed),
                'removed': list(removed),
            }
            
        changelog.setdefault(
            current_version.game_version,
            {},
        ).setdefault(
            current_version.content_version,
            {},
        )['game_objects'] = game_objects_diff

    with open(dist_folder/'changelog.json', 'w', encoding = 'utf-8') as file:
        json.dump(changelog, file, indent = 2, ensure_ascii = False)
