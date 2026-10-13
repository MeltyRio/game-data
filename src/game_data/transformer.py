from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from glob import glob
import json
import math
import os
from pathlib import Path
import re
from typing import Literal, TypedDict
import urllib.parse

from PIL import Image
from lxml import etree
from rich.progress import Progress

from luna_kit.cinematictable import CinematicTable
from luna_kit.gameobjectdata import GameObjectData, ShopItem
from luna_kit.loc import LOC
from luna_kit.pvr import PVR
from luna_kit.questtable import QuestManager, QuestTable
from luna_kit.swf import SWF
from luna_kit.typings import (
    ArenaQTESettingsType,
    DefaultGameCampaignType,
    FusionData,
    GroupQuestsType,
    PonyTasksType,
    PrizeTypes,
)
from luna_kit.typings.defaultGameCampaign import MazeData as GameData_MazeData
from luna_kit.xml import parse_xml

from .console import console, track, COLUMNS
from .crop import crop_image
from .data_types import GameData
from .data_types import GameDataTypes
from .data_types import QuestDataTypes
from .data_types import CinematicTypes
from .data_types import MazeDataTypes
from .data_types.GameDataTypes import *
from .data_types.common_types import LANGUAGES
from .utils import strToBool, strToInt


class ObjectOverride(TypedDict):
    preferred_name: TranslatableString
    alt_name: AltName
    tags: list[str]
    wiki_path: str

type ObjectOverrides = dict[CategoryName, dict[GameObjectId, ObjectOverride]]


NPC_PONIES = [
    "Pony_Derpy", # derpy box, not playable muffins
    'Pony_Disguised_Spike',
    "Pony_Chest",
    'Pony_Tirek', # Not the playable tirek
    'Pony_Tirek_TOTB',
    'Pony_Windigo', # unobtainable
]

QUEST_PONIES = [
    'Pony_Quest_Duplicate_Starlight',
    'Pony_Quest_Duplicate_Discord',
    'Pony_Quest_Duplicate_Trixie',
    'Pony_Quest_Duplicate_Thorax',
    'Pony_Quest_Fluttershy_Duplicate',
    'Pony_Quest_Duplicate_Scootaloo',
    'Pony_Quest_Duplicate_Sweetiebelle',
    'Pony_Quest_Duplicate_Apple_Bloom',
    'Pony_Quest_Changeling_Runaway_01',
    'Pony_Quest_Changeling_Runaway_02',
]

UNUSED_PONIES = [
    'Pony_Twilight_Sneak_Le',
    'Pony_Camo_Dash',
    'Pony_Wingless_Rainbow_Dash',
    'Pony_Crystal_Luna_Hair_Test',
    'Pony_Token_Test',
]

LOCATIONS: dict[int, Location] = {
    0: 'PONYVILLE',
    1: 'CANTERLOT',
    2: 'SWEET_APPLE_ACRES',
    3: 'EVERFREE_FOREST',
    4: 'CRYSTAL_EMPIRE',
    5: 'CHANGELING_KINGDOM',
    6: 'KLUGETOWN',
    7: 'KLUGETOWN',
    8: 'MAZE',
}

CURRENCY = {
    1: 'Bits',
    2: 'Gems',
}


def encode_wiki_path(name: str) -> str:
    return urllib.parse.quote(name.replace(' ', '_'))


def find_in_sprite(sprite: str | Path, frame: str):
    with open(sprite, 'r', encoding = 'utf-8') as file:
        sprite_content = file.read()
    
    match = re.search(
        fr'FRAME\s+"{re.escape(frame)}"(\s*\/\/.*)?\s*{'{'}[\S\s]*?FM\s+(0x[\da-zA-Z]+)',
        sprite_content,
        re.MULTILINE,
    )

    if match is None:
        console.print('pattern', fr'FRAME\s+"{re.escape(frame)}"(\s*\/\/.*)?\s*{'{'}[\S\s]*?FM\s+(0x[\da-zA-Z]+)')
        console.print('could not find frame')
        return
    
    module_id = match.group(2)

    match = re.search(
        fr'MD\s+{re.escape(module_id)}\s+MD_IMAGE\s+(?P<image>\d+)\s+(?P<x>\d+)\s+(?P<y>\d+)\s+(?P<w>\d+)\s+(?P<h>\d+)',
        sprite_content,
    )

    if match is None:
        console.print('could not find module')
        return
    
    image_id = int(match.groupdict()['image'])

    match = re.search(
        rf'(?<=IMAGE 0x{image_id:04x} ").*(?=")',
        sprite_content,
    )

    if match is None:
        console.print('could not find image')
        return
    
    return match.group()



def _transform_image(src: str | Path, dest: str | Path):
    src = Path(src)
    
    if src.suffix == '.pvr':
        image = PVR(src).image
    else:
        image = Image.open(src)
    
    if image is not None:
        image = crop_image(image)
        image.save(dest)
    else:
        console.print(f'[red]Could not load {src}[/]')


def swf2webp(input: str | Path, output: str | Path, *, ffdec_path: str = 'ffdec'):
    input = Path(input)
    console.print(f'[yellow]{input.name}[/yellow]: Loading')
    swf = SWF(input, ffdec = ffdec_path)
    console.print(f'[yellow]{input.name}[/yellow]: Fixing')
    swf.fix()
    console.print(f'[yellow]{input.name}[/yellow]: Rendering')
    swf.render_webp(output)
    console.print(f'[yellow]{input.name}[/yellow]: Saved')


class Transformer:
    game_data: GameData
    
    def __init__(
        self,
        game_folder: str | Path,
        output_folder: str | Path,
        override_folder: str | Path,
        version: str,
        *,
        ffdec: str = 'ffdec',
    ) -> None:
        self.game_folder = Path(game_folder)
        self.output_folder = Path(output_folder)
        self.images_folder = self.output_folder/'images'
        self.game_objects_folder = self.images_folder/'game_objects'
        self.override_folder = Path(override_folder)

        self.output_folder.mkdir(parents = True, exist_ok = True)
        self.images_folder.mkdir(parents = True, exist_ok = True)
        self.game_objects_folder.mkdir(parents = True, exist_ok = True)

        self.ffdec = ffdec

        self.gameObjectData = GameObjectData(
            self.game_folder/'gameobjectdata.xml',
            self.game_folder/'shopdata.xml',
            self.game_folder/'gameobjectcategorydata.xml',
        )

        with open(
            self.game_folder/'defaultGameCampaign.json',
            'r',
            encoding = 'utf-8',
        ) as file:
            self.defaultGameCampaign: DefaultGameCampaignType = json.load(file)
        
        self.locs: dict[Language, LOC] = {(loc := LOC(str(filename)))['DEV_ID'].lower().strip(): loc for filename in self.game_folder.glob('*.loc')}

        self.daily_goals_shop = {
            item['item_id']: item['cost']
            for item in self.defaultGameCampaign.get('mini_games', {}).get('dailygoals', {}).get('itemshop', [])
        }

        self.quest_manager = QuestManager(self.game_folder/'questmanager.xml')
        
        content_version: str = parse_xml(self.game_folder/'data_ver.xml')[0].get('Value', '')
        
        self.game_data = GameData()
        self.game_data.game_version.game_version = version
        self.game_data.game_version.content_version = content_version

        self.pony_houses: dict[str, list[str]] = {}

        self._image_executor = ThreadPoolExecutor()
        self._image_futures: list[Future] = []

    def start(self):
        self.get_game_data()

    def save(self):
        console.log('Saving files')
        self.game_data.save(self.output_folder)
    
    def get_game_data(self):
        try:
            self.get_category_pony()
            self.get_category_consumable()
            self.get_category_house()
            self.get_category_decor()
            self.get_category_avatar()
            self.get_category_avatar_frame()
            self.get_category_background()
            self.get_category_background_frame()
            self.get_category_cutie_mark()
            self.get_category_pet()
            self.get_category_theme()
            self.get_category_path()
            self.get_category_item()
            self.get_category_booster()
            self.get_tasks()
            self.get_category_token()
            self.get_category_costume()
            self.get_category_costume_part()

            self.get_group_quests()
            self.get_fortune_shop()
            self.get_collections()
            self.get_maze_data()

            self.get_quest_data()
            self.get_dialogue()

            self.apply_overrides()

            self.wait_for_images()
        except:
            self._image_executor.shutdown(wait = True, cancel_futures = True)
            raise
        finally:
            self._image_executor.shutdown(wait = True)
    
    def wait_for_images(self):
        if len(self._image_futures) == 0:
            return
        
        for future in track(self._image_futures, description = 'Finishing image processing...'):
            future.result()
        
        self._image_futures.clear()
    
    def apply_overrides(self):
        if not (self.override_folder/'game_objects.json').exists():
            return
        
        with open(self.override_folder/'game_objects.json', 'r', encoding = 'utf-8') as file:
            object_overrides: ObjectOverrides = json.load(file)
        
        console.print('Applying object overrides')

        for category in CATEGORY_NAMES:
            if category not in object_overrides:
                continue

            objects: dict[GameObjectId, GenericObjectType] = getattr(self.game_data.game_objects, category).objects

            for id, overrides in object_overrides[category].items():
                if id not in objects:
                    continue

                for attr, value in overrides.items():
                    if hasattr(objects[id], attr):
                        setattr(objects[id], attr, value)
    
    def get_category_pony(self):
        ponies = self.game_data.game_objects.pony.objects

        
        for hidden_pony in self.gameObjectData['HiddenPony'].values():
            pony_id = hidden_pony.get('Parent', {}).get('PonyName')
            if pony_id and pony_id not in NPC_PONIES:
                NPC_PONIES.append(pony_id)
            
        main_images = self.game_objects_folder/'pony/main'
        portrait_images = self.game_objects_folder/'pony/portrait'
        main_images.mkdir(parents = True, exist_ok = True)
        portrait_images.mkdir(parents = True, exist_ok = True)

        groups = {}

        for index, pony in enumerate(track(
            self.gameObjectData['Pony'].values(),
            description = 'Getting ponies...',
        )):
            try:
                shop_data = self.gameObjectData.get_object_shopdata(pony.id)
                pony_info = PonyType.model_construct(
                    index = index,
                    id = pony.id,
                    name = self.translate_string(pony.get('Name', {}).get('Unlocal', pony.id)),
                    description = self.translate_string(pony.get('Description', {}).get('Unlocal', '')),
                    image = {},
                    alt_name = {},
                    preferred_name = {},
                )

                ponies[pony.id] = pony_info


                if pony.id in UNUSED_PONIES and 'unused' not in pony_info.tags:
                    pony_info.tags.append('unused')
                if pony.id in NPC_PONIES and 'npc' not in pony_info.tags:
                    pony_info.tags.append('npc')
                if pony.id in QUEST_PONIES and 'quest' not in pony_info.tags:
                    pony_info.tags.append('quest')

                # images

                pony_info.image['portrait'] = self.add_image(
                    [pony.get('Icon', {}).get('Url', '')],
                    portrait_images/(pony.id + '.png'),
                )

                pony_info.image['main'] = self.add_image(
                    [pony.get('Shop', {}).get('Icon', '')],
                    main_images/(pony.id + '.png'),
                )


                # General stuff

                location: int = -1
                if shop_data is not None:
                    shopdata_location: str = shop_data.get('MapZone', '-1')
                    shopdata_location = shopdata_location.split(',')[0]
                    location = strToInt(shopdata_location)
                else:
                    location = pony.get('House', {}).get('HomeMapZone', -1)

                pony_info.location = LOCATIONS.get(
                    location, # type: ignore
                    'UNKNOWN',
                )
                pony_info.house = pony.get('House', {}).get('Type')
                if isinstance(pony_info.house, str):
                    self.pony_houses.setdefault(pony_info.house, []).append(pony.id)

                
                changeling = pony.get('IsChangelingWithSet', {}).get('AltPony', None)
                if changeling:
                    pony_info.changeling.id = changeling
                    pony_info.changeling.IAmAlterSet = pony.get('IsChangelingWithSet', {}).get('IAmAlterSet', False)

                group: list[str] = pony.get('Friends', {}).get('Friend', [])
                group = list(filter(lambda id: id != '', group))
                if len(group):
                    group.insert(0, pony.id)
                    pony_info.group_master = True
                
                for id in group:
                    groups[id] = group
                
                pony_info.group = group

                pony_info.ai_type = pony.get('AI', {}).get('Special_AI', 0)
                pony_info.max_level = pony.get('AI', {}).get('Max_Level', False)

                for prize_id, amount in zip(
                    pony.get('StarRewards', {}).get('ID', []),
                    pony.get('StarRewards', {}).get('Amount', []),
                ):
                    pony_info.rewards.append(StarReward.model_construct(
                        item = prize_id,
                        amount = amount,
                    ))
                
                # extra metadata

                
                pony_info.minigame.can_play_minecart = pony.get('Minigames', {}).get('CanPlayMineCart', True)
                pony_info.minigame.hard_lock = pony.get('MGHardLock', {}).get('Lock', False)
                pony_info.minigame.cooldown = pony.get('Minigames', {}).get('TimeBetweenPlayActions', 0)
                pony_info.minigame.skip_cost = pony.get('Minigames', {}).get('PlayActionSkipAgainCost', 0)
                pony_info.minigame.exp_rank = pony.get('Minigames', {}).get('EXP_Rank', 0) # Not sure what this is, but I'll keep it
                pony_info.minigame.has_wings = not pony.get('Minigames', {}).get('NoWings', True)

                pony_info.arrival_xp = pony.get('OnArrive', {}).get('EarnXP', 0)
                pony_info.not_pony = pony.get('Misc', {}).get('IsNotPony', False)
                pony_info.ban_pets = pony.get('Misc', {}).get('BenPets', False)

                pony_info.costumes = [costume for costume in pony.get('Sets', {}).get('Sets', []) if costume]

                if shop_data:
                    pony_info.unlock_level = shop_data.get('UnlockValue', 0)
                    pony_info.price = self.get_price(shop_data)
                
                pony_info.wiki_path = encode_wiki_path(pony_info.name['english'])


            except Exception as e:
                e.add_note(f'Pony: {pony.id}')
                console.print_exception()
        
        for pony_id, group in groups.items():
            ponies[pony_id].group = group

    def get_category_house(self):
        houses = self.game_data.game_objects.house.objects
        shops = self.game_data.game_objects.shop.objects

        house_images = self.game_objects_folder/'house/main'
        shop_images = self.game_objects_folder/'shop/main'
        house_images.mkdir(parents = True, exist_ok = True)
        shop_images.mkdir(parents = True, exist_ok = True)

        index: dict[Literal['house', 'shop'], int] = {
            'house': 0,
            'shop': 0,
        }

        for house in track(
            self.gameObjectData['Pony_House'].values(),
            description = 'Getting houses...',
        ):
            try:
                is_shop = house.get('ShopModule', {}).get('IsAShop', False)

                special = house.get('Special', {})
                if special.get('IsALotto') or special.get('IsCKBossEntrance') or special.get('IsSpinningWheel'):
                    is_shop = True

                house_info: HouseType | ShopType
                shop_data = self.gameObjectData.get_object_shopdata(house.id)

                house_class = ShopType if is_shop else HouseType

                house_info = house_class.model_construct(
                    index = index['shop' if is_shop else 'house'],
                    id = house.id,
                    name = self.translate_string(house.get('Name', {}).get('Unlocal', house.id)),
                )
                index[house_info.category] += 1

                if isinstance(house_info, ShopType):
                    shops[house.id] = house_info
                else:
                    houses[house.id] = house_info

                house_info.image['main'] = self.add_image(
                    [
                        house.get('Icon', {}).get('BookIcon', ''),
                        house.get('Shop', {}).get('Icon', ''),
                    ],
                    (shop_images if is_shop else house_images)/f'{house.id}.png',
                )

                residents = self.pony_houses.get(house.id, [])
                house_info.residents = residents

                location: Location = 'UNKNOWN' if shop_data is None else LOCATIONS.get(
                    strToInt(shop_data.get('MapZone', -1).split(',')[0]),
                    'UNKNOWN',
                )

                house_info.grid_size = house.get('GridData', {}).get('Size', 0) // 2
                house_info.build.time = house.get('Construction', {}).get('ConstructionTime', 0)
                house_info.build.skip_cost = house.get('Construction', {}).get('SkipCost', 0)
                house_info.build.xp = house.get('XP', {}).get('OnConstructionComplete', 0) + house.get('XP', {}).get('OnConstructionStarted', 0)

                visitors = [visitor for visitor in house.get('Visitors', {}).get('Ponies', []) if visitor]

                for visitor in visitors:
                    if visitor not in self.game_data.game_objects.pony.objects:
                        if visitor:
                            console.log(f'house {house.id} has nonexistent visitor "{visitor}"')
                        continue
                    
                    inns = self.game_data.game_objects.pony.objects[visitor].inns
                    if house.id not in inns:
                        inns.append(house.id)
                
                house_info.visitors = visitors

                if isinstance(house_info, HouseType):
                    if len(house_info.residents):
                        for resident in house_info.residents:
                            pony = self.game_data.game_objects.pony.objects[resident]
                            if pony.location not in house_info.location:
                                house_info.location.append(pony.location)
                    else:
                        house_info.location.append(location)
                else:
                    house_info.location = location
                
                if isinstance(house_info, ShopType):
                    if special.get('IsALotto'):
                        house_info.special = 'lotto'
                    elif special.get('IsCKBossEntrance'):
                        house_info.special = 'ck_entrance'
                    elif special.get('IsSpinningWheel'):
                        house_info.special = 'ferris_wheel'
                    else:
                        house_info.product = house.get('ShopModule', {}).get('Consumable_A', '')
                        
                        consumable = self.game_data.game_objects.consumable.objects.get(house_info.product)
                        if consumable is not None and consumable.critter is not None:
                            critter = self.game_data.game_objects.pony.objects.get(consumable.critter.critter)
                            if critter is not None:
                                critter.critter_farm = house.id
                                self.game_data.game_objects.pony.objects[critter.changeling.id].critter_farm = house.id
                    
                    house_info.can_sell = house.get('Sell', {}).get('CanSell', False)

                    if shop_data:
                        house_info.unlock_level = shop_data.get('UnlockValue', 0)
                        house_info.price = self.get_price(shop_data)

                house_info.wiki_path = encode_wiki_path(house_info.name['english'])

            
            except Exception as e:
                e.add_note(f'House: {house.id}')
                console.print_exception()

    def get_category_decor(self):
        decors = self.game_data.game_objects.decor.objects
        
        decor_images = self.game_objects_folder/'decor/main'
        decor_images.mkdir(parents = True, exist_ok = True)
        
        for index, decor in enumerate(track(
            self.gameObjectData['Decore'].values(),
            description = 'Getting decor...',
        )):
            try:
                shop_data = self.gameObjectData.get_object_shopdata(decor.id)
                decor_info = DecorType.model_construct(
                    index = index,
                    id = decor.id,
                    name = self.translate_string(decor.get('Name', {}).get('Unlocal', decor.id)),
                    image = {},
                )
                
                decors[decor.id] = decor_info
                
                decor_info.image['main'] = self.add_image(
                    [decor.get('Shop', {}).get('Icon', '')],
                    decor_images/f'{decor.id}.png',
                )

                if shop_data is not None:
                    decor_info.location = LOCATIONS.get(
                        strToInt(shop_data.get('MapZone', -1).split(',')[0]),
                        'UNKNOWN',
                    )
                
                decor_info.limit = decor.get('Shop', {}).get('PurchaseLimit', 0)
                decor_info.grid_size = decor.get('GridData', {}).get('Size', 0) // 2
                decor_info.xp = decor.get('OnPurchase', {}).get('EarnXP', 0)

                decor_info.pro.is_pro = decor.get('ProDecoration', {}).get('IsProDecore', False)
                decor_info.pro.size = decor.get('ProDecoration', {}).get('GridSizeBonus', 0)
                decor_info.pro.time = decor.get('ProDecoration', {}).get('TimeBonusPercent', 0)
                decor_info.pro.bits = decor.get('ProDecoration', {}).get('BitsBonusPercent', 0)

                if shop_data:
                    decor_info.unlock_level = shop_data.get('UnlockValue', 0)
                    decor_info.price = self.get_price(shop_data)
                
            except Exception as e:
                e.add_note(f'Decor: {decor.id}')
                console.print_exception()
        
        with open(self.game_folder/'decoration_fusion_val.json', 'r') as file:
            fusion_data: FusionData = json.load(file)
        
        for decor in fusion_data['DecoreList']:
            if not decor['id'] in decors:
                console.print(f'Decor {decor["id"]} not found')
                continue
            decors[decor['id']].fusion_points = decor['val']

    def get_category_avatar(self):
        avatars = self.game_data.game_objects.avatar.objects
        
        main_images = self.game_objects_folder/'avatar/main'
        preview_images = self.game_objects_folder/'avatar/preview'
        main_images.mkdir(parents = True, exist_ok = True)
        preview_images.mkdir(parents = True, exist_ok = True)
        
        for index, avatar in enumerate(track(
            self.gameObjectData['ProfileAvatar'].values(),
            description = 'Getting avatars...',
        )):
            try:
                avatar_info = AvatarType.model_construct(
                    index = index,
                    id = avatar.id,
                    name = self.translate_string(avatar.get('Shop', {}).get('Label', avatar.id)),
                    image = {},
                )
                
                avatars[avatar.id] = avatar_info

                main_path: str = avatar.get('Settings', {}).get('PictureActive', '')
                avatar_info.is_default = avatar.get('Settings', {}).get('IsDefault', False)
                avatar_info.pony = avatar.get('Settings', {}).get('PonyStarsID', '')

                avatar_info.animated = main_path.endswith('.swf')

                if avatar_info.animated:
                    avatar_info.image['main'] = self.add_animated_image(
                        f'swf/{main_path}',
                        main_images/f'{avatar.id}.webp'
                    )
                else:
                    avatar_info.image['main'] = self.add_image(
                        [main_path],
                        main_images/f'{avatar.id}.png',
                    )
                
                avatar_info.image['preview'] = self.add_image(
                    [avatar.get('Shop', {}).get('Icon', '')],
                    preview_images/f'{avatar.id}.png',
                )

                shop_data = self.gameObjectData.get_object_shopdata(avatar.id)
                if shop_data:
                    avatar_info.price = self.get_price(shop_data)
                
                
            except Exception as e:
                e.add_note(f'Avatar: {avatar.id}')
                console.print_exception()

    def get_category_avatar_frame(self):
        avatar_frames = self.game_data.game_objects.avatar_frame.objects
        
        main_images = self.game_objects_folder/'avatar_frame/main'
        preview_images = self.game_objects_folder/'avatar_frame/preview'
        main_images.mkdir(parents = True, exist_ok = True)
        preview_images.mkdir(parents = True, exist_ok = True)
        
        for index, frame in enumerate(track(
            self.gameObjectData['ProfileAvatarFrame'].values(),
            description = 'Getting frames...',
        )):
            try:
                frame_info = AvatarFrameType.model_construct(
                    index = index,
                    id = frame.id,
                    name = self.translate_string(frame.get('Shop', {}).get('Label', frame.id)),
                    image = {},
                )
                
                avatar_frames[frame.id] = frame_info

                main_path: str = frame.get('Settings', {}).get('PictureActive', '')
                frame_info.is_default = frame.get('Settings', {}).get('IsDefault', False)

                frame_info.animated = main_path.endswith('.swf')

                if frame_info.animated:
                    frame_info.image['main'] = self.add_animated_image(
                        f'swf/{main_path}',
                        main_images/f'{frame.id}.webp'
                    )
                    frame_info.image['preview'] = self.add_image(
                        [frame.get('Shop', {}).get('Icon', '')],
                        preview_images/f'{frame.id}.png',
                    )
                else:
                    frame_info.image['main'] = self.add_image(
                        [main_path],
                        main_images/f'{frame.id}.png',
                    )
                    frame_info.image['preview'] = frame_info.image['main']
                
                shop_data = self.gameObjectData.get_object_shopdata(frame.id)
                if shop_data:
                    frame_info.price = self.get_price(shop_data)
                
                
            except Exception as e:
                e.add_note(f'Avatar frame: {frame.id}')
                console.print_exception()

    def get_category_background(self):
        backgrounds = self.game_data.game_objects.background.objects
        
        main_images = self.game_objects_folder/'background/main'
        preview_images = self.game_objects_folder/'background/preview'
        main_images.mkdir(parents = True, exist_ok = True)
        preview_images.mkdir(parents = True, exist_ok = True)
        
        for index, background in enumerate(track(
            self.gameObjectData['PlayerCardBackground'].values(),
            description = 'Getting backgrounds...',
        )):
            try:
                background_info = BackgroundType(
                    index = index,
                    id = background.id,
                    name = self.translate_string(background.get('Shop', {}).get('Label', background.id)),
                    image = {},
                )
                
                backgrounds[background.id] = background_info

                background_info.is_default = background.get('Settings', {}).get('IsDefault', False)

                background_image = background.get('Settings', {}).get('BackgroundImage', '')
                background_sprite = f'{background_image}.sprite'
                background_sprite_frame = background.get('Settings', {}).get('BackgroundImageFrame', '')
                if os.path.exists(self.game_folder/background_sprite):
                    background_image = find_in_sprite(self.game_folder/background_sprite, background_sprite_frame) or background_image
                

                background_info.image['main'] = self.add_image(
                    [background_image],
                    main_images/f'{background.id}.png',
                )
                background_info.image['preview'] = self.add_image(
                    [background.get('Settings', {}).get('PictureActive', '')],
                    preview_images/f'{background.id}.png',
                )

                shop_data = self.gameObjectData.get_object_shopdata(background.id)
                if shop_data:
                    background_info.price = self.get_price(shop_data)
                
            except Exception as e:
                e.add_note(f'Background: {background.id}')
                console.print_exception()

    def get_category_background_frame(self):
        background_frames = self.game_data.game_objects.background_frame.objects
        
        main_images = self.game_objects_folder/'background_frame/main'
        main_images.mkdir(parents = True, exist_ok = True)
        
        for index, frame in enumerate(track(
            self.gameObjectData['PlayerCardBackgroundFrame'].values(),
            description = 'Getting background frames...',
        )):
            try:
                frame_info = BackgroundFrameType.model_construct(
                    index = index,
                    id = frame.id,
                    name = self.translate_string(frame.get('Shop', {}).get('Label', frame.id)),
                    image = {
                        'main': self.add_image(
                            [frame.get('Settings', {}).get('PictureActive', '')],
                            main_images/f'{frame.id}.png',
                        )
                    },
                    is_default = frame.get('Settings', {}).get('IsDefault', False),
                )
                
                background_frames[frame.id] = frame_info

                shop_data = self.gameObjectData.get_object_shopdata(frame.id)
                if shop_data:
                    frame_info.price = self.get_price(shop_data)
                
            except Exception as e:
                e.add_note(f'Background frame: {frame.id}')
                console.print_exception()
    
    def get_category_cutie_mark(self):
        cutie_marks = self.game_data.game_objects.cutie_mark.objects
        
        main_images = self.game_objects_folder/'cutie_mark/main'
        main_images.mkdir(parents = True, exist_ok = True)
        
        for index, cutie_mark in enumerate(track(
            self.gameObjectData['PlayerCardCutieMark'].values(),
            description = 'Getting cutie marks...',
        )):
            try:
                cutie_mark_info = CutieMarkType.model_construct(
                    index = index,
                    id = cutie_mark.id,
                    name = self.translate_string(cutie_mark.get('Shop', {}).get('Label', cutie_mark.id)),
                    image = {
                        'main': self.add_image(
                            [cutie_mark.get('Settings', {}).get('PictureActive', '')],
                            main_images/f'{cutie_mark.id}.png',
                        )
                    },
                    is_default = cutie_mark.get('Settings', {}).get('IsDefault', False),
                    pony = cutie_mark.get('Settings', {}).get('PonyStarsID', ''),
                    is_reward = bool(cutie_mark.get('Settings', {}).get('PonyStarsID', '')),
                )

                cutie_marks[cutie_mark.id] = cutie_mark_info

                shop_data = self.gameObjectData.get_object_shopdata(cutie_mark.id)
                if shop_data:
                    cutie_mark_info.price = self.get_price(shop_data)
                
            except Exception as e:
                e.add_note(f'Cutie mark: {cutie_mark.id}')
                console.print_exception()

    def get_category_pet(self):
        pets = self.game_data.game_objects.pet.objects
        
        pet_images = self.game_objects_folder/'pet/main'
        pet_images.mkdir(parents = True, exist_ok = True)
        
        for index, pet in enumerate(track(
            self.gameObjectData['PonyPet'].values(),
            description = 'Getting pets...',
        )):
            try:
                pet_info = PetType.model_construct(
                    index = index,
                    id = pet.id,
                    name = self.translate_string(pet.get('Name', {}).get('Unlocal', pet.id)),
                    image = {},
                )
                
                pets[pet.id] = pet_info
                
                pet_info.image['main'] = self.add_image(
                    [pet.get('Shop', {}).get('Icon', '')],
                    pet_images/f'{pet.id}.png',
                )

                pet_info.pony = pet.get('Settings', {}).get('PonyUniqueID', '')
                pet_info.flying = pet.get('Settings', {}).get('IsFlying', False)
                pet_info.minecart_bonus = pet.get('Settings', {}).get('GameBonus', 0)
                pet_info.task_bonus = pet.get('Settings', {}).get('TaskBonus', 0)
                
                shop_data = self.gameObjectData.get_object_shopdata(pet.id)
                if shop_data:
                    pet_info.price = self.get_price(shop_data)
                
                
            except Exception as e:
                e.add_note(f'Pet: {pet.id}')
                console.print_exception()

    def get_category_theme(self):
        themes = self.game_data.game_objects.theme.objects
        
        theme_images = self.game_objects_folder/'theme/main'
        theme_images.mkdir(parents = True, exist_ok = True)
        
        for index, theme in enumerate(track(
            self.gameObjectData['Theme'].values(),
            description = 'Getting themes...',
        )):
            try:
                theme_info = ThemeType.model_construct(
                    index = index,
                    id = theme.id,
                    name = self.translate_string(theme.get('Appearance', {}).get('Name', theme.id)),
                    image = {},
                )
                
                themes[theme.id] = theme_info
                
                theme_info.season = theme.get('Appearance', {}).get('Season', '')
                theme_info.shop_bonus = theme.get('Bonus', {}).get('ShopInCome', 0)
                theme_info.texture_suffix = theme.get('MaterialsOverride', {}).get('Suffix', '')

                shop_data = self.gameObjectData.get_object_shopdata(theme.id)
                if shop_data is not None:
                    theme_info.location = LOCATIONS.get(
                        strToInt(shop_data.get('MapZone', -1)),
                        'UNKNOWN',
                    )
                    theme_info.price = self.get_price(shop_data)
                
                theme_info.image['main'] = self.add_image(
                    [theme.get('Appearance', {}).get('Image', '')],
                    theme_images/f'{theme.id}.png',
                )
                
            except Exception as e:
                e.add_note(f'Theme: {theme.id}')
                console.print_exception()

    def get_category_path(self):
        paths = self.game_data.game_objects.path.objects
        
        path_images = self.game_objects_folder/'path/main'
        path_images.mkdir(parents = True, exist_ok = True)
        
        for index, path in enumerate(track(
            self.gameObjectData['Path'].values(),
            description = 'Getting paths...',
        )):
            try:
                path_info = PathType.model_construct(
                    index = index,
                    id = path.id,
                    name = self.translate_string(path.get('Name', {}).get('Unlocal', path.id)),
                    image = {},
                )
                
                paths[path.id] = path_info
                
                path_info.sprite = path.get('Sprite', {}).get('Sprite', '')

                path_info.location = LOCATIONS.get(
                    strToInt(path.get('Shop', {}).get('MapZone', -1)),
                    'UNKNOWN',
                )
                path_info.image['main'] = self.add_image(
                    [path.get('Shop', {}).get('Icon', '')],
                    path_images/f'{path.id}.png',
                )
                
            except Exception as e:
                e.add_note(f'Path: {path.id}')
                console.print_exception()

    def get_category_item(self):
        items = self.game_data.game_objects.item.objects
        
        item_images = self.game_objects_folder/'item/main'
        item_images.mkdir(parents = True, exist_ok = True)

        with open(self.game_folder/'prizetype.json', 'r') as file:
            prize_types: PrizeTypes = json.load(file)
        
        for index, (id, item) in enumerate(track(
            prize_types['PrizeData'].items(),
            description = 'Getting items...',
        )):
            try:
                item_info = ItemType.model_construct(
                    index = index,
                    id = id,
                    name = self.translate_string(item['loc_string']),
                    image = {},
                )
                
                items[id] = item_info
                
                item_info.image['main'] = self.add_image(
                    [item['image']],
                    item_images/f'{id}.png',
                )

                item_info.alt_ids = prize_types['PrizeStrings'].get(id, [])
                
                
            except Exception as e:
                e.add_note(f'Item: {id}')
                console.print_exception()

    def get_category_booster(self):
        boosters = self.game_data.game_objects.booster.objects
        
        booster_images = self.game_objects_folder/'booster/main'
        booster_images.mkdir(parents = True, exist_ok = True)
        
        for index, booster in enumerate(track(
            self.gameObjectData['ProgressBooster'].values(),
            description = 'Getting boosters...',
        )):
            try:
                booster_info = BoosterType.model_construct(
                    index = index,
                    id = booster.id,
                    name = self.translate_string(booster.get('Shop', {}).get('Label', booster.id)),
                    image = {},
                )
                
                boosters[booster.id] = booster_info
                
                booster_info.image['main'] = self.add_image(
                    [booster.get('Shop', {}).get('Icon', '')],
                    booster_images/f'{booster.id}.png',
                )

                booster_info.type = list[Literal['xp', 'bits']](['xp', 'bits'])[booster.get('Settings', {}).get('Type', 0)]
                booster_info.time = booster.get('Settings', {}).get('Time', 0)
                booster_info.multiplier = booster.get('Settings', {}).get('Multiplier', 0)

                shop_data = self.gameObjectData.get_object_shopdata(booster.id)
                if shop_data is not None:
                    booster_info.price = self.get_price(shop_data)
                
                
                
            except Exception as e:
                e.add_note(f'Booster: {booster.id}')
                console.print_exception()

    def get_category_token(self):
        tokens = self.game_data.game_objects.token.objects
        
        token_images = self.game_objects_folder/'token/main'
        token_images.mkdir(parents = True, exist_ok = True)
        
        for index, token in enumerate(track(
            self.gameObjectData['QuestSpecialItem'].values(),
            description = 'Getting tokens...',
        )):
            try:
                token_info = TokenType.model_construct(
                    index = index,
                    id = token.id,
                    name = self.translate_string(token.get('QuestSpecialItem', {}).get('Name', token.id)),
                    image = {},
                )
                
                tokens[token.id] = token_info
                
                token_info.image['main'] = self.add_image(
                    [token.get('QuestSpecialItem', {}).get('Icon', '')],
                    token_images/f'{token.id}.png',
                )

                token_info.consumable = token.get('QuestSpecialItem', {}).get('ConsumableId', '')
                token_info.chance = self.defaultGameCampaign['game_object_data']['QuestSpecialItem'] \
                    .get(token.id, {}).get('Chance', token.get('QuestSpecialItem', {}).get('Chance', ''))

                token_info.tasks = [task for task in token.get('QuestSpecialItem', {}).get('PonyTasks', []) if task]

                token_info.unlimited = token.get('SaveSettings', {}).get('IsUnlimited', False)
                token_info.no_reset = token.get('SaveSettings', {}).get('DisableReset', False)
                token_info.special = token.get('Other', {}).get('SpecialCaseID', 0)

                for task in token_info.tasks:
                    if task not in self.game_data.tasks_data.tasks:
                        continue
                    
                    task_overrides = self.defaultGameCampaign['game_object_data']['QuestSpecialItem'].get(token.id, {}).get('PonyTasksOverride', {}).get(task, {})
                    
                    self.game_data.tasks_data.tasks[task].reward.token = token.id
                    self.game_data.tasks_data.tasks[task].reward.token_amount = task_overrides.get('Amount', 1)
                    self.game_data.tasks_data.tasks[task].chance = task_overrides.get('Chance', token_info.chance)

                
            except Exception as e:
                e.add_note(f'Token: {token.id}')
                console.print_exception()

    def get_category_consumable(self):
        consumables = self.game_data.game_objects.consumable.objects
        
        consumable_images = self.game_objects_folder/'consumable/main'
        consumable_images.mkdir(parents = True, exist_ok = True)
        
        for index, consumable in enumerate(track(
            self.gameObjectData['Consumable'].values(),
            description = 'Getting consumables...',
        )):
            try:
                consumable_info = ConsumableType.model_construct(
                    index = index,
                    id = consumable.id,
                    name = self.translate_string(consumable.get('Name', {}).get('Unlocal', consumable.id)),
                    image = {},
                )
                
                consumables[consumable.id] = consumable_info
                
                consumable_info.consume = {
                    'xp': consumable.get('Consume', {}).get('XP', 0),
                    'bits': consumable.get('Consume', {}).get('SoftCoins', 0),
                    'gems': consumable.get('Consume', {}).get('Gems', 0),
                    'hearts': consumable.get('Consume', {}).get('Hearts', 0),
                    'wheels': consumable.get('Consume', {}).get('MinecartWheel', 0),
                    'blitz_energy': consumable.get('Consume', {}).get('ClickerEnergy', 0),
                    'tls': consumable.get('Consume', {}).get('TLS', 0),
                }

                consumable_info.time = consumable.get('Production', {}).get('Time', 0)
                consumable_info.skip_cost = consumable.get('Production', {}).get('SkipCost', 0)

                if consumable.get('Farm', {}).get('LessButtonType', None) is not None:
                    consumable_info.farm = [
                        FarmCost.model_construct(
                            shard = consumable.get('Farm', {}).get('LessButtonType', ''),
                            shard_cost = consumable.get('Farm', {}).get('LessButtonTypeCost', 0),
                            item = 'Bits',
                            item_cost = consumable.get('Farm', {}).get('LessButtonCoinsCost', 0),
                        ),
                        FarmCost.model_construct(
                            shard = consumable.get('Farm', {}).get('MoreButtonType', ''),
                            shard_cost = consumable.get('Farm', {}).get('MoreButtonTypeCost', 0),
                            item = 'Gems',
                            item_cost = consumable.get('Farm', {}).get('MoreButtonCoinsCost', 0),
                        ),
                    ]
                
                if consumable.get('AnimalHouse', {}).get('MainFeedType', None) is not None:
                    consumable_info.critter = ConsumableCritter.model_construct(
                        critter = consumable.get('AnimalHouse', {}).get('AnimalID', ''),
                        main_feed = consumable.get('AnimalHouse', {}).get('MainFeedType', ''),
                        additional_feed = consumable.get('AnimalHouse', {}).get('AdditionalFeedType', ''),
                        phases = [
                            {
                                'main': consumable.get('AnimalHouse', {}).get('MainFeedFirstPhaseCost', 0),
                                'additional': 0,
                            },
                            {
                                'main': consumable.get('AnimalHouse', {}).get('MainFeedSecondPhaseCost', 0),
                                'additional': consumable.get('AnimalHouse', {}).get('AdditionalFeedSecondPhaseCost', 0),
                            },
                            {
                                'main': consumable.get('AnimalHouse', {}).get('FinalMainFeed', 0),
                                'additional': consumable.get('AnimalHouse', {}).get('FinalAdditionalFeed', 0),
                            },
                        ],
                        upgrade = CritterUpgrade.model_construct(
                            currency = CURRENCY.get(
                                consumable.get('AnimalHouse', {}).get('UpgradeCurrencyType', 1),
                                'Bits',
                            ),
                            cost = consumable.get('AnimalHouse', {}).get('UpgradeCurrencyCost', 0),
                            shard = consumable.get('AnimalHouse', {}).get('UpgradeShard', ''),
                            shard_cost = consumable.get('AnimalHouse', {}).get('UpgradeShardCost', 0),
                        ),
                        final_cooldown = consumable.get('AnimalHouse', {}).get('FinalTimerValue', 0),
                        final_reward = {
                            'gems': consumable.get('AnimalHouse', {}).get('FinalRewardGems', 0),
                            'xp': consumable.get('AnimalHouse', {}).get('FinalRewardXP', 0),
                        }
                    )
                

                consumable_image = consumable.get('Graphic', {}).get('Sprite', '')
                consumable_sprite = f'{consumable_image}.sprite'
                consumable_sprite_frame = consumable.get('Graphic', {}).get('Frame', '')
                if os.path.exists(self.game_folder/consumable_sprite):
                    consumable_image = find_in_sprite(self.game_folder/consumable_sprite, consumable_sprite_frame) or consumable_image
                
                consumable_info.image['main'] = self.add_image(
                    [consumable_image],
                    consumable_images/f'{consumable.id}.png',
                )
                
            except Exception as e:
                e.add_note(f'Consumable: {consumable.id}')
                console.print_exception()

    def get_category_costume(self):
        costumes = self.game_data.game_objects.costume.objects
        
        main_images = self.game_objects_folder/'costume/main'
        alt_images = self.game_objects_folder/'costume/alt'
        main_images.mkdir(parents = True, exist_ok = True)
        alt_images.mkdir(parents = True, exist_ok = True)
        
        for index, costume in enumerate(track(
            self.gameObjectData['PonySet'].values(),
            description = 'Getting costumes...',
        )):
            try:
                costume_info = CostumeType.model_construct(
                    index = index,
                    id = costume.id,
                    name = self.translate_string(costume.get('PonySet', {}).get('Localization', costume.id)),
                    image = {},
                )
                
                costumes[costume.id] = costume_info
                
                costume_info.image['main'] = self.add_image(
                    [costume.get('PonySet', {}).get('Icon', '')],
                    main_images/f'{costume.id}.png',
                )

                if costume.get('PonySet', {}).get('AltIcon', None):
                    costume_info.image['main'] = self.add_image(
                        [costume.get('PonySet', {}).get('AltIcon', '')],
                        alt_images/f'{costume.id}.png',
                    )
                
                costume_info.enabled = costume.get('PonySet', {}).get('Enabled', False)
                costume_info.can_be_new = costume.get('PonySet', {}).get('CanBeNew', False)
                costume_info.is_only_alternate_mesh = costume.get('PonySet', {}).get('IsOnlyAlternativeMesh', False)
                costume_info.is_subset = costume.get('PonySet', {}).get('IsSubSet', False)
                costume_info.subsets = list(costume.get('PonySet', {}).get('SubSets', []))
                if costume_info.subsets:
                    costume_info.subsets.insert(0, costume.id)

                costume_info.parts = {
                    'body': costume.get('Parts', {}).get('Body', None),
                    'mane': costume.get('Parts', {}).get('Mane', None),
                    'tail': costume.get('Parts', {}).get('Tail', None),
                }

                costume_info.bonus.type = costume.get('Bonus', {}).get('Type', '')
                costume_info.bonus.amount = costume.get('Bonus', {}).get('Amount', '')

                # TODO: Add TlsBackground

            except Exception as e:
                e.add_note(f'Costume: {costume.id}')
                console.print_exception()
        
        
        for pony in self.game_data.game_objects.pony.objects.values():
            if pony.costumes:
                for id in pony.costumes:
                    costumes[id].pony = pony.id
        

        for costume in costumes.values():
            pony = self.game_data.game_objects.pony.objects.get(costume.pony)
            if costume.subsets:
                for subset in costume.subsets:
                    costumes[subset].subsets = costume.subsets
                    if not costumes[subset].pony:
                        if pony and subset not in pony.costumes:
                            pony.costumes.append(subset)
                        costumes[subset].pony = costume.pony
        
        for costume in costumes.values():
            if not costume.pony:
                costume.tags.append('unused')

    def get_category_costume_part(self):
        costume_parts = self.game_data.game_objects.costume_part.objects
        
        main_images = self.game_objects_folder/'costume_part/main'
        alt_images = self.game_objects_folder/'costume_part/alt'
        main_images.mkdir(parents = True, exist_ok = True)
        alt_images.mkdir(parents = True, exist_ok = True)
        
        for index, costume_part in enumerate(track(
            self.gameObjectData['PonyPart'].values(),
            description = 'Getting costume part...',
        )):
            try:
                costume_part_info = CostumePartType.model_construct(
                    index = index,
                    id = costume_part.id,
                )
                
                costume_parts[costume_part.id] = costume_part_info

                part_overrides = self.defaultGameCampaign.get('game_object_data', {}).get('PonyPart', {}).get(costume_part.id, {})
                
                costume_part_info.model_name = costume_part.get('PonyPart', {}).get('ModelName', '')
                costume_part_info.linked_part = costume_part.get('PonyPart', {}).get('LinkedPart', None)
                costume_part_info.type = costume_part.get('PonyPart', {}).get('Type', '').lower()
                costume_part_info.apply_time = part_overrides.get('ApplyTime', costume_part.get('PonyPart', {}).get('ApplyTime', 0))
                costume_part_info.gem_price = part_overrides.get('PurchasePrice', costume_part.get('PonyPart', {}).get('PurchasePrice', 0))
                costume_part_info.materials = part_overrides.get('Ingredients', costume_part.get('PonyPart', {}).get('Ingredients', [0] * 5))
                costume_part_info.apply_price = part_overrides.get('ApplyPrice', 0)

                
                costume_part_info.image['main'] = self.add_image(
                    [costume_part.get('PonyPart', {}).get('Icon', '')],
                    main_images/f'{costume_part.id}.png',
                )

                if costume_part.get('PonyPart', {}).get('AltIcon', None):
                    costume_part_info.image['main'] = self.add_image(
                        [costume_part.get('PonyPart', {}).get('AltIcon', '')],
                        alt_images/f'{costume_part.id}.png',
                    )
                
            except Exception as e:
                e.add_note(f'Costume part: {costume_part.id}')
                console.print_exception()


    def get_group_quests(self):
        console.print('Getting group quests')

        self.game_data.group_quests.random_pros = self.defaultGameCampaign.get('group_quests', {}).get('random_pros', [])

        with open(self.game_folder/'groupquests.json', 'r') as file:
            group_quest_data: GroupQuestsType = json.load(file)
        
        for id, quest_data in group_quest_data.items():
            quest_info = QuestDetail.model_construct(
                name = self.translate_string(quest_data['Name']),
                description = self.translate_string(quest_data['Description']),
            )

            self.game_data.group_quests.quests[id] = quest_info

            for story_point in quest_data['StoryPoints']:
                if not story_point.get('PremiumPony'):
                    continue

                pro = story_point['PremiumPony']
                self.game_data.game_objects.pony.objects[pro].pro.append(id)
                quest_info.pro.append(pro)
        
        for pony in self.game_data.group_quests.random_pros:
            self.game_data.game_objects.pony.objects[pony].pro.append('random')
        
        for seasonal_slot in self.defaultGameCampaign.get('group_quests', {}).get('seasonal_slots', []):
            self.game_data.group_quests.quests[seasonal_slot['seasonal']].special = 'seasonal'
        
        self.game_data.group_quests.quests['GQ_0_Tutorial'].special = 'tutorial'

    def get_fortune_shop(self):
        if (self.game_folder/'fortuneshopdata.json').exists():
            with open(self.game_folder/'fortuneshopdata.json', 'r') as file:
                fortune_shop_data = json.load(file)
        else:
            fortune_shop_data = self.defaultGameCampaign.get('global_defines', {}).get(
                'fortune_shop_data',
                self.defaultGameCampaign.get('global_defines', {}).get('personal_shop_data')
            )

        console.print('Getting fortune shop')

        if not fortune_shop_data:
            console.log('No fortune shop data')
            return

        fortune_shop = self.game_data.fortune_shop

        fortune_shop.max_items_in_shop = fortune_shop_data.get('max_items_in_shop', 6)
        fortune_shop.refresh_cost = fortune_shop_data['refresh_cost']
        fortune_shop.item_rarity_chances = fortune_shop_data['item_rarity_chances']
        fortune_shop.item_price_chances = fortune_shop_data['item_price_chances']

        price_names: list[FortuneShopPrices] = ['regular', 'discount', 'super', 'ultra']

        for rarity, items in fortune_shop_data['item_lists'].items():
            fortune_shop.items[rarity] = {}
            for item in track(
                items,
                description = f'Getting {rarity} prices...',
                transient = True,
            ):
                fortune_shop.items[rarity][item['id']] = item_info = FortuneShopItem(
                    id = item['id'],
                    rarity = rarity,
                    amount = item.get('amount', 1),
                    prices = FortuneShopItemPricesList.model_construct(
                        regular = dict(zip(price_names, item['price_list'])),
                        royal = dict(zip(price_names, item.get('sub_price_list', []))),
                    )
                )

    def get_tasks(self):
        with open(self.game_folder/'ponytasks.json', 'r') as file:
            ponytasks: PonyTasksType = json.load(file)
        
        task_icons = self.images_folder/'tasks'
        task_icons.mkdir(parents = True, exist_ok = True)
        
        for index, task in enumerate(track(ponytasks['PonyTasks'], description = 'Getting tasks...')):
            task_info = TaskEntry.model_construct(
                id = task['ID'],
                index = index,
                name = self.translate_string(task['LocalizedName']),
                pony = task['Pony'],
                duration = task['Duration'],
                hidden = task.get('HideTask', False),
                skip_cost = task['SkipCost'],
            )

            task_info.requirement.pony = task.get('PonyRequirement', '')
            task_info.requirement.house = task.get('GoToHouse', '')
            task_info.requirement.quests = task.get('QuestRequirements', [])

            if task_info.pony in self.game_data.game_objects.pony.objects:
                self.game_data.game_objects.pony.objects[task_info.pony].tasks.append(task_info.id)
            else:
                continue

            self.game_data.tasks_data.tasks[task_info.id] = task_info

            task_info.reward.bits = task.get('RewardCoins', 0)
            task_info.reward.gems = task.get('RewardGems', 0)
            task_info.reward.xp = task.get('RewardXp', 0)
            task_info.reward.consumable = task.get('RewardConsumable', '')
            task_info.reward.consumable_amount = task.get('RewardConsumableAmount', 0)

            task_info.image = self.add_image(
                [task['Icon']],
                task_icons/f'{task_info.id}.png',
            )

            
            
        for task in self.defaultGameCampaign.get('PonyTaskData', []):
            if task['ID'] not in self.game_data.tasks_data.tasks:
                continue
            task_info = self.game_data.tasks_data.tasks[task['ID']]

            if 'SkipCost' in task:
                task_info.skip_cost = task['SkipCost']
            if 'RewardCoins' in task:
                task_info.reward.bits = task['RewardCoins']
            if 'RewardXp' in task:
                task_info.reward.xp = task['RewardXp']
            if 'RewardGems' in task:
                task_info.reward.gems = task['RewardGems']
            if 'Duration' in task:
                task_info.duration = task['Duration']
    
    def get_collections(self):
        collections = self.game_data.collection_data.collections
        fashion_show = self.game_data.collection_data.fashion_show

        used_collections = set[str]()
        fashion_show_collections = set[str]()
        vip_collections = set[str]()

        raw_collection_xml = parse_xml(self.game_folder/'collectionData.xml')[0]

        index: dict[Literal['collection', 'fashion_show'], int] = {
            'collection': 0,
            'fashion_show': 0,
        }

        images_path = 'images/collections'


        collection_data = raw_collection_xml.find('CollectionData')
        collection_type_data = raw_collection_xml.find('CollectionType')
        collection_flags_data = raw_collection_xml.find('OtherFlags')

        if collection_data is None or collection_type_data is None or collection_flags_data is None:
            raise ValueError('Collection data malformed')
        
        for collection_list in collection_type_data:
            if collection_list.tag != 'CollectionList':
                continue
            collection_type = collection_list.get('collectionType')
            
            if collection_type == 'Collections':
                collection_set = used_collections
            elif collection_type == 'FashionShow':
                collection_set = fashion_show_collections
            else:
                continue

            for collection_item in collection_list:
                if collection_item.tag != 'Collection':
                    continue
                collection_set.add(collection_item.attrib['collectionId'])
        
        for collection_flag_list in collection_flags_data:
            if collection_flag_list.tag != 'DependsOnVIP':
                continue

            for collection_item in collection_flag_list:
                if collection_item.tag != 'Collection':
                    continue
                vip_collections.add(collection_item.attrib['id'])

        for collection_el in track(
            collection_data,
            description = 'Getting collections...'
        ):
            collection_id = collection_el.attrib['collectionId']
            try:
                items: list[CollectionItem] = []
                fashion_show_items: list[FashionShowItem] = []
                rewards = CollectionReward.model_construct()

                for child in collection_el:
                    if child.tag == 'Rewards':
                        for reward_el in child:
                            sub_reward = CollectionRewardEntry.model_construct(
                                item = reward_el.get('objectId') or '',
                                amount = strToInt(reward_el.get('objectAmount'), 0),
                            )
                            if reward_el.tag == 'Reward':
                                rewards.main = sub_reward
                            elif reward_el.tag == 'AltReward':
                                rewards.alt = sub_reward

                    elif child.tag == 'CollectionItem':
                        item_id = child.get('itemId')
                        if item_id:
                            item = CollectionItem(
                                item = item_id,
                                alt = child.get('DuplItemId'),
                                count = strToInt(child.get('itemNum'), 1),
                            )
                            items.append(item)

                            part_set = child.find('RequiredPartSet')

                            if part_set is not None:
                                entry = FashionShowItem.model_construct(pony = item_id)

                                for part in part_set:
                                    if part.tag == 'part':
                                        entry.parts.append(part.attrib.get('id', ''))
                                
                                fashion_show_items.append(entry)
                
                if len(fashion_show_items):
                    fashion_show[collection_id] = FashionShowEntry.model_construct(
                        index = index['fashion_show'],
                        id = collection_id,
                        name = self.translate_string(collection_el.attrib.get('locString', collection_id)),
                        reward = rewards,
                        items = fashion_show_items,
                    )
                    
                    index['fashion_show'] += 1
                else:
                    tags: list[str] = []

                    if collection_id not in used_collections:
                        tags.append('unused')
                    if collection_id in vip_collections:
                        tags.append('vip')

                    collections[collection_id] = CollectionEntry.model_construct(
                        index = index['collection'],
                        id = collection_id,
                        name = self.translate_string(collection_el.attrib.get('locString', collection_id)),
                        tags = tags,
                        reward = rewards,
                        ponies = items,
                        image = {lang: f'{images_path}/{lang}/{collection_id}.png' for lang in LANGUAGES},
                    )
                    
                    index['collection'] += 1

                    for item in items:
                        if item.item not in self.game_data.game_objects.pony.objects:
                            continue
                        self.game_data.game_objects.pony.objects[item.item].collections.append(collection_id)
                    
                    if rewards.main.item in self.game_data.game_objects.pony.objects:
                        self.game_data.game_objects.pony.objects[rewards.main.item].collections.append(collection_id)
                
            except Exception as e:
                e.add_note(f'Collections: {collection_id}')
                console.print_exception()

    def get_maze_data(self):
        main_images = self.images_folder/'events/main/'
        outro_images = self.images_folder/'events/outro/'
        main_images.mkdir(parents = True, exist_ok = True)
        outro_images.mkdir(parents = True, exist_ok = True)
        
        maze_data = self.defaultGameCampaign.get('MazeData')
        if maze_data is None:
            with open(self.override_folder/'fallback_maze_data.json', 'r', encoding = 'utf-8') as file:
                maze_data: GameData_MazeData | None = json.load(file)
        
        if maze_data is None:
            console.print('[red]Could not get maze data[/]')
            return
        
        with open(self.game_folder/'arenaqtesettings.json', 'r', encoding = 'utf-8') as file:
            arena_qt_settings: ArenaQTESettingsType = json.load(file)
        
        arena_presets = {preset['ID']: preset for preset in arena_qt_settings['Presets']}

        with open(self.game_folder/'mazesettings.json', 'r', encoding = 'utf-8') as file:
            maze_settings: dict = json.load(file)['Parameters'][0]

        event_id: str = maze_settings['TLS_ID']
        mapzone: int = maze_settings['MapZone']
        
        maze_save = parse_xml(self.game_folder/f'initial_pony_save_{mapzone}.xml')[0][0]
        initial_objects = maze_save.find('Inital_Objects')
        if initial_objects is None:
            raise ValueError('Cannot find initial maze objects')
        
        
        maze_quest_info = self.quest_manager[event_id]
        self.game_data.maze_data.id = event_id
        self.game_data.maze_data.name = self.translate_string(maze_quest_info.loc_name)
        self.game_data.maze_data.image = {
            'main': self.add_image(
                [maze_quest_info.image],
                main_images/f'{maze_quest_info.name}.png',
            ),
            'outro': self.add_image(
                [maze_quest_info.final_image],
                outro_images/f'{maze_quest_info.name}.png',
            ),
        }

        self.game_data.maze_data.settings = MazeDataTypes.MazeSettings(
            tile_energy = maze_settings.get('MoveEnergyCost', 30),
            battle_energy = maze_settings.get('BattleEnergy', 15),
            max_energy = maze_settings.get('MaxEnergy', 300),
            energy_cooldown = maze_settings.get('RestoreEnergyCooldown', 114),
        )
        
        for object_category in initial_objects:
            if object_category.tag == 'Pony_House_Objects':
                for obj_el in object_category:
                    position_el = obj_el.find('Position')
                    house_id = obj_el.attrib['ID']
                    if position_el is None:
                        console.print(f'Cannot find position for {house_id}')
                        continue
                    
                    self.game_data.maze_data.map.shops.append(
                        MazeDataTypes.MazeMapShop(
                            id = house_id,
                            x = strToInt(position_el.attrib['x']),
                            y = strToInt(position_el.attrib['y']),
                        )
                    )

                    if house_id in self.game_data.game_objects.shop.objects:
                        self.game_data.game_objects.shop.objects[house_id].location = 'MAZE'

            elif object_category.tag == 'MazeBlock_Objects':
                for block_el in track(object_category, description = 'Getting maze map...'):
                    block_id = block_el.attrib['ID']
                    block_obj = self.gameObjectData.get_object(block_id)
                    if block_obj is None:
                        console.print(f'Cannot find {block_id}')
                        continue

                    position_el = block_el.find('Position')
                    entity_el = block_el.find('Entity')

                    if position_el is None:
                        console.print(f'Cannot find position for {block_id}')
                        continue

                    block = MazeDataTypes.MazeMapBlock(
                        id = block_id,
                        x = strToInt(position_el.get('x')),
                        y = strToInt(position_el.get('y')),
                        uncovered = strToBool(block_el.get('IsUncovered')),
                        connections = {
                            'north_east': block_obj.get('Connections', {}).get('NorthEast', False),
                            'north_west': block_obj.get('Connections', {}).get('NorthWest', False),
                            'south_east': block_obj.get('Connections', {}).get('SouthEast', False),
                            'south_west': block_obj.get('Connections', {}).get('SouthWest', False),
                        },
                    )

                    if entity_el is not None:
                        block.entity = MazeDataTypes.MazeBlockEntity(
                            id = entity_el.attrib['ID'],
                            type = entity_el.attrib['Type'], # type: ignore
                        )
                    
                    self.game_data.maze_data.map.blocks.append(block)
                
        for maze_pony in track(
            self.gameObjectData['MazePony'].values(),
            description = 'Getting maze ponies...'
        ):
            self.game_data.maze_data.ponies[maze_pony.id] = MazeDataTypes.MazePony(
                id = maze_pony.id,
                pony = maze_pony.get('Parent', {}).get('PonyName', ''),
                power = maze_pony.get('Stats', {}).get('DamagePower', 0),
                fights = maze_pony.get('Stats', {}).get('Fights_Amount', 0),
                upgraded = maze_pony.get('Stats', {}).get('Elite_By_Default', 0),
            )
        

        for maze_boss in track(
            self.gameObjectData['MazeBoss'].values(),
            description = 'Getting maze bosses...'
        ):
            self.game_data.maze_data.bosses[maze_boss.id] = MazeDataTypes.MazeBoss(
                id = maze_boss.id,
                pony = maze_boss.get('Parent', {}).get('PonyName', ''),
                required_power = math.ceil(maze_boss.get('Stats', {}).get('Hits', 0) / arena_presets.get(maze_boss.get('Fights', {}).get('QTEPreset', ''), {}).get('ActionHitsNum', 1)),
                hp = maze_boss.get('Stats', {}).get('Hits', 0),
                critical_multiplier = maze_boss.get('Stats', {}).get('CritMultiplier', 0),
                drop_chest = maze_boss.get('Fights', {}).get('DropChest', ''),
                rewards = [
                    MazeDataTypes.MazeBossReward(
                        item = item,
                        amount = amount,
                    )
                    for item, amount in zip(
                        maze_boss.get('Rewards', {}).get('Prizes', []),
                        maze_boss.get('Rewards', {}).get('Amounts', []),
                    )
                    if item
                ],
            )

        for maze_shop in track(
            self.gameObjectData['MazeChest'].values(),
            description = 'Getting maze chests...'
        ):
            self.game_data.maze_data.chests[maze_shop.id] = MazeDataTypes.MazeChest(
                id = maze_shop.id,
                tier = maze_shop.get('Rewards', {}).get('TierID', ''),
            )


        for maze_shop in track(
            self.gameObjectData['MazeShop'].values(),
            description = 'Getting maze shops...'
        ):
            self.game_data.maze_data.shops[maze_shop.id] = MazeDataTypes.MazeShop(
                id = maze_shop.id,
                tier = maze_shop.get('Display', {}).get('TierID', ''),
            )
        
        for tier_id, tier_info in maze_data['MazePonyPrice'].items():
            self.game_data.maze_data.shop_tiers[tier_id] = MazeDataTypes.MazeShopTier(
                id = tier_id,
                slots = [
                    MazeDataTypes.MazeShopSlot(
                        id = slot[0].get('id', ''),
                        price = slot[0].get('price', ''),
                    )
                    for slot in tier_info.values()
                ]
            )

        for tier_id, tier_info in maze_data['ChestRewards'].items():
            self.game_data.maze_data.chest_rewards[tier_id] = [
                slot['id']
                for slot in tier_info
            ]
        
        self.game_data.maze_data.community.token = maze_settings['LeaderboardCurrency']
        
        bosses = {boss['ID']: boss for boss in maze_settings['Bosses']}

        base_points: int | None = None

        for comm_helper in maze_settings['LeaderboardBosses']:
            boss_id: str = comm_helper['BossId']
            boss = bosses.get(boss_id)
            if boss is None:
                console.print(f'[red]Cannot find boss {boss_id}[/]')
                continue

            preset_id: str = boss['QTEPreset']
            preset = arena_presets.get(preset_id)
            if preset is None:
                console.print(f'[red]Cannot find preset {preset_id}[/]')
                continue

            leaderboard_loot: int = 0
            for bar in preset['ActionBars']:
                if bar['Loot'] == 'leaderboard_currency':
                    leaderboard_loot = bar['LootCount']
                    break
            
            leaderboard_hits: int = preset['ActionHitsNum']
            leaderboard_crit_chance: float = preset['ActionCritChance']
            leaderboard_crit_multiplier: float = preset['ActionCritMultiplier']

            average_crits = leaderboard_hits * leaderboard_crit_chance

            min_score = leaderboard_loot * leaderboard_hits
            average_score = round((
                leaderboard_loot * (leaderboard_hits - average_crits)
            ) + (
                leaderboard_loot * leaderboard_crit_multiplier * average_crits
            ))

            if base_points is None:
                base_points = min_score


            helper = MazeDataTypes.MazeCommHelper(
                id = comm_helper['BossId'],
                pony = boss['BossPony']['Pony'],
                cooldown = comm_helper['Cooldown'],
                skip = comm_helper['SkipCost'],
                points = [min_score, average_score],
                multiplier = min_score // base_points,
            )

            self.game_data.maze_data.community.helpers.append(helper)
    
    def get_quest_data(self):
        quest_data = self.game_data.quest_data

        quest_icons = self.images_folder/'quests/quests/icon/'
        giver_icons = self.images_folder/'quests/quests/giver_icon/'
        giver_images = self.images_folder/'quests/quests/giver_image/'
        task_icons = self.images_folder/'quests/quests/tasks/'

        category_main_images = self.images_folder/'quests/category/main/'
        category_outro_images = self.images_folder/'quests/category/outro/'
        category_reward_images = self.images_folder/'quests/category/reward/'

        for path in [quest_icons,
                     giver_icons,
                     giver_images,
                     task_icons,
                     category_main_images,
                     category_outro_images,
                     category_reward_images]:
            
            path.mkdir(parents = True, exist_ok = True)
        
        quest_table = QuestTable(self.game_folder/'questtable.xml')

        for category in track(
            self.quest_manager.values(),
            description = 'Getting quest categories...',
        ):
            category_data = QuestDataTypes.QuestCategory(
                id = category.name,
                name = self.add_translation(category.loc_name),
                final_text = self.add_translation(category.final_text),
                active_limit = category.active_limit,
                time_limited = category.time_limited,
                building = category.building,
                outro_cinematic = category.outro_cinematic,
            )
            quest_data.categories[category_data.id] = category_data

            if category.image:
                category_data.image['main'] = self.add_image(
                    [category.image],
                    category_main_images/f'{category_data.id}.png',
                )
            if category.final_image:
                category_data.image['outro'] = self.add_image(
                    [category.final_image],
                    category_outro_images/f'{category_data.id}.png',
                )
            if category.reward_icon:
                category_data.image['reward'] = self.add_image(
                    [category.reward_icon],
                    category_reward_images/f'{category_data.id}.png',
                )

        for quest in track(
            quest_table.values(),
            description = 'Getting quests...',
        ):
            quest_info = QuestDataTypes.QuestType(
                id = quest.name,
                category = quest.category,
                info = QuestDataTypes.QuestInfo(
                    title = self.add_translation(quest.info.title),
                    description = self.add_translation(quest.info.description),
                    completed_description = self.add_translation(quest.info.complete_description),
                    skippable = quest.info.skippable,
                    auto_start = quest.info.auto_start,
                    global_quest = quest.info.global_quest,
                    invisible = quest.info.invisible_quest,
                    add_to_complete_after_start = quest.info.add_to_complete_after_start,
                    skip_for_COPPA = quest.info.skip_for_COPPA,
                    skip_for_OFT = quest.info.skip_for_OFT,
                    location = LOCATIONS.get(quest.info.mapzone, 'UNKNOWN'),
                    any_quest = quest.info.any_quest,
                ),
                rewards = QuestDataTypes.QuestRewards(
                    bits = quest.rewards.bits,
                    gems = quest.rewards.gems,
                    hearts = quest.rewards.hearts,
                    xp = quest.rewards.xp,
                    item1 = QuestDataTypes.QuestRewardItem(
                        id = quest.rewards.item.id,
                        value = quest.rewards.item.value,
                        alt_currency = 'Gems',
                        alt_value = quest.rewards.item.alt_value,
                        consumable_id = quest.rewards.item.consumable_id,
                        consumable_count = quest.rewards.item.consumable_count,
                    ),
                    item2 = QuestDataTypes.QuestRewardItem(
                        id = quest.rewards.item2.id,
                        value = quest.rewards.item2.value,
                        alt_currency = 'Gems',
                        alt_value = quest.rewards.item2.alt_value,
                        consumable_id = quest.rewards.item2.consumable_id,
                        consumable_count = quest.rewards.item2.consumable_count,
                    ),
                ),
            )

            quest_data.quests[quest_info.id] = quest_info

            for task in quest.task_list:
                task_entry = QuestDataTypes.QuestTask(
                    id = task.name,
                    description = self.add_translation(task.description),
                    skippable = task.skippable,
                    skip_for_OFT = task.is_OFT,
                    skip_cost = task.skip_cost,
                    ad_skip = not task.no_skip_by_ads,
                    has_go = task.has_go,
                )

                if task.counts:
                    task_entry.objective = QuestDataTypes.QuestTaskObjective(
                        scope = 'local' if task.counts.type in ['LocalObjectCount', 'LocalCount'] else 'global',
                        category = task.counts.category,
                        item = task.counts.sub_object,
                        value = task.counts.value,
                    )

                if task.icon:
                    image_name = os.path.splitext(os.path.basename(task.icon))[0]
                    task_entry.icon = self.add_image(
                        [task.icon],
                        task_icons/f'{image_name}.png',
                    )
            
            quest_info.requirements.any_quest = quest.requirements.any_quest
            quest_info.requirements.quests_completed = quest.requirements.quests_completed.copy()
            quest_info.requirements.no_start_zones = [LOCATIONS.get(zone, 'UNKNOWN') for zone in quest.requirements.no_start_zones]

            for count in quest.requirements.global_counts:
                quest_info.requirements.global_counts.append(QuestDataTypes.GlobalCountRequirement(
                    category = count.category,
                    item = count.sub_object,
                    amount = count.value,
                ))

            for event in quest.events.start:
                quest_info.events.start.append(QuestDataTypes.QuestEvent(
                    type = event.type,
                    value = event.value,
                ))
            for event in quest.events.end:
                quest_info.events.end.append(QuestDataTypes.QuestEvent(
                    type = event.type,
                    value = event.value,
                ))

            if quest.info.icon:
                image_name = os.path.splitext(os.path.basename(quest.info.icon))[0]
                quest_info.info.icon = self.add_image(
                    [quest.info.icon],
                    quest_icons/f'{image_name}.png',
                )
            if quest.info.giver_icon:
                image_name = os.path.splitext(os.path.basename(quest.info.giver_icon))[0]
                quest_info.info.giver_icon = self.add_image(
                    [quest.info.giver_icon],
                    giver_icons/f'{image_name}.png',
                )
            if quest.info.giver_image:
                image_name = os.path.splitext(os.path.basename(quest.info.giver_image))[0]
                quest_info.info.giver_image = self.add_image(
                    [quest.info.giver_image],
                    giver_images/f'{image_name}.png',
                )
        
        for quest in quest_data.quests.values():
            not_first = False

            for id in quest.requirements.quests_completed:
                if id in quest_data.quests:
                    previous = quest_data.quests[id]
                    not_first = not_first or quest.category == previous.category
                    previous.next.append(quest.id)

                    if quest.category != previous.category:
                        if (previous_category := quest_data.categories.get(previous.category)):
                            if quest.id not in previous_category.connections.unlocks:
                                previous_category.connections.unlocks.append(quest.id)
                        
                        if (current_category := quest_data.categories.get(quest.category)):
                            if previous.id not in current_category.connections.depends:
                                current_category.connections.depends.append(previous.id)
                        

            if not not_first:
                if (category := quest_data.categories.get(quest.category)):
                    category.connections.start.append(quest.id)


    def get_dialogue(self):
        cinematic_table = CinematicTable(
            self.game_folder/'cinematictable.xml',
            self.game_folder/'cinematicmanager.xml'
        )

        participants: dict[str, str] = {}
        images: dict[str, dict[str, Path]] = {} # dict[swf, set[label]]

        images_path = self.images_folder/'dialogue'

        def add_line_image(flash_id: str, label: str):
            path = images_path / Path(flash_id).stem / f'{label}.png'
            key = path.relative_to(self.output_folder).as_posix()

            images.setdefault(flash_id, {})[label] = path
            return RenamedFile(
                path = key,
                original = label,
            )

        for scene in track(
            cinematic_table.values(),
            description = 'Getting dialogue',
        ):
            flash_id = scene.flash_id or 'conversationmanager.swf'
            scene_data = CinematicTypes.CinematicType(
                id = scene.name,
            )
            self.game_data.cinematic_data.cinematics[scene.name] = scene_data

            for event in scene.events:
                if event.name == 'ConvoAddParticipant':
                    name = event.parameters['Participant']['Name']
                    id = event.parameters['Pony']['PonyID']
                    participants[str(name)] = str(id)
                elif event.name == 'ConvoTalk':
                    name = str(event.parameters['Talk']['Name'])
                    label = participants.get(name, name)
                    line = str(event.parameters['Talk']['Text_Local'])

                    scene_data.lines.append(CinematicTypes.CinematicLine(
                        image = add_line_image(flash_id, label),
                        line = self.add_translation(line),
                    ))
                elif event.name == 'CinematicLine':
                    name = str(event.parameters['LineSettings']['PonyID'])
                    label = participants.get(name, name)
                    line = str(event.parameters['LineSettings']['Text_Local'])

                    scene_data.lines.append(CinematicTypes.CinematicLine(
                        image = add_line_image(flash_id, label),
                        line = self.add_translation(line),
                    ))

        with Progress(
            *COLUMNS,
            console = console,
        ) as progress:
            task = progress.add_task(
                description = 'Getting dialogue images...',
                total = sum(len(labels) for labels in images.values()),
            )

            for flash_id, labels in images.items():
                swf_path = self.game_folder/'swf'/flash_id
                if not swf_path.is_file():
                    console.print(f'[red]Cannot find {flash_id}[/]')
                    progress.advance(task, len(labels))
                    continue

                
                with SWF(swf_path, ffdec = self.ffdec) as swf:
                    for label, path in labels.items():
                        path.parent.mkdir(parents = True, exist_ok = True)
                        image = swf.get_portrait(label)
                        if image is not None:
                            image = crop_image(image)
                            image.save(path)
                        else:
                            console.print(f'[red]Cannot render label {flash_id}/{label}')

                        progress.advance(task)

    
    def translate_string(self, key: str) -> TranslatableString:
        return {lang: loc.translate(key).strip().replace('|', '') for lang, loc in self.locs.items()}
    
    def add_translation(self, key: str) -> str:
        """
        Add translations to game_data.locales and return the string id

        Args:
            key (str): String key

        Returns:
            str: String key
        """

        translation = self.translate_string(key)
        for lang, string in translation.items():
            self.game_data.locales[lang][key] = string
        
        return key
    
    def add_image(self, game_paths: list[str], dest: str | Path) -> RenamedFile:
        used_game_name: str | None = None
        used_filename: str | Path | None = None
        dest = Path(dest)

        source_paths = set[str]()
        for path in game_paths:
            if path:
                path = path.strip()
                path = path.replace('\\', '/')
                source_paths.add(path.removeprefix('/'))
                source_paths.add(os.path.basename(path))
        

        for filename in source_paths:
            name = os.path.splitext(filename)[0]

            if os.path.exists(self.game_folder/(name + '.png')):
                used_filename = self.game_folder/(name + '.png')
            elif os.path.exists(self.game_folder/(name + '.pvr')):
                used_filename = self.game_folder/(name + '.pvr')
            else:
                found_paths = list(self.game_folder.glob(name + '.*', case_sensitive = False))
                if len(found_paths):
                    for path in found_paths:
                        if path.suffix == '.png' or (path.suffix == '.pvr' and '.alpha' not in path.name):
                            used_filename = path
                            break

            if used_filename is not None:
                used_game_name = filename
                break
        
        rel_path = dest.relative_to(self.output_folder).as_posix()
        
        if used_filename is not None:
            future = self._image_executor.submit(_transform_image, used_filename, dest)
            self._image_futures.append(future)
        else:
            console.print(f'[red]Could not find {game_paths}, {rel_path}[/]')
        
        return RenamedFile.model_construct(path = rel_path, original = used_game_name)
    
    def add_animated_image(self, game_path: str, dest: str | Path) -> RenamedFile:
        if os.path.exists(self.game_folder/game_path):

            future = self._image_executor.submit(
                swf2webp,
                self.game_folder/game_path,
                dest,
                ffdec_path = self.ffdec,
            )
            self._image_futures.append(future)
        
        return RenamedFile.model_construct(
            path = Path(dest).relative_to(self.output_folder).as_posix(),
            original = game_path,
        )
    
    def get_price(self, shopdata: ShopItem):
        price = Price.model_construct()

        price.base.currency = CURRENCY.get(shopdata.get('CurrencyType', 0))
        price.base.amount = shopdata.get('Cost', 0)

        price.token = shopdata.get('TaskTokenID')
        price.daily_goals = self.daily_goals_shop.get(shopdata.id, 0)

        return price

