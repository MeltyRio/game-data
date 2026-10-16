"""Shared animation references in the public 3D catalogue."""
import json


def compact_animations(catalogue: dict) -> dict:
    """Intern exact references, including event bindings, without changing GLBs.

    Event bindings remain part of each model's selection. Only identical
    bindings share a table row; the skeletal GLB can be shared independently.
    """
    assets: list[dict] = []
    indices: dict[str, int] = {}

    def actor(model: dict) -> dict:
        references = []
        for animation in model['animations']:
            key = json.dumps(animation, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if key not in indices:
                indices[key] = len(assets)
                assets.append(animation)
            references.append(indices[key])
        result = {**model, 'animations': references}
        if 'additionalActor' in model:
            child = model['additionalActor']
            result['additionalActor'] = {**child, 'model': actor(child['model'])}
        return result

    models = {identity: actor(model) for identity, model in catalogue['models'].items()}
    return {**catalogue, 'schemaVersion': 4, 'animationAssets': assets, 'models': models}


def animation_assets(catalogue: dict) -> list[dict] | None:
    """Read the optional schema-4 table; inline legacy catalogues stay valid."""
    if 'animationAssets' not in catalogue:
        return None
    assets = catalogue['animationAssets']
    if (catalogue.get('schemaVersion') != 4 or not isinstance(assets, list)
            or any(not isinstance(asset, dict) for asset in assets)):
        raise ValueError('Invalid shared animation table')
    return assets


def resolve_animation(reference: object, assets: list[dict] | None) -> dict:
    if type(reference) is int and assets is not None and 0 <= reference < len(assets):
        return assets[reference]
    if isinstance(reference, dict):
        return reference
    raise ValueError('Invalid animation reference or table index')
