from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import html
import json
import os
from pathlib import Path
import tempfile
import time
from typing import TYPE_CHECKING

import google_play_scraper as gplay
from playstoreapi.config import config, getDevicesCodenames, getDevicesReadableNames
from playstoreapi.googleplay import GooglePlayAPI, LoginError
import requests

from game_data.data_types.GPlayTypes import GPlayAPKDetails
from luna_kit.api import Downloader, Version
from luna_kit.file_utils import PathOrBinaryFile
from luna_kit.find_ark_key import find_aes_key

from .console import console
from .data_types.GPlayTypes import GPlayAPKDetails, GPlayFile
from .s3 import get_s3_client, get_secret_s3_client
from .utils import json_dumps_compact

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client




PACKAGE_NAME = "com.gameloft.android.ANMP.GloftPOHM"
GPLAY_CONFIG_PATH = '.playstoreapi'
GPLAY_CONFIG_KEY = 'gplay/gplay_config.json'
LUNA_KIT_ARK_KEYS_KEY = 'luna_kit/ark_keys.json'



def unescape_text(s: str):
    return html.unescape(s.replace("<br>", "\n"))

@dataclass
class AppInfo:
    version: str
    release_notes: str
    raw_release_notes: str
    icon_url: str

class StoreManager:
    api: GooglePlayAPI | None
    public_s3: 'S3Client | None'
    secret_s3: 'S3Client | None'

    public_bucket: str | None
    secret_bucket: str | None

    package_name: str

    def __init__(
        self,
        public_bucket: str | None = None,
        secret_bucket: str | None = None,
        package_name: str = PACKAGE_NAME,
    ) -> None:
        self.public_bucket = public_bucket
        self.secret_bucket = secret_bucket
    
        self.public_s3 = get_s3_client(public_bucket) if public_bucket else None
        self.secret_s3 = get_secret_s3_client(secret_bucket) if secret_bucket else None

        self.package_name = package_name

        self.api = self._get_gplay_api()


    def _gplay_login_with_retry(
        self,
        api: GooglePlayAPI,
        dispenser_url: str,
        max_retries: int = 5,
        base_delay: float = 2.0,
    ) -> bool:
        # Retry to handle cold starts on free render hosting
        
        last_exc = None
        for attempt in range(max_retries):
            try:
                api.login(anonymous = True, tokenDispenser = dispenser_url)
                return True
            except LoginError as e:
                last_exc = e
                if '502' not in str(e):
                    raise  # not a transient dispenser-cold-start error, bail immediately

                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    console.print(f'[yellow]Dispenser returned 502, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})[/]')
                    time.sleep(delay)

        if last_exc is not None:
            raise last_exc
        return False

    def _get_gplay_api(self):
        api = GooglePlayAPI('en_US', 'UTC', device_codename = 'gplayapi_px_9a')

        config: dict | None = None

        if os.path.exists(GPLAY_CONFIG_PATH):
            with open(GPLAY_CONFIG_PATH, 'r') as file:
                config = json.load(file)

        elif self.secret_s3 is not None and self.secret_bucket:
            try:
                config_object = self.secret_s3.get_object(
                    Bucket = self.secret_bucket,
                    Key = GPLAY_CONFIG_KEY,
                )

                config = json.load(config_object['Body'])
            except:
                console.print('Cannot get google play config')
        
        dispenser_url = os.environ.get('PLAYSTORE_DISPENSER_URL')
        
        try:
            logged_in = False
            if config:
                try:
                    api.login(
                        gsfId = config['gsfId'],
                        authSubToken = config['authSubToken'],
                        check = True,
                        deviceCheckinConsistencyToken = config['deviceCheckinConsistencyToken'],
                        deviceConfigToken = config['deviceConfigToken'],
                        dfeCookie = config['dfeCookie'],
                    )
                    logged_in = True
                except:
                    pass

            if not logged_in and dispenser_url:
                self._gplay_login_with_retry(api, dispenser_url)
            elif not api.gsfId:
                console.print('Cannot log into google play')
                return

            console.print('Logged into google play')
            
                
        except LoginError as e:
            if not dispenser_url:
                console.print('[red]Cannot use google play api[/]')
                return
            
            e.add_note(f'Dispenser: {dispenser_url}')

            raise
        
        config = {
            "authSubToken": api.authSubToken,
            "gsfId": api.gsfId,
            "deviceCheckinConsistencyToken": api.deviceCheckinConsistencyToken,
            "deviceConfigToken": api.deviceConfigToken,
            "dfeCookie": api.dfeCookie,
        }

        config_data = json_dumps_compact(config)
        with open(GPLAY_CONFIG_PATH, 'w', encoding = 'utf-8') as file:
            file.write(config_data)
        
        if self.secret_s3 is not None and self.secret_bucket:
            console.print('Saving gplay config to s3')
            self.secret_s3.put_object(
                Bucket = self.secret_bucket,
                Key = GPLAY_CONFIG_KEY,
                Body = config_data.encode('utf-8'),
                ContentType = 'application/json',
            )
        else:
            console.print('Cannot save gplay config to s3', self.secret_s3, self.secret_bucket)
        
        return api

    def get_gplay_api_details(self):

        if not self.api:
            return
        

        details = self.api.details(self.package_name)
        icon_url: str = ''

        for image in details['image']:
            if image.get('imageType') == 4:
                if image.get('imageUrl'):
                    icon_url = image['imageUrl']
                    break

        return AppInfo(
            version = details['details']['appDetails']['versionString'],
            raw_release_notes = details['details']['appDetails']['recentChangesHtml'],
            release_notes = unescape_text(details['details']['appDetails']['recentChangesHtml']),
            icon_url = icon_url,
        )

    def get_gplay_scrape_details(self):
        app_info = gplay.app(self.package_name)

        return AppInfo(
            version = app_info['version'],
            raw_release_notes = app_info['recentChanges'],
            release_notes = app_info['recentChangesHTML'],
            icon_url = app_info['icon'],
        )

    def get_apkmirror_details(self):
        response = requests.post(
            f"https://www.apkmirror.com/wp-json/apkm/v1/app_exists?pnames={self.package_name}",
            headers = {
                "User-Agent": "APKUpdater-v3.0.3",
                # This is a key from APKUpdater https://github.com/rumboalla/apkupdater/issues/58#issuecomment-309238684
                "Authorization": "Basic YXBpLWFwa3VwZGF0ZXI6cm01cmNmcnVVakt5MDRzTXB5TVBKWFc4"
            }
        )
        response.raise_for_status()

        raw_app_info = response.json()
        return AppInfo(
            version = raw_app_info['data'][0]['release']['version'],
            raw_release_notes = raw_app_info['data'][0]['release']['whats_new'],
            release_notes = unescape_text(raw_app_info['data'][0]['release']['whats_new']),
            icon_url = raw_app_info['data'][0]['app']['icon_url'],
        )


    def get_app_info(self) -> AppInfo:
        with ThreadPoolExecutor(max_workers = 3) as threader:
            futures = [
                threader.submit(self.get_gplay_api_details),
                threader.submit(self.get_gplay_scrape_details),
                # threader.submit(self.get_apkmirror_details),
            ]

            infos = filter(lambda details: details is not None, [future.result() for future in futures])

        return sorted(
            infos,
            key = lambda details: Version.parse(details.version),
            reverse = True,
        )[0]

    # apk stuff

    def download_apk(self, file: GPlayFile, output: PathOrBinaryFile):
        response = requests.get(file['url'], cookies = file['cookies'], headers = {}, stream = True)
        downloader = Downloader(response, output)
        return downloader.full_download(console = console)
    
    def get_apk_details(self, version_code: int | None = None) -> GPlayAPKDetails:
        if self.api is None:
            raise ValueError('Not logged in')
        
        details: GPlayAPKDetails = self.api.download(self.package_name, versionCode = version_code) # type: ignore
        return details
    
    def find_lib_apk(self, apk_details: GPlayAPKDetails):
        ARCHITECTURES = [
            "arm64_v8a",
            "armeabi_v7a",
            "x86",
            "x86_64",
        ]

        lib_file: GPlayFile | None = None
        used_arch: str | None = None
        
        for split in apk_details['splits']:
            name = split['name']
            arch = name.removeprefix('config.')

            if arch not in ARCHITECTURES:
                continue

            if used_arch is None or ARCHITECTURES.index(arch) < ARCHITECTURES.index(used_arch):
                used_arch = arch
                lib_file = split['file']
        
        return lib_file

    def get_aes_key(self, apk_file: PathOrBinaryFile, version: Version):
        try:
            key = find_aes_key(apk_file)
        except Exception as e:
            console.print_exception()
            return
        
        if self.public_s3 is not None and self.public_bucket:
            ark_keys: dict[str, str] = {}
            try:
                ark_keys = json.load(self.public_s3.get_object(
                    Bucket = self.public_bucket,
                    Key = LUNA_KIT_ARK_KEYS_KEY,
                )['Body'])
            except Exception as e:
                e.add_note('Failed to get aes keys')
                console.print_exception()
            
            key_hex = key.hex()
            if key_hex not in list(ark_keys.values()):
                ark_keys[str(version)] = key_hex

                try:
                    self.public_s3.put_object(
                        Bucket = self.public_bucket,
                        Key = LUNA_KIT_ARK_KEYS_KEY,
                        Body = json.dumps(ark_keys, indent = 2, ensure_ascii = False).encode('utf-8'),
                        ContentType = 'application/json',
                    )
                except Exception as e:
                    e.add_note('Failed to upload keys')
                    console.print_exception()

        return key
    
    def get_stored_aes_key(self, version: Version) -> bytes | None:
        """
        Gets the aes key of a version from the stored keys, if it has been stored before

        Args:
            version (Version): Version to get the key for
        """

        if self.public_s3 is None or not self.public_bucket:
            return None

        try:
            ark_keys: dict[str, str] = json.load(self.public_s3.get_object(
                Bucket = self.public_bucket,
                Key = LUNA_KIT_ARK_KEYS_KEY,
            )['Body'])
        except Exception:
            return None

        key_hex = ark_keys.get(str(version))

        if not key_hex:
            return None

        return bytes.fromhex(key_hex)

    def fetch_aes_key(self, version: Version) -> bytes | None:
        """
        Downloads lib apk, extracts aes key, and uploads key to rucket

        Args:
            version (Version): Version this is for
        """

        with tempfile.TemporaryDirectory() as tempdir:
            dirpath = Path(tempdir)
            apk_path = dirpath/'app.apk'

            console.print('Fetching apk details')
            details = self.get_apk_details()
            lib_info = self.find_lib_apk(details)
            if lib_info is None:
                console.print('[red]Could not find lib apk, trying main apk[/]')
                lib_info = details['file']

            console.print('Downloading apk')
            self.download_apk(lib_info, apk_path)
            
            return self.get_aes_key(apk_path, version)
