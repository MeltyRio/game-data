"""Exact native SubObject targets shared by selection and source verification."""


class VisibilityError(ValueError):
    pass


def compile_subobject_event(attributes: dict[str, str], frame: int,
                            controls: dict[tuple[str, str], tuple[str, str]],
                            model_stem: str, mesh_names: list[str]) -> dict:
    """Bind only this model's declared ID to all exact-name native meshes.

    An ID declared for another model is not a fallback. Missing geometry stays
    unsupported, including a hidden default that would otherwise be a no-op.
    """
    if set(attributes) != {'ID', 'Show'} or not attributes['ID'] or attributes['Show'] not in {'0', '1'}:
        raise VisibilityError('invalid-native-subobject-event')
    target = controls.get((model_stem, attributes['ID']))
    if target is None:
        raise VisibilityError('unresolved-native-subobject:' + attributes['ID'])
    mesh, default = target
    if not mesh or default not in {'0', '1'}:
        raise VisibilityError('invalid-native-subobject-control')
    indices = [index for index, name in enumerate(mesh_names) if name == mesh]
    if not indices:
        raise VisibilityError('missing-native-subobject-mesh:' + mesh)
    return {'frame': frame, 'type': 'SubObject', 'meshes': indices,
            'visible': attributes['Show'] == '1'}
