"""Build a new local demo through game-data's normal model generation stage.

All tables, artwork and binary inputs are self-authored test fixtures. This
exercises explicit source discovery; it does not prove real-game coverage.
"""
import argparse
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from bootstrap import ROOT
from native_fixture import write_fixture
from luna_kit.gameobjectdata import GameObjectData
from game_data.model_gen import generate_models


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


def make_demo(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    native = output/'raw/extracted'
    assets = output/'assets'
    report_path = output/'raw/model-generation.json'
    write_fixture(native)
    objects = write_native_tables(native)
    campaign = json.loads((native/'defaultGameCampaign.json').read_text())
    report = generate_models(objects, native, assets, report_path,
                             campaign=campaign, public_identities={'Synthetic_Flag'})
    summary = {'fixture': 'self-authored; no game resource bytes',
               'assets': str(assets.resolve()), 'report': str(report_path.resolve()),
               'converted': report['generation']['converted']}
    (output/'demo-summary.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True,
                        help='New local output directory; existing paths are refused')
    args = parser.parse_args()
    try:
        summary = make_demo(args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
