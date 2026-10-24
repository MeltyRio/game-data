"""Compile native PonyBook controls from the already resolved object and rig.

The controller constants mirror the reviewed native PonyBook branches. Clip
availability alone never establishes a model/library association. That remains
owned by discovery; this stage only uses that association's validated CSV.
"""
from collections.abc import Callable
from pathlib import Path
import re
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from .visibility import VisibilityError, compile_subobject_event

if TYPE_CHECKING:
    from luna_kit.gameobjectdata import GameObject

STATES = ('Open', 'Closed', 'Happy', 'Frown')
DEFAULT_TAPS = {
    0: tuple('gen_tap_' + side for side in ('north', 'east', 'south', 'west')),
    3: tuple('granny_tap_' + side for side in ('north', 'east', 'south', 'west')),
}
RANDOM_TAPS = {
    2: ('gen_interact_01_high', 'gen_interact_02_med', 'gen_interact_03_low'),
    4: ('gen_interact_01', 'gen_interact_02', 'gen_interact_03', 'gen_interact_high_01',
        'gen_interact_high_centre', 'gen_interact_levelup_02', 'gen_interact_levelup_01', 'gen_upright_interact_bow'),
}
RANDOM_GENERIC = tuple('gen_interact_levelup_' + value for value in ('01', '02', '03', '04', '05', '06', '09'))


class InteractionError(ValueError):
    pass


def native_click_names(obj: GameObject) -> list[str]:
    """Native allowed branches, not the game's remote-service random gate."""
    conversation = obj.get('Conversation')
    if conversation is not None:
        value = conversation.get('InteractionAnimation', '')
        if not isinstance(value, str):
            raise InteractionError('Invalid Conversation.InteractionAnimation')
        return [value] if value else []
    ai = (obj.get('AI') or {}).get('Special_AI')
    if type(ai) is not int or ai not in range(6):
        raise InteractionError('Unreviewed AI.Special_AI for PonyBook clicks')
    return sorted(set(DEFAULT_TAPS.get(ai, ('gen_snap_01',))) | set(RANDOM_TAPS.get(ai, RANDOM_GENERIC)))


def read_events(path: Path) -> dict:
    """Read exact native records, retaining unknown events for diagnostics."""
    raw = path.read_bytes()
    text = raw.decode('utf-8-sig')
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
        raise InteractionError('DTD/entity declarations are unsupported')
    text = re.sub(r'^\s*<\?xml[^>]*\?>', '', text)
    root = ET.fromstring('<Events>' + text + '</Events>')
    return parse_events(root)


def parse_events(root: ET.Element) -> dict:
    """Validate and collect records from an already parsed native document."""
    if any(node.tag not in {'MeshList', 'AnimationList'} or node.attrib for node in root):
        raise InteractionError('Unsupported event XML structure')
    result, controls = {}, {}
    for section in root:
        if section.tag == 'MeshList':
            for model in section:
                if model.attrib or (model.text or '').strip():
                    raise InteractionError('Unsupported native model declaration')
                for item in model:
                    if (item.tag != 'SubObject' or set(item.attrib) != {'ID', 'Name', 'DefaultVisible'}
                            or len(item) or (item.text or '').strip()):
                        raise InteractionError('Unsupported native mesh declaration')
                    key = (model.tag, item.get('ID'))
                    value = (item.get('Name'), item.get('DefaultVisible'))
                    if not all(key) or not value[0] or value[1] not in {'0', '1'}:
                        raise InteractionError('Invalid native mesh declaration')
                    if key in controls and controls[key] != value:
                        raise InteractionError('Conflicting native mesh declaration')
                    controls[key] = value
        else:
            for clip in section:
                if clip.tag != 'Animation' or set(clip.attrib) != {'Name'} or not clip.get('Name'):
                    raise InteractionError('Invalid native animation declaration')
                rows = result.setdefault(clip.get('Name'), [])
                declaration_rows = []
                for frame in clip:
                    if frame.tag != 'Frame' or set(frame.attrib) != {'Index'} or not re.fullmatch(r'\d+', frame.get('Index', '')):
                        raise InteractionError('Invalid native event frame')
                    for event in frame:
                        if len(event) or (event.text or '').strip():
                            raise InteractionError('Nested native event is unsupported')
                        row = {'frame': int(frame.get('Index')), 'type': event.tag, 'attributes': dict(event.attrib)}
                        if row in declaration_rows:
                            raise InteractionError('Duplicate native event in one animation declaration')
                        declaration_rows.append(row)
                        # Native repeated Animation declarations can duplicate
                        # the same callback; merge only across declarations.
                        if row not in rows:
                            rows.append(row)
    return {'animations': result, 'controls': controls}


def prepare_interactions(selection: dict, category: str, obj: GameObject, *,
                         model_stem: str, mesh_names: list[str], clips: list[dict],
                         effect_resolver: Callable[[dict[str, str], int], dict] | None = None) -> dict:
    """Extend a verified static scene without guessing at missing controls.

    Unsupported individual actions retain explicit exclusions. Parsing failures
    disable this optional profile; they never turn eventful actions into bare
    skeletal clips. Default masks and the previously validated rest stay intact.
    """
    preferred = list(dict.fromkeys([*selection['clip_names'],
        *(row['name'] for row in selection['excluded_clips'] if row['reason'] in
          {'native_events_not_supported', 'ponybook_native_events_not_supported'})]))
    if category != 'Pony':
        return {**selection, 'clip_names': list(dict.fromkeys([*selection['clip_names'], *(clip['name'] for clip in clips)])),
                'preferred_animations': preferred}
    result = {**selection, 'clip_names': list(selection['clip_names']),
              'excluded_clips': list(selection['excluded_clips']),
              'provenance': dict(selection['provenance'])}
    diagnostics = []
    paths = list(dict.fromkeys([path for path in [selection.get('event_path'), *selection.get('additional_event_paths', [])] if path]))
    documents = [(path, read_events(path)) for path in paths]
    evidence = selection['provenance']
    if evidence.get('display', {}).get('selected') == 'ponybook-default':
        eye_names = {state: [evidence['ponybook']['eyes'][state]] for state in STATES}
        enabled = evidence['ponybook']['eye_controller_enabled']
    else:
        source = evidence.get('eyes', {})
        eye_names = source.get('groups', {state: [] for state in STATES})
        if source.get('resolved_native_controller'):
            eye_names = {state: [source['resolved_native_controller'][state]] for state in STATES}
        enabled = source.get('enabled', True)
    eyes = {state: [i for i, name in enumerate(mesh_names) if enabled and name in eye_names.get(state, [])] for state in STATES}
    all_eyes = set().union(*map(set, eyes.values()))
    visible = set(selection['visible_meshes'])
    # Never change a verified default appearance merely to make blinking work.
    blink = bool(eyes['Open'] and eyes['Closed'] and not set(eyes['Open']) & set(eyes['Closed'])
                 and {i for i in all_eyes if mesh_names[i] in visible} == set(eyes['Open']))
    if not blink:
        diagnostics.append({'code': 'blink-unavailable', 'reason': 'Missing, overlapping, or nondefault native Open/Closed eye meshes'})
    try:
        requested_clicks = native_click_names(obj)
    except InteractionError as error:
        requested_clicks = []
        diagnostics.append({'code': 'click-unavailable', 'reason': str(error)})
    available = {clip['name']: clip for clip in clips}
    candidates = list(dict.fromkeys([*selection['clip_names'],
        *(row['name'] for row in selection['excluded_clips'] if row['reason'] in {'native_events_not_supported', 'ponybook_native_events_not_supported'}),
        *requested_clicks, 'arena_pony_defeat', *(clip['name'] for clip in clips)]))
    retained = set(selection['visible_meshes']) | {mesh_names[i] for i in all_eyes}
    events, selected, approvals = {}, [], {}
    resolved_paths = {}
    supported_types = {'EyeSet', 'Emitter', 'SubObject'}
    subobjects = set()
    for name in candidates:
        if name not in available:
            if name in requested_clicks or name == 'arena_pony_defeat':
                diagnostics.append({'code': 'native-action-unavailable', 'name': name, 'reason': 'Absent from validated associated CSV'})
            continue
        raw_events = []
        for path, document in documents:
            for row in document['animations'].get(name, []):
                if row not in raw_events:
                    raw_events.append(row)
        count = available[name]['end'] - available[name]['start'] + 1
        compiled, needed, reason, keys = [], set(), None, {}
        for row in sorted(raw_events, key=lambda item: item['frame']):
            frame, kind, attrs = row['frame'], row['type'], row['attributes']
            if kind not in supported_types:
                reason = 'unsupported-native-event:' + kind; break
            # Keep unsupported native boundary behavior explicit at this stage.
            if frame >= count:
                reason = 'native-event-frame-outside-clip'; break
            if kind == 'EyeSet':
                if set(attrs) != {'Open', 'EnableBlink'} or attrs['Open'] not in {*STATES, 'None'} or attrs['EnableBlink'] not in {'0', '1'}:
                    reason = 'invalid-native-eye-event'; break
                state = attrs['Open']
                if all_eyes and state != 'None' and not eyes[state]:
                    reason = 'missing-native-eye-state:' + state; break
                event = {'frame': frame, 'type': kind, 'state': state, 'enableBlink': attrs['EnableBlink'] == '1'}
                needed |= {mesh_names[i] for i in all_eyes}
            elif kind == 'SubObject':
                try:
                    bindings = [compile_subobject_event(attrs, frame, document['controls'], model_stem, mesh_names)
                        for _, document in documents if row in document['animations'].get(name, [])]
                    event = bindings[0]
                    if any(binding != event for binding in bindings[1:]):
                        raise VisibilityError('conflicting-native-subobject-sources')
                except VisibilityError as error:
                    reason = str(error); break
                needed |= {mesh_names[i] for i in event['meshes']}
            else:
                if effect_resolver is None:
                    reason = 'unsupported-native-event:Emitter'; break
                try:
                    event = effect_resolver(attrs, frame)
                except ValueError as error:
                    reason = 'unsupported-native-emitter:' + str(error); break
            targets = event['meshes'] if kind == 'SubObject' else [event.get('effect')]
            event_keys = [(event['frame'], kind, target) for target in targets]
            value = event['visible'] if kind == 'SubObject' else event
            if any(key in keys and keys[key] != value for key in event_keys):
                reason = 'conflicting-native-event'; break
            keys.update((key, value) for key in event_keys)
            if event not in compiled: compiled.append(event)
        if reason:
            diagnostics.append({'code': 'native-action-excluded', 'name': name, 'reason': reason, 'events': raw_events})
            result['excluded_clips'] = [row for row in result['excluded_clips'] if row['name'] != name]
            result['excluded_clips'].append({'name': name, 'reason': reason})
            continue
        selected.append(name)
        if compiled: events[name] = compiled
        retained |= needed
        subobjects.update(i for event in compiled if event['type'] == 'SubObject' for i in event['meshes'])
        for path, _ in documents:
            if path not in resolved_paths:
                resolved_paths[path] = path.resolve()
            approvals.setdefault(resolved_paths[path], []).append(name)
    click_names = [name for name in requested_clicks if name in selected]
    profile = {'version': 1, 'clickAnimations': click_names}
    model_profile = {'version': 1}
    if all_eyes or any(event['type'] == 'EyeSet' for rows in events.values() for event in rows):
        model_profile['eyes'] = eyes
    if subobjects:
        model_profile['subObjects'] = sorted(subobjects)
    result['clip_names'] = selected
    result['preferred_animations'] = [name for name in preferred if name in selected]
    result['excluded_clips'] = [row for row in result['excluded_clips'] if row['name'] not in selected]
    result['provenance']['clip_scope'] = 'all valid clips in the verified native library; unsupported events explicitly excluded'
    if 'arena_pony_defeat' in selected:
        profile['dizzyAnimation'] = 'arena_pony_defeat'
    if not (blink or click_names or events or 'dizzyAnimation' in profile):
        result['provenance']['interactions'] = {'diagnostics': diagnostics, 'nativeClickCandidates': requested_clicks}
        return result
    result.update(clip_names=selected, retained_meshes=[name for name in dict.fromkeys(mesh_names) if name in retained],
                  interactions=profile, model_interactions=model_profile, animation_events=events, event_approvals=approvals)
    result['excluded_clips'] = [row for row in result['excluded_clips'] if row['name'] not in selected]
    result['provenance']['interactions'] = {'diagnostics': diagnostics, 'nativeClickCandidates': requested_clicks,
        'sources': [path.name for path, _ in documents],
        'clickPolicy': 'viewer randomly chooses from native allowed branches; remote-service gate and directional choice are not simulated'}
    return result
