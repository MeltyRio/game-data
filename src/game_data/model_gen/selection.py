"""Select a conservative basic viewer scene from explicit native declarations.

This does not parse RK geometry, discover alternate resources, execute events,
attest that an extracted directory is complete.
The first declared idle is a documented viewer policy. With no declaration,
the reviewed native RK loader selects its own initial static pose.
"""
from pathlib import Path
import hashlib
import re
from xml.etree import ElementTree as ET

from .wardrobe import WardrobeError, resolve_wardrobe


class SelectionError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


def _fail(code, detail):
    raise SelectionError(code, detail)


def _strings(value, field):
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        _fail('invalid_declaration', field + ' must be a string array')
    return [v for v in value if v]


def _event_file(folder, stem):
    if (not isinstance(stem, str) or not stem or stem in {'.', '..'}
            or any(c in stem for c in '/\\\0') or any(ord(c) < 32 for c in stem)):
        _fail('invalid_event_source', 'Unsafe or missing event resource stem')
    # Native AnimationEventsXML holds a stem; do not guess away an extension.
    if stem.endswith('.xml'):
        _fail('unsupported_event_stem', 'Expected native event stem without .xml')
    name = stem + '.xml'
    path = folder / name
    if not path.is_file():
        _fail('missing_event_source', 'Missing extracted event XML: ' + name)
    if path.resolve().parent != folder.resolve():
        _fail('invalid_event_source', 'Event file leaves the extracted directory')
    if [p.name for p in folder.iterdir() if p.name.casefold() == name.casefold()] != [name]:
        _fail('ambiguous_event_source', 'Event filename is not unique and exact: ' + name)
    return path


def _events(path, model_stem, mesh_names):
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8-sig')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
            _fail('invalid_event_source', 'DTD/entity declarations are unsupported')
        text = re.sub(r'^\s*<\?xml[^>]*\?>', '', text)
        root = ET.fromstring('<NativeEvents>' + text + '</NativeEvents>')
    except (UnicodeError, ET.ParseError) as error:
        _fail('invalid_event_source', str(error))
    defaults, eventful, absent_hidden = {}, set(), set()
    controls = {}
    for section in root:
        if section.tag not in {'MeshList', 'AnimationList'} or section.attrib:
            _fail('unsupported_event_source', 'Unknown event XML section or attributes')
        if section.tag == 'AnimationList':
            for animation in section:
                name = animation.get('Name')
                if animation.tag != 'Animation' or not name:
                    _fail('invalid_event_source', 'Malformed animation event declaration')
                # Union repeated declarations: any event makes the whole clip
                # unsupported, including events whose target is another model.
                if len(animation) or set(animation.attrib) != {'Name'}:
                    eventful.add(name)
        else:
            for model in section:
                if model.tag != model_stem:
                    continue
                if model.attrib:
                    _fail('unsupported_mesh_defaults', 'Unknown model defaults attributes')
                for item in model:
                    if (item.tag != 'SubObject' or set(item.attrib) != {'ID', 'Name', 'DefaultVisible'}
                            or len(item) or (item.text or '').strip()):
                        _fail('unsupported_mesh_defaults', 'Unknown SubObject declaration')
                    target, mesh, flag = item.get('ID'), item.get('Name'), item.get('DefaultVisible')
                    if not target or not mesh or flag not in {'0', '1'}:
                        _fail('invalid_mesh_defaults', 'Invalid SubObject ID/name/visibility')
                    value = flag == '1'
                    if target in controls and controls[target] != (mesh, value):
                        _fail('conflicting_mesh_defaults', 'Conflicting SubObject ID: ' + target)
                    if mesh in defaults and defaults[mesh] != value:
                        _fail('conflicting_mesh_defaults', 'Conflicting mesh visibility: ' + mesh)
                    if mesh not in mesh_names:
                        if value:
                            _fail('missing_controlled_mesh', 'Default-visible SubObject absent from RK: ' + mesh)
                        # Native exact-name hide is a no-op when the mesh is absent.
                        # Event-bearing clips remain excluded below.
                        absent_hidden.add(mesh)
                    controls[target] = (mesh, value)
                    defaults[mesh] = value
    if not any(section.tag == 'AnimationList' for section in root):
        _fail('incomplete_event_source', 'Event XML has no AnimationList')
    return defaults, eventful, hashlib.sha256(raw).hexdigest(), sorted(absent_hidden)


def _eyes(obj, game_objects, mesh_names, *, allow_single_mesh_noop=False):
    data = obj.get('Eyes')
    source = 'Eyes'
    known_fields = {'EyeSubObject_' + state for state in ('Open', 'Closed', 'Happy', 'Frown')}
    schema = getattr(game_objects, 'CATEGORY_DATA', {}).get('Pony', {}).get('Eyes', {}).get('attributes', {})
    for field in {*schema, *(data or {})}:
        if field.startswith('EyeSubObject_') and field not in known_fields:
            _fail('unsupported_eye_state', 'Unknown native eye state: ' + field)
    if data is None:
        source = 'CATEGORY_DATA.Pony.Eyes'
        data = {}
        for state in ('Open', 'Closed', 'Happy', 'Frown'):
            field = 'EyeSubObject_' + state
            default = schema.get(field, {}).get('default')
            if not isinstance(default, str) or not default:
                _fail('missing_eye_defaults', 'Missing native category default: ' + field)
            data[field] = [default]
    groups = {state: set(_strings(data.get('EyeSubObject_' + state, []), 'Eyes.' + state))
              for state in ('Open', 'Closed', 'Happy', 'Frown')}
    if not groups['Open']:
        # The native Pony config leaves an absent/empty Open string disabled.
        # Initialization, blink and event EyeSet paths all honor that flag, so
        # other declared eye states do not hide RK's initially visible meshes.
        return set(), set(), {'field': source,
                              'groups': {k: sorted(v) for k, v in groups.items()},
                              'enabled': False,
                              'rule': 'reviewed-empty-Open-disables-eye-controller'}
    opened = groups['Open'].intersection(mesh_names)
    if not opened:
        # Native exact-name show/hide does nothing when lookup fails. Restrict
        # this proof to one actual mesh without wardrobe: check a superset of
        # first-Item names, the native literal lod0 -> lod1 substitution, and
        # constructor defaults, without choosing a replacement eye mesh.
        names = set().union(*groups.values())
        candidates = names | {name.replace('lod0', 'lod1', 1) for name in names}
        candidates |= {'eyes_open', 'eyes_shut', 'eyes_happy', 'eyes_frown'}
        if allow_single_mesh_noop and not candidates.intersection(mesh_names):
            return set(), set(), {'field': source,
                                  'groups': {k: sorted(v) for k, v in groups.items()},
                                  'lookup_candidates': sorted(candidates),
                                  'rule': 'reviewed-single-mesh-eye-lookups-no-op'}
        _fail('missing_open_eye_mesh', 'No declared Open eye mesh exists in this RK')
    controlled = set().union(*groups.values()).intersection(mesh_names)
    provenance = {'field': source, 'groups': {k: sorted(v) for k, v in groups.items()}}
    # The native Pony configuration reads the first Item of each eye state,
    # then replaces the first literal 'lod0' with 'lod1'. Only add its initial
    # hide operations when the entire controller resolves exactly and its Open
    # agrees with this static preview's single declared Open mesh.
    native_groups = {
        state: (data.get('EyeSubObject_' + state, [''])[0]
                if data.get('EyeSubObject_' + state) else '').replace('lod0', 'lod1', 1)
        for state in groups
    }
    native_names = set(native_groups.values())
    if (len(native_names) == 4 and '' not in native_names
            and native_names <= mesh_names and opened == {native_groups['Open']}):
        added_hidden = native_names - controlled - opened
        if added_hidden:
            controlled |= native_names
            provenance['resolved_native_controller'] = native_groups
            provenance['added_hidden_meshes'] = sorted(added_hidden)
            provenance['rule'] = 'reviewed-complete-first-Item-controller-initial-hide'
    return opened, controlled, provenance


def _select_world_view(category, obj, game_objects, *, model_stem, mesh_names, clips, extracted_dir, outfit_id=None, csv_first_name=None, csv_excluded_names=None, allow_additional_actor=False):
    """Return explicit visible meshes, rest, and event-free declared clip names.

    `clips` is the already validated ordered CSV list of name/start/end/fps.
    No later declared idle or later valid CSV row is substituted. For a Pony
    name absent from a completely audited CSV, native lookup selects original
    row zero. With no Pony idle declaration, native RK loading queries "default"
    first, then uses that same row-zero lookup. This is a loaded static pose,
    not the later AI's choice of idle. csv_excluded_names distinguishes absence
    from rejected bad rows.
    """
    if category not in {'Pony', 'PonyPet'}:
        _fail('unsupported_category', 'No basic-view selection policy for ' + str(category))
    if not isinstance(mesh_names, list) or not mesh_names or any(not isinstance(n, str) or not n for n in mesh_names):
        _fail('invalid_mesh_names', 'Expected nonempty RK mesh names')
    declared, sources = [], {}
    def add(name, field):
        if name:
            if not isinstance(name, str):
                _fail('invalid_declaration', 'Animation name is not a string: ' + field)
            if name not in sources:
                declared.append(name)
                sources[name] = []
            sources[name].append(field)
    provenance, wardrobe = {}, None
    events_absent = False
    if category == 'Pony':
        if obj.get('Model', {}).get('MediumLOD') != model_stem:
            _fail('model_declaration_mismatch', 'RK stem differs from Model.MediumLOD')
        additional = obj.get('AdditionalObject') or {}
        if (additional.get('Model') or additional.get('Animation')) and not allow_additional_actor:
            _fail('additional_object_not_supported', 'Actor requires an additional model/controller')
        try:
            wardrobe = resolve_wardrobe(obj, game_objects, outfit_id=outfit_id)
        except WardrobeError as error:
            _fail(error.code, str(error))
        animation = obj.get('Animation', {})
        for field, values in animation.items():
            if field.endswith('AnimationList'):
                _strings(values, 'Animation.' + field)
                for index, name in enumerate(values):
                    add(name, f'Animation.{field}[{index}]')
        conversation = obj.get('Conversation') or {}
        idle = conversation.get('IdleAnimation')
        if idle:
            rest_source = 'Conversation.IdleAnimation'
        else:
            left = _strings(animation.get('LeftIdleAnimationList', []), 'Animation.LeftIdleAnimationList')
            idle = left[0] if left else None
            rest_source = 'Animation.LeftIdleAnimationList[first nonempty]'
        add(conversation.get('IdleAnimation'), 'Conversation.IdleAnimation')
        add(conversation.get('InteractionAnimation'), 'Conversation.InteractionAnimation')
        event_stem = animation.get('AnimationEventsXML')
        event_source = {'context': 'world-actor-preview'}
        if event_stem:
            event_rule = 'Animation.AnimationEventsXML + .xml'
        else:
            special_ai = (obj.get('AI') or {}).get('Special_AI')
            # Luna reads the native int field. Unknown future kinds must be
            # reviewed rather than inheriting today's world-actor fallback.
            if type(special_ai) is not int or special_ai not in range(6):
                _fail('unknown_special_ai', 'Empty event source requires a known native AI.Special_AI integer (0–5)')
            event_source['special_ai'] = special_ai
            if special_ai == 2:
                # Native 33cad8 clears both controller and MeshList pointers
                # before this branch; later visibility/event callbacks honor
                # the null pointers. No local-file absence is inferred.
                events_absent = True
                event_rule = 'reviewed-world-actor-Special_AI-2-bypass'
            else:
                event_stem = 'pony_type01'
                event_rule = 'reviewed-world-actor-pony_type01-default'
        provenance['events'] = {'file': None, 'sha256': None,
                                'rule': event_rule, **event_source}
        opened, controlled, provenance['eyes'] = _eyes(obj, game_objects, set(mesh_names),
            allow_single_mesh_noop=wardrobe is None and len(mesh_names) == 1)
        visible = set(mesh_names) - controlled | opened
        if wardrobe:
            wardrobe_meshes = set(wardrobe['mesh_visibility'])
            if controlled.intersection(wardrobe_meshes):
                _fail('eye_wardrobe_order_unverified', 'Eye meshes also have wardrobe controls')
            for mesh, shown in wardrobe['mesh_visibility'].items():
                if shown and mesh in mesh_names:
                    visible.add(mesh)
                elif not shown:
                    visible.discard(mesh)
            provenance['wardrobe'] = wardrobe['provenance']
            provenance['wardrobe']['absent_declared_meshes'] = sorted(wardrobe_meshes - set(mesh_names))
    else:
        data = obj.get('Object', {})
        if data.get('Model') != model_stem:
            _fail('model_declaration_mismatch', 'RK stem differs from Object.Model')
        for field in ('AnimationIdle', 'AnimationWalk', 'AnimationRun', 'AnimationEditorIdle'):
            add(data.get(field), 'Object.' + field)
        if outfit_id is not None:
            _fail('unsupported_category', 'A PonySet cannot be applied to a PonyPet')
        idle, rest_source = data.get('AnimationIdle'), 'Object.AnimationIdle'
        if not idle:
            # Caller supplies the original first CSV row, before invalid rows
            # are filtered. Never choose clips[0] from a filtered valid list.
            if not isinstance(csv_first_name, str) or not csv_first_name:
                _fail('original_first_csv_clip_unverified', 'Empty pet idle requires the validated original first CSV row')
            idle, rest_source = csv_first_name, 'Object.AnimationIdle empty -> original CSV row 0'
        controlled, visible = {'eyes_shut'}, set(mesh_names) - {'eyes_shut'}
        provenance['eyes'] = {'rule': 'reviewed-pet-editor-exact-eye-names',
                              'hidden': ['eyes_shut'], 'shown_if_present': ['eyes_open'],
                              'context': 'viewer static display uses native editor eye policy; rest uses declared actor Idle'}
        # Pet initialization marks the actor +0x3e8; native Pony event setup
        # returns immediately for that marker. This is positive controller
        # evidence, not an inference from a locally absent XML resource.
        events_absent = True
        provenance['events'] = {'file': None, 'sha256': None,
                                'rule': 'reviewed-pet-marker-bypasses-Pony-event-setup'}
    loader_pose = category == 'Pony' and not idle
    audited = (isinstance(csv_excluded_names, list)
               and all(isinstance(name, str) for name in csv_excluded_names))
    if loader_pose:
        # Native RK drawable load queries the literal "default", evaluates its
        # first frame, then stops that layer. Rejected CSV declarations are not
        # a native name miss and must never activate the row-zero fallback.
        if not audited or 'default' in csv_excluded_names:
            _fail('native_initial_pose_unverified', 'Native RK initial pose requires a complete audit with no rejected default clip')
        idle, rest_source = 'default', 'native RK drawable loader'
    if not idle:
        _fail('initial_pose_not_declared', 'No explicit idle candidate for the viewer')
    requested_idle = idle
    rest_rule = 'reviewed-native-RK-load-default-clip' if loader_pose else None
    clip_index = {}
    for clip in clips:
        name = clip['name']
        if name in clip_index and clip_index[name] != clip:
            _fail('conflicting_csv_clip', 'Conflicting validated CSV clip: ' + name)
        clip_index[name] = clip
    if idle not in clip_index:
        # Native named lookup falls back to the first library's first clip only
        # when the name was not found. A strict-parser rejection is not absence.
        if (category != 'Pony' or not audited or idle in csv_excluded_names
                or not isinstance(csv_first_name, str) or csv_first_name not in clip_index
                or csv_first_name in csv_excluded_names):
            _fail('native_initial_pose_unverified' if loader_pose else 'declared_idle_not_in_csv',
                  'Initial clip is absent or rejected; native CSV row-zero fallback is unverified: ' + idle)
        idle = csv_first_name
        rest_rule = ('reviewed-native-RK-load-default-miss-to-original-CSV-row-0' if loader_pose
                     else 'reviewed-native-name-lookup-miss-to-original-CSV-row-0')
    add(idle, rest_source if idle == requested_idle else rest_source + ' -> original CSV row 0')
    path, defaults, eventful, absent_hidden = None, {}, set(), []
    if category == 'Pony' and not events_absent:
        path = _event_file(Path(extracted_dir), event_stem)
        defaults, eventful, digest, absent_hidden = _events(path, model_stem, set(mesh_names))
        provenance['events'].update(file=path.name, sha256=digest)
    if wardrobe and set(wardrobe['mesh_visibility']).intersection(defaults):
        _fail('wardrobe_subobject_order_unverified', 'Wardrobe meshes also have SubObject defaults')
    if controlled.intersection(defaults):
        _fail('eye_subobject_order_unverified', 'Eye meshes also have SubObject defaults; controller order requires review')
    if idle in eventful:
        _fail('initial_pose_requires_events', 'Initial idle requires unsupported native events: ' + idle)
    for mesh, shown in defaults.items():
        if shown:
            visible.add(mesh)
        else:
            visible.discard(mesh)
    if not visible:
        _fail('no_visible_meshes', 'Native static selection hides every mesh')
    excluded, selected = [], []
    for name in declared:
        if name not in clip_index:
            excluded.append({'name': name, 'reason': 'declared_clip_not_in_csv', 'fields': sources[name]})
        elif name in eventful:
            excluded.append({'name': name, 'reason': 'native_events_not_supported', 'fields': sources[name]})
        else:
            selected.append(name)
    provenance.update({'rest': {'field': rest_source, 'name': idle,
                               'context': 'viewer initial static pose; not a claim about game initialization'},
                       'mesh_defaults': defaults, 'absent_default_hidden_meshes': absent_hidden,
                       'clip_fields': sources,
                       'clip_scope': 'explicit object references only; event-bearing clips excluded'})
    if rest_rule:
        provenance['rest'].update(requested_name=requested_idle, rule=rest_rule)
    if loader_pose:
        provenance['rest'].update(
            context='native RK drawable load static pose; not the later AI idle state',
            default_clip_present='default' in clip_index,
            original_csv_first_name=csv_first_name)
        provenance['clip_scope'] = 'explicit object references and native loader initial clip only; event-bearing clips excluded'
    return {'visible_meshes': list(dict.fromkeys(n for n in mesh_names if n in visible)),
            'clip_names': selected, 'rest_name': idle, 'event_path': path,
            'events_absent': events_absent,
            'provenance': provenance, 'excluded_clips': excluded}


def _ponybook_view(world, *, obj, game_objects, model_stem, mesh_names, clips,
                   extracted_dir, outfit_id, csv_first_name, csv_excluded_names, **unused):
    """Compile a public default appearance, retaining verified town actions.

    No player equipment or alternate owner is inferred. Display the first frame
    of the resolved native Conversation idle, without autoplay. Keep the loader
    constructor clip available and validate both event contexts independently.
    Missing dependencies remain errors.
    """
    animation = obj.get('Animation') or {}
    path = _event_file(Path(extracted_dir), animation.get('Rig_MediumLOD'))
    defaults, eventful, digest, absent_hidden = _events(path, model_stem, set(mesh_names))
    # Reuse Luna's ordered fields; native config consumes Item zero, then
    # changes only the first literal lod0 to lod1. A later Item is not a fallback.
    data = obj.get('Eyes')
    if data is None:
        schema = getattr(game_objects, 'CATEGORY_DATA', {}).get('Pony', {}).get('Eyes', {}).get('attributes', {})
        data = {'EyeSubObject_' + state: [schema.get('EyeSubObject_' + state, {}).get('default', '')]
                for state in ('Open', 'Closed', 'Happy', 'Frown')}
    names = {}
    for state in ('Open', 'Closed', 'Happy', 'Frown'):
        values = data.get('EyeSubObject_' + state, [])
        _strings(values, 'Eyes.' + state)
        names[state] = (values[0] if values else '').replace('lod0', 'lod1', 1)
    visible = set(mesh_names)
    if names['Open']:
        if names['Open'] not in visible:
            _fail('ponybook_eye_policy_unverified', 'Native first-Item Open is absent from the selected RK')
        visible -= {names[state] for state in ('Closed', 'Happy', 'Frown')}
        visible.add(names['Open'])
    # Native order is Eyes, MeshList, then the selected wardrobe. Use the
    # existing wardrobe policy (default / explicitly requested complete outfit).
    for mesh, shown in defaults.items():
        if shown: visible.add(mesh)
        else: visible.discard(mesh)
    wardrobe = resolve_wardrobe(obj, game_objects, outfit_id=outfit_id)
    if wardrobe:
        for mesh, shown in wardrobe['mesh_visibility'].items():
            if shown and mesh in mesh_names: visible.add(mesh)
            elif not shown: visible.discard(mesh)
    # Native config misses can leave extra expression layers visible. Keep a
    # previously verified world preview until the complete alternate/owned
    # display context is known, rather than silently changing its appearance.
    if visible != set(world['visible_meshes']):
        _fail('ponybook_visibility_unverified', 'PonyBook and verified world default visibility differ')
    conversation = obj.get('Conversation')
    requested = 'gen_idle_00' if conversation is None else conversation.get('IdleAnimation')
    if not isinstance(requested, str) or not requested or '\0' in requested:
        _fail('ponybook_idle_unverified', 'Expected one explicit native PonyBook idle name')
    valid = {clip['name'] for clip in clips}
    rejected = csv_excluded_names
    if (not isinstance(rejected, list) or any(not isinstance(n, str) for n in rejected)
            or requested in rejected):
        _fail('ponybook_idle_unverified', 'PonyBook idle requires a complete, non-rejected CSV declaration')
    def resolve(name):
        if name in rejected:
            _fail('ponybook_idle_unverified', 'Rejected native pose/idle cannot use a name-miss fallback: ' + name)
        if name in valid:
            return name, 'exact-native-name'
        if not isinstance(csv_first_name, str) or csv_first_name not in valid or csv_first_name in rejected:
            _fail('ponybook_idle_unverified', 'Original native row-zero fallback is unverified')
        return csv_first_name, 'native-name-miss-to-original-CSV-row-zero'
    constructor, constructor_rule = resolve('default')
    idle, idle_rule = resolve(requested)
    world_events = set()
    if world['event_path'] is not None:
        _, world_events, _, _ = _events(world['event_path'], model_stem, set(mesh_names))
    all_events = eventful | world_events
    if constructor in all_events:
        _fail('ponybook_idle_requires_events', 'PonyBook constructor pose requires unsupported events: ' + constructor)
    if idle in all_events:
        _fail('ponybook_idle_requires_events', 'PonyBook display idle requires unsupported events: ' + idle)
    selected = [name for name in world['clip_names'] if name not in eventful]
    excluded = list(world['excluded_clips'])
    for name in world['clip_names']:
        if name in eventful:
            excluded.append({'name': name, 'reason': 'ponybook_native_events_not_supported',
                             'fields': world['provenance']['clip_fields'].get(name, [])})
    if constructor not in selected: selected.append(constructor)
    if idle not in selected: selected.append(idle)
    # Validate/hash both sources in the converter too. A missing world event
    # controller does not imply that a present PonyBook XML can be ignored.
    extra = [] if world['event_path'] == path else [path]
    fields = {name: list(values) for name, values in world['provenance']['clip_fields'].items()}
    fields.setdefault(constructor, []).append('native ConversationPony constructor: default / original CSV row zero')
    fields.setdefault(idle, []).append('PonyBook.Conversation.IdleAnimation')
    idle_field = 'Conversation.IdleAnimation' if conversation is not None else 'native absent-Conversation default'
    provenance = {**world['provenance'], 'clip_fields': fields,
        'display': {'requested': 'ponybook-default', 'selected': 'ponybook-default',
                    'context': 'public default appearance; no player state or native UI controller'},
        'ponybook': {'event_file': path.name, 'event_sha256': digest,
                     'mesh_defaults': defaults, 'absent_default_hidden_meshes': absent_hidden,
                     'eyes': names, 'eye_controller_enabled': bool(names['Open']),
                     'visibility_order': ['Eyes', 'MeshList', 'wardrobe'],
                     'world_visibility_equivalent': True,
                     'constructor': {'requested_name': 'default', 'name': constructor,
                                     'rule': constructor_rule,
                                     'field': 'native ConversationPony RK drawable constructor'},
                     'idle': {'requested_name': requested, 'name': idle, 'rule': idle_rule,
                              'playable': True, 'field': idle_field}},
        'rest': {'field': idle_field, 'requested_name': requested, 'name': idle, 'rule': idle_rule,
                 'context': 'PonyBook display idle first frame; no autoplay'},
        'clip_scope': 'verified object-declared town/Conversation actions plus native constructor pose/PonyBook idle; both event contexts checked'}
    return {**world, 'rest_name': idle, 'clip_names': selected, 'additional_event_paths': extra,
            'provenance': provenance, 'excluded_clips': excluded}


def select_native_view(category, obj, game_objects, *, model_stem, mesh_names, clips,
                       extracted_dir, outfit_id=None, csv_first_name=None,
                       csv_excluded_names=None, allow_additional_actor=False,
                       display_profile='world-preview'):
    """Prefer a verified PonyBook appearance without discarding a valid world view.

    Explicit world-preview preserves the original API. ponybook-default is
    strict. Normal generation uses ponybook-preferred: an unverified book
    context retains a separately validated world view and records why. Errors
    in the world source, association or converter are never swallowed here.
    """
    if display_profile not in {'world-preview', 'ponybook-default', 'ponybook-preferred'}:
        _fail('invalid_display_profile', 'Unknown 3D display profile')
    args = dict(category=category, obj=obj, game_objects=game_objects,
                model_stem=model_stem, mesh_names=mesh_names, clips=clips,
                extracted_dir=extracted_dir, outfit_id=outfit_id,
                csv_first_name=csv_first_name, csv_excluded_names=csv_excluded_names,
                allow_additional_actor=allow_additional_actor)
    world = _select_world_view(**args)
    if display_profile == 'world-preview':
        return world
    if category != 'Pony':
        world['provenance']['display'] = {'requested': display_profile, 'selected': 'pet-editor-preview'}
        return world
    try:
        selected = _ponybook_view(world, **args)
        selected['provenance']['display']['requested'] = display_profile
        return selected
    except SelectionError as error:
        if display_profile == 'ponybook-default': raise
        world['provenance']['display'] = {
            'requested': display_profile, 'selected': 'world-preview',
            'ponybook_unavailable': {'code': error.code, 'reason': str(error)}}
        return world
