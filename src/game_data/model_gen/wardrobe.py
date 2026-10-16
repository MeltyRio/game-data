"""Resolve the reviewed native default wardrobe and a requested complete outfit.

Retained 11.4.2a ARM64 evidence covers ordered Sets, direct SubSets, three
part slots, and one LinkedPart. It does not establish deeper nesting.
No model, owner, part or mesh is inferred from a resource name.
"""
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from luna_kit.gameobjectdata import GameObject, GameObjectData

SLOTS = ('Body', 'Mane', 'Tail')


class WardrobeError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


def _fail(code: str, detail: str) -> NoReturn:
    raise WardrobeError(code, detail)


def _array(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _fail('invalid_wardrobe_declaration', field + ' must be a string array')
    return value


def registered_sets(owner: GameObject, sets: dict[str, GameObject]) -> list[str]:
    """Pony.Sets stops at its first empty item; each direct SubSets is retained.

    The native SubSets parser has no matching empty-item terminator. An empty
    SubSets reference is therefore unresolved, not permission to skip entries.
    """
    result = []
    for identity in _array(owner.get('Sets', {}).get('Sets', []), 'Sets.Sets'):
        if not identity:
            break
        if identity not in sets:
            _fail('missing_wardrobe_set', 'Missing PonySet: ' + identity)
        children = _array(sets[identity].get('PonySet', {}).get('SubSets', []), 'PonySet.SubSets')
        for child in children:
            if child == identity:
                _fail('cyclic_wardrobe_sets', 'Cyclic SubSets: ' + identity)
            if not child or child not in sets:
                _fail('missing_wardrobe_set', 'Missing SubSets reference: ' + repr(child))
            if _array(sets[child].get('PonySet', {}).get('SubSets', []), 'PonySet.SubSets'):
                _fail('nested_wardrobe_subsets_unverified', 'Nested SubSets requires native review: ' + child)
        for sid in [identity, *children]:
            if sid not in result:
                result.append(sid)
    return result


def resolve_set_parts(outfit: GameObject, parts: dict[str, GameObject]) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Resolve explicit Parts and exactly one LinkedPart, preserving slot types."""
    def part(pid: str, slot: str | None = None, parent: str | None = None) -> dict[str, str]:
        data = parts.get(pid, {}).get('PonyPart', {})
        actual, mesh = data.get('Type'), data.get('ModelName')
        if actual not in SLOTS or not isinstance(mesh, str) or not mesh:
            _fail('invalid_wardrobe_part', 'Missing/invalid PonyPart: ' + str(pid))
        if slot is not None and slot != actual:
            _fail('wardrobe_slot_mismatch', 'PonyPart Type differs from Parts slot: ' + str(pid))
        link = data.get('LinkedPart', '')
        if not isinstance(link, str):
            _fail('invalid_wardrobe_part', 'LinkedPart must be a string: ' + str(pid))
        if parent is not None and link:
            if link in {pid, parent}:
                _fail('cyclic_linked_part', 'Cyclic LinkedPart: ' + str(pid))
            _fail('nested_linked_part_unverified', 'More than one LinkedPart hop requires native review: ' + str(pid))
        return {'id': pid, 'slot': actual, 'mesh': mesh, 'linked_part': link,
                'field': 'PonyPart.ModelName'}

    raw = outfit.get('Parts')
    if not isinstance(raw, dict) or set(raw) - set(SLOTS):
        _fail('invalid_wardrobe_slots', 'Missing/unknown Parts slots')
    direct, expanded = {}, {}
    for slot in SLOTS:
        pid = raw.get(slot, '')
        if not isinstance(pid, str):
            _fail('invalid_wardrobe_part', 'Parts reference must be a string')
        if pid:
            direct[slot] = part(pid, slot)
    for item in direct.values():
        linked = [part(item['linked_part'], parent=item['id'])] if item['linked_part'] else []
        for candidate in [item, *linked]:
            slot = candidate['slot']
            if slot in expanded and expanded[slot]['id'] != candidate['id']:
                _fail('conflicting_wardrobe_parts', 'Conflicting LinkedPart for slot ' + slot)
            expanded[slot] = candidate
    if not direct:
        _fail('empty_wardrobe_parts', 'Outfit has no declared parts')
    return direct, expanded


def resolve_wardrobe(owner: GameObject, game_objects: GameObjectData, *,
                     outfit_id: str | None = None) -> dict | None:
    """Return slot choices and exact mesh controls, without reading RK files.

    A requested outfit starts from native default slots and applies its explicit
    parts plus each part's one linked part. Conflicting linked choices fail.
    This is a complete-outfit viewer policy, not a player's saved mixed outfit.
    """
    sets, parts = game_objects.get('PonySet', {}), game_objects.get('PonyPart', {})
    ids = registered_sets(owner, sets)
    if not ids:
        if outfit_id is not None:
            _fail('outfit_not_registered', 'Owner has no declared wardrobe')
        return None
    if outfit_id is not None and outfit_id not in ids:
        _fail('outfit_not_registered', 'Outfit is not registered by this owner: ' + str(outfit_id))
    used_parts, choices, controls = {}, {}, []
    for sid in ids:
        direct, expanded = resolve_set_parts(sets[sid], parts)
        choices[sid] = (direct, expanded)
        controls.extend(expanded.values())
        used_parts.update((item['id'], item) for item in expanded.values())
    defaults = choices[ids[0]][0]
    if set(defaults) != set(SLOTS):
        _fail('incomplete_default_wardrobe', 'First native set must supply all three default slots')
    selected = dict(defaults)
    if outfit_id is not None:
        selected.update(choices[outfit_id][1])
    visibility = {}
    for item in controls:
        shown = item['mesh'] == selected[item['slot']]['mesh']
        if item['mesh'] in visibility and visibility[item['mesh']] != shown:
            _fail('conflicting_wardrobe_mesh_visibility', 'Same mesh has incompatible slot visibility: ' + item['mesh'])
        visibility[item['mesh']] = shown
    return {'mesh_visibility': visibility,
            'provenance': {'set_ids': ids, 'part_ids': sorted(used_parts),
                           'default_set': ids[0], 'outfit_id': outfit_id,
                           'default_slots': {slot: item['id'] for slot, item in defaults.items()},
                           'selected_slots': {slot: item['id'] for slot, item in selected.items()},
                           'parts': used_parts,
                           'rule': 'first native set defaults; requested outfit explicit parts and one LinkedPart override defaults',
                           'context': 'complete-outfit viewer policy; no saved player equipment'}}
