# PYTHON_ARGCOMPLETE_OK

from argparse import ArgumentParser
import io
import json
import mimetypes
import os
from pathlib import Path
import resource
import shutil
from time import perf_counter

from PIL import Image
import argcomplete
from botocore.exceptions import ClientError

from luna_kit.api import API, Version
from luna_kit.gameobjectdata import GameObjectData
from luna_kit.typings import DLCManifest

from .data_types import GameData
from .data_types.GameDataTypes import GameVersion
from .app_info import StoreManager
from .console import console
from .crop import crop_image
from .downloader import download
from .env import GAME_DATA_ENV_VAR, is_dev, load_env, set_mode
from .extractor import extract
from .image_gen import generate_images
from .model_gen import generate_models
from .model_gen.converter.animation_cache import AnimationCache
from .notify import Notifier
from .s3 import DEFAULT_BUCKET, get_s3_client
from .sync import sync, upload_file
from .transformer import Transformer


GAME_VERSION_KEY = 'game_version.json'
GAME_VERSION_CHECKER_KEY = 'game_version_checker/game_version.json'
GAME_VERSION_CHECKER_DLC_MANIFEST_KEY = 'game_version_checker/current_dlc_manifest.json'


def get_s3_version(s3_client, bucket: str, key: str) -> GameVersion | None:
    """Gets a version file from the bucket. Returns None if it does not exist"""
    try:
        version_file = s3_client.get_object(
            Bucket = bucket,
            Key = key,
        )
        return GameVersion.model_validate_json(version_file['Body'].read())
    except Exception:
        return None


def compare_versions(version: str, other: str) -> int:
    """
    Compares two versions

    Returns 1 if the version is newer, -1 if it is older and 0 if they are the same
    """

    try:
        parsed = Version.parse(version)
        parsed_other = Version.parse(other)
    except ValueError:
        # Fall back to comparing the versions as strings if they cannot be parsed
        if version == other:
            return 0
        return 1 if version > other else -1

    if parsed == parsed_other:
        return 0

    return 1 if parsed > parsed_other else -1


def build_cdn(
    raw_dir: str | Path,
    dist_dir: str | Path,
    version: str,
    upload: bool,
    overrides_dir: str | Path,
    skip: list[str] | None = None,
    ffdec: str = 'ffdec',
    force: bool = False,
) -> bool | None:
    BUCKET = os.environ.get('S3_BUCKET')
    PRIVATE_BUCKET = os.environ.get('PRIVATE_S3_BUCKET')

    if upload and not BUCKET:
        raise ValueError('Missing S3_BUCKET environment variable')

    start_time = perf_counter()

    if skip is None:
        skip = []

    dist_dir = Path(dist_dir)
    raw_dir = Path(raw_dir)
    arks_dir = raw_dir/'arks'
    extracted_dir = raw_dir/'extracted'

    latest_dlc_manifest: DLCManifest | None = None
    s3_client = get_s3_client(BUCKET) if upload and BUCKET else None

    notifier = Notifier()

    success: bool = True
    failed_rkms = set()
    model_cache = None

    aes_key: bytes | None = None

    if BUCKET and s3_client:
        checker_version = get_s3_version(s3_client, BUCKET, GAME_VERSION_CHECKER_KEY)
        published_version = get_s3_version(s3_client, BUCKET, GAME_VERSION_KEY)

        last_version = checker_version or published_version

        if last_version is None:
            try:
                with open(dist_dir/'game_version.json', 'r', encoding = 'utf-8') as file:
                    last_version = GameVersion.model_validate_json(file.read())
            except:
                console.print('Could not get current version')

        # If the detected version is not the published version, the build of that version
        # never finished. The build has to be redone, but the version was already announced,
        # so the notifications are skipped.
        pending_version = (
            checker_version.game_version
            if (
                checker_version is not None
                and published_version is not None
                and checker_version.game_version != published_version.game_version
            )
            else None
        )

        if  version == 'latest' or force:
            console.print('getting app info')

            store_manager = StoreManager(
                public_bucket = BUCKET,
                secret_bucket = PRIVATE_BUCKET,
            )
            
            app_info = store_manager.get_app_info()
            console.print('got app info')
            if version == 'latest':
                latest_version = app_info.version
            else:
                latest_version = version

            # The version code is only known for the version the store reports
            version_code = app_info.version_code if app_info.version == latest_version else None

            notifier.version = latest_version
            notifier.release_notes = app_info.release_notes
            notifier.app_icon = app_info.icon_url

            version = latest_version

            version_diff = compare_versions(latest_version, last_version.game_version) if last_version else 1

            if version_diff > 0:
                console.print(f'New app version found: [yellow]{latest_version}[/]')
                notifier.notify('app')

                if last_version:
                    last_version.game_version = latest_version
                else:
                    last_version = GameVersion(
                        game_version = latest_version
                    )

                try:
                    s3_client.put_object(
                        Bucket = BUCKET,
                        Key = GAME_VERSION_CHECKER_KEY,
                        Body = last_version.model_dump_json().encode('utf-8'),
                        ContentType = 'application/json',
                    )
                except:
                    console.print('[red]Failed to save version[/]')

                try:
                    aes_key = store_manager.fetch_aes_key(Version.parse(latest_version), version_code)
                except:
                    console.print('[red]Could not get aes key[/]')

                if aes_key is None:
                    console.print('[yellow]No aes key, the ark files might not be extractable[/]')
                
            elif pending_version is not None:
                # The detected version was never published, so the build of it did not finish.
                # Build it again, but skip the notifications, the version was already announced.
                version = pending_version
                console.print(f'Pending build found for version [yellow]{version}[/]')

                if compare_versions(latest_version, version) == 0:
                    # The key of the version might not be known yet, get it from the apk again
                    try:
                        aes_key = store_manager.fetch_aes_key(Version.parse(version), version_code)
                    except:
                        console.print('[red]Could not get aes key[/]')
                else:
                    console.print(f'[yellow]The store has version {latest_version} instead of {version}, the known keys will be used[/]')

                if aes_key is None:
                    console.print('[yellow]No aes key, the ark files might not be extractable[/]')
            elif version_diff < 0:
                # The store has an older version than the one we have, do not downgrade
                console.print('[green]All up to date![/]')
                return True
            else:
                try:
                    last_dlc_manifest_file = s3_client.get_object(
                        Bucket = BUCKET,
                        Key = GAME_VERSION_CHECKER_DLC_MANIFEST_KEY,
                    )
                    last_dlc_manifest = json.load(last_dlc_manifest_file['Body'])

                    api = API('android', latest_version)
                    latest_dlc_manifest = api.get_dlc_manifest()

                    if last_dlc_manifest == latest_dlc_manifest:
                        console.print('[green]All up to date![/]')
                        return True
                    else:
                        console.print('New content update found!')
                        notifier.notify('content')

                except ClientError:
                    console.print('Could not check dlc_manifest')
                    notifier.notify('content')
                
            console.print(f'[green]Found version {version}[/]')
        else:
            if last_version and compare_versions(version, last_version.game_version) < 0:
                console.print(f'[red]Selected version {version} is lower than last version {last_version.game_version}[/]')
                if console.input(f'Continue (y/N)? ').strip() in ['', 'n', 'no', 'f', 'false']:
                    console.print('Quitting')
                    return
                console.print(f'Fetching version: {version}')

    else:
        console.print(f'version: {version}')
        


    if 'download' not in skip:
        shutil.rmtree(arks_dir, ignore_errors = True)
    if 'extract' not in skip:
        shutil.rmtree(extracted_dir, ignore_errors = True)
    if 'transform' not in skip:
        shutil.rmtree(dist_dir, ignore_errors = True)

    os.makedirs(dist_dir, exist_ok = True)
    
    if 'download' not in skip:
        console.print('Downloading files')
        download(arks_dir, version)
        console.line()

    if 'extract' not in skip:
        console.print('Extracting files')
        try:
            failed_rkms = extract(arks_dir, extracted_dir, aes_key = aes_key)
        except:
            console.print_exception()
            console.print('[red]Failed to extract files[/]')
            success = False
            if 'model_gen' not in skip:
                return False
        else:
            if failed_rkms and 'model_gen' not in skip:
                console.print('[yellow]RKM extraction failed; affected 3D models will be excluded[/]')

        console.line()

    if success:
        game_data: GameData | None = None
        
        if 'transform' not in skip:
            console.print('Transforming data')
            transformer = Transformer(
                extracted_dir,
                dist_dir,
                overrides_dir,
                version,
                ffdec = ffdec,
            )

            transformer.start()
            transformer.save()
            game_data = transformer.game_data

            console.line()
        else:
            game_data = GameData.load(dist_dir)

        if game_data and 'model_gen' not in skip:
            console.print('Generating supported 3D models and animations')
            if 'transform' not in skip:
                native_objects = transformer.gameObjectData
                campaign = transformer.defaultGameCampaign
            else:
                native_objects = GameObjectData(
                    extracted_dir/'gameobjectdata.xml',
                    extracted_dir/'shopdata.xml',
                    extracted_dir/'gameobjectcategorydata.xml',
                )
                with (extracted_dir/'defaultGameCampaign.json').open(encoding='utf-8') as file:
                    campaign = json.load(file)
            public_identities = {
                identity
                for name in ('pony', 'pet', 'costume')
                for identity in getattr(game_data.game_objects, name).objects
            }
            model_cache = AnimationCache(raw_dir/'model-cache/animations')
            if s3_client and BUCKET:
                model_cache.load_remote(s3_client, BUCKET)
            report = generate_models(
                native_objects, extracted_dir, dist_dir, raw_dir/'model-associations.json',
                campaign=campaign, public_identities=public_identities,
                failed_rkms=failed_rkms, animation_cache=model_cache,
            )
            console.print(report['summary'])

        if game_data and 'image_gen' not in skip:
            console.print('Generating images')
            generate_images(game_data, dist_dir)

    if upload and s3_client and BUCKET:
        if success:
            sync(
                dist_folder = dist_dir,
                bucket = BUCKET,
            )

            if model_cache is not None:
                model_cache.publish_remote(dist_dir/'models_3d.json')

            s3_client.copy_object(
                Bucket = BUCKET,
                Key = GAME_VERSION_CHECKER_KEY,
                CopySource = {'Bucket': BUCKET, 'Key': GAME_VERSION_KEY},
                MetadataDirective = 'COPY',
            )

        api = API('android', version)
        latest_dlc_manifest = api.get_dlc_manifest()

        s3_client.put_object(
            Bucket = BUCKET,
            Key = GAME_VERSION_CHECKER_DLC_MANIFEST_KEY,
            Body = json.dumps(latest_dlc_manifest).encode('utf-8'),
            ContentType = 'application/json',
        )
    
    console.log(f'Time: {(perf_counter() - start_time) / 60:.2f}m')

    return success
    

def create_argparser():

    argparser = ArgumentParser()

    argparser.add_argument(
        '--env',
        dest = 'env',
        choices = ['dev', 'prod'],
        default = None,
        help = f'The environment to use. Can be set with {GAME_DATA_ENV_VAR}. Note: this is more for loading .env and different notifications.json files in development.',
    )

    command = argparser.add_subparsers(
        title = 'command',
        dest = 'command',
    )

    build = command.add_parser(
        'build',
        description = 'Build cdn',
    )

    build.add_argument(
        '-v', '--version',
        dest = 'version',
        help = 'Game version to download. If "latest" it will check if it can download the latest version, and will exit if already on the latest version.',
        default = 'latest',
    )

    build.add_argument(
        '-r', '--raw',
        dest = 'raw',
        help = 'Raw output folder',
        default = 'raw',
    )

    build.add_argument(
        '-o', '--output',
        dest = 'output',
        help = 'Output destination',
        default = 'dist',
    )

    build.add_argument(
        '--overrides',
        help = 'Path to overrides folder',
        default = 'overrides',
    )

    build.add_argument(
        '-u', '--upload',
        dest = 'upload',
        action = 'store_true',
        help = 'Upload the files to R2',
    )

    build.add_argument(
        '--skip',
        dest = 'skip',
        nargs = '+',
        choices = ['download', 'extract', 'transform', 'model_gen', 'image_gen'],
        help = 'Skip action',
        default = [],
    )

    build.add_argument(
        '--ffdec',
        dest = 'ffdec',
        help = 'Path to ffdec.jar',
        default = 'ffdec',
    )


    build.add_argument(
        '-f', '--force',
        dest = 'force',
        action = 'store_true',
        help = 'Force update and notification',
    )

    
    upload = command.add_parser(
        'upload',
        description = 'Upload a single file',
    )

    upload.add_argument(
        '-i', '--input',
        dest = 'input',
        help = 'Input file',
        required = True,
    )

    upload.add_argument(
        '-k', '--key',
        dest = 'key',
        help = 'Destination key',
    )

    argcomplete.autocomplete(argparser)

    return argparser

def main() -> bool | None:
    argparser = create_argparser()

    args = argparser.parse_args()

    if args.env:
        set_mode(prod = args.env == 'prod')
    load_env()

    if is_dev():
        console.print('[red]Currently running in dev environment[/]')
    

    match args.command:
        case 'build':
            return not build_cdn(
                raw_dir = args.raw,
                dist_dir = args.output,
                version = args.version,
                upload = args.upload,
                overrides_dir = args.overrides,
                skip = args.skip,
                ffdec = args.ffdec,
                force = args.force,
            )
    
            peak_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            console.log(f"Peak Memory Usage: {peak_memory:.2f} MB")

        case 'upload':
            BUCKET = os.environ.get('S3_BUCKET')
            
            if not BUCKET:
                raise ValueError('Cannot upload without S3_BUCKET environment variable')
            
            input: str = args.input
            key: str = args.key or os.path.basename(input)

            if not os.path.exists(input):
                raise FileNotFoundError(f'File "{input}" does not exist')
            
            content_type, _ = mimetypes.guess_type(input)

            with open(input, 'rb') as file:
                file_data = file.read()
            
            if content_type and content_type.startswith('image/'):
                image = Image.open(input)
                if not getattr(image, 'is_animated', False):
                    image = crop_image(image)
                
                    image_file = io.BytesIO()
                    image.save(image_file, format = content_type.removeprefix('image/'))
                    file_data = image_file.getvalue()
            
            console.print(f'Uploading [yellow]{key}[/]')
            
            upload_file(
                get_s3_client(BUCKET),
                BUCKET,
                file_data,
                key,
                content_type,
                override = True,
            )

            console.print('[green]Uploaded![/]')
            
