"""Self-authored two-joint flag in RK/ANIM formats. No game bytes or artwork."""
import math
from pathlib import Path
import struct
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image
from luna_kit.gameobjectdata import GameObjectData


def write_fixture(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)

    def text(value):
        return value.encode().ljust(64, b"\0")

    # Native upright is negative Y; Rx(pi) in the GLB makes this flag Y-up.
    vertices = [(-.45, 0, 0, 0, 0), (.45, 0, 0, 1, 0),
                (-.45, -1, 0, 0, .5), (.45, -1, 0, 1, .5),
                (-.45, -2, 0, 0, 1), (.45, -2, 0, 1, 1)]
    bind = np.repeat(np.eye(4, dtype="<f4")[None], 2, axis=0)
    bind[1, 1, 3] = -1
    bones = b"".join(struct.pack("<3i", parent, index, 0) + bind[index].T.tobytes() + text(name)
                     for index, (parent, name) in enumerate([(-1, "root"), (0, "flag")]))
    parts = [
        (1, 1, struct.pack("<4I", 4, 0, 0, 0)),
        (2, 1, text("flag")),
        (3, 6, b"".join(struct.pack("<5f", *vertex) for vertex in vertices)),
        (4, 12, struct.pack("<12H", 0, 1, 2, 1, 3, 2, 2, 3, 4, 3, 5, 4)),
        (7, 2, bones), (13, 1, struct.pack("<H2B", 1026, 12, 0)),
        (16, 1, text("flag")),
        (17, 6, b"".join(struct.pack("<4B4H", joint, 0, 0, 0, 65535, 0, 0, 0)
                          for joint in [0, 0, 1, 1, 1, 1])),
    ]
    offset = 80 + 24 * 16
    entries, body = [], bytearray()
    for tag, count, data in parts:
        entries.append(struct.pack("<4I", tag, offset, count, len(data)))
        body.extend(data)
        offset += len(data)
    (folder / "flag.rk").write_bytes(struct.pack("<8sII64s", b"RKFORMAT", 5, 2, text("Synthetic flag"))
                                    + b"".join(entries).ljust(24 * 16, b"\0") + body)
    # Type 0 global poses, 46 frames / 15 fps; the child swings around its bind.
    frames = bytearray()
    for index in range(46):
        angle = math.sin(index * 2 * math.pi / 45) * .65
        frames.extend(struct.pack("<8f", 0, 0, 0, 1, 0, 0, 0, 1))
        frames.extend(struct.pack("<8f", 0, 0, math.sin(angle / 2), math.cos(angle / 2), 0, -1, 0, 1))
    (folder / "flag.anim").write_bytes(struct.pack("<8sII64sIII", b"RKFORMAT", 5, 2, text("flag"), 2, 46, 0) + frames)
    (folder / "flag.csv").write_text("rest,0,0,15\nwave,0,45,15\n", encoding="utf-8")
    (folder / "flag.xml").write_text("<MeshList/><AnimationList/>", encoding="utf-8")
    (folder / "flag.rkm").write_text("DiffuseTexture=flag\nShader=rkshader\nNoCompress=true\n"
                                     "ClampMode=RK_REPEAT\nBlendMode=none\nCull=0\nDepthWrite=1\nDepthTest=1\n", encoding="utf-8")
    image = Image.new("RGBA", (16, 16))
    image.putdata([(40, 180, 210, 255) if (x // 4 + y // 4) % 2 == 0 else (255, 205, 60, 255)
                   for y in range(16) for x in range(16)])
    image.save(folder / "flag.png")


def write_native_tables(folder, declarations=None):
    """Declare synthetic resources using the real native table field names."""
    folder = Path(folder)
    if declarations is None:
        declarations = {'Synthetic_Flag': {'model': 'flag', 'animation': 'flag', 'events': 'flag'}}
    (folder/'gameobjectcategorydata.xml').write_text('''<Categories>
<GameObjectCategory Name="Pony">
<Parameter Name="Model"><Attribute Name="MediumLOD" Type="string"/></Parameter>
<Parameter Name="Animation" Optional="1">
 <Attribute Name="Rig_MediumLOD" Type="string"/>
 <Attribute Name="AnimationEventsXML" Type="string"/>
 <Attribute Name="LeftIdleAnimationList" Type="stringWithDefault" Array="20"/>
</Parameter>
<Parameter Name="Conversation" Optional="1"><Attribute Name="IdleAnimation" Type="string"/></Parameter>
<Parameter Name="Eyes" Optional="1">
 <Attribute Name="EyeSubObject_Open" Type="stringWithDefault" Array="2" DefaultValue="eyes_open"/>
 <Attribute Name="EyeSubObject_Closed" Type="stringWithDefault" Array="2" DefaultValue="eyes_shut"/>
 <Attribute Name="EyeSubObject_Happy" Type="stringWithDefault" Array="2" DefaultValue="eyes_happy"/>
 <Attribute Name="EyeSubObject_Frown" Type="stringWithDefault" Array="2" DefaultValue="eyes_frown"/>
</Parameter>
<Parameter Name="Sets"><Attribute Name="Sets" Type="stringWithDefault" Array="35"/></Parameter>
</GameObjectCategory></Categories>''', encoding='utf-8')
    root = ET.Element('GameObjects')
    category = ET.SubElement(root, 'Category', ID='Pony')
    for identity, declaration in declarations.items():
        obj = ET.SubElement(category, 'GameObject', ID=identity)
        ET.SubElement(obj, 'Model', MediumLOD=declaration['model'])
        animation = ET.SubElement(obj, 'Animation', Rig_MediumLOD=declaration['animation'],
                                  AnimationEventsXML=declaration['events'])
        idle_list = ET.SubElement(animation, 'LeftIdleAnimationList')
        ET.SubElement(idle_list, 'Item', Value='wave')
        ET.SubElement(obj, 'Conversation', IdleAnimation='rest')
        eyes = ET.SubElement(obj, 'Eyes')
        # A synthetic flag is deliberately its own declared visible mesh. This
        # is a fixture contract, not a fallback for real characters' eye groups.
        opened = ET.SubElement(eyes, 'EyeSubObject_Open')
        ET.SubElement(opened, 'Item', Value='flag')
        for state in ['Closed', 'Happy', 'Frown']:
            ET.SubElement(eyes, 'EyeSubObject_' + state)
        ET.SubElement(ET.SubElement(obj, 'Sets'), 'Sets')
    ET.ElementTree(root).write(folder/'gameobjectdata.xml', encoding='utf-8')
    (folder/'shopdata.xml').write_text('<ShopData/>', encoding='utf-8')
    (folder/'defaultGameCampaign.json').write_text('{"game_object_data":{}}', encoding='utf-8')
    return GameObjectData(folder/'gameobjectdata.xml', folder/'shopdata.xml',
                          folder/'gameobjectcategorydata.xml')
