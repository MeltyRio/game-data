"""Static GLBs and independent animations with optional native sampling data."""
import io
import json
import struct
import numpy as np
from PIL import Image
from .core import Model, Clip, require, local_trs, pose_matrices


class GLB:
    def __init__(self):
        self.document = {'asset': {'version': '2.0', 'generator': 'atp-model-converter/0.1.0.dev0'},
                         'buffers': [{'byteLength': 0}], 'bufferViews': [], 'accessors': []}
        self.binary = bytearray()

    def view(self, payload, target=None):
        self.binary.extend(b'\0' * (-len(self.binary) % 4))
        row = {'buffer': 0, 'byteOffset': len(self.binary), 'byteLength': len(payload)}
        if target is not None: row['target'] = target
        self.binary.extend(payload)
        self.document['bufferViews'].append(row)
        return len(self.document['bufferViews']) - 1

    def accessor(self, array, kind, component=5126, target=None, bounds=False):
        dtype = {5126: '<f4', 5123: '<u2', 5125: '<u4'}[component]
        values = np.asarray(array, dtype=dtype)
        require(len(values) > 0 and np.isfinite(values).all(), 'Empty/non-finite accessor')
        index = self.view(values.tobytes(), target)
        row = {'bufferView': index, 'componentType': component, 'count': len(values), 'type': kind}
        if bounds:
            flat = values.reshape(len(values), -1)
            row.update(min=flat.min(axis=0).tolist(), max=flat.max(axis=0).tolist())
        self.document['accessors'].append(row)
        return len(self.document['accessors']) - 1

    def finish(self):
        self.document['buffers'][0]['byteLength'] = len(self.binary)
        text = json.dumps(self.document, separators=(',', ':'), allow_nan=False).encode()
        text += b' ' * (-len(text) % 4)
        binary = bytes(self.binary) + b'\0' * (-len(self.binary) % 4)
        length = 12 + 8 + len(text) + 8 + len(binary)
        return struct.pack('<4sII', b'glTF', 2, length) + struct.pack('<I4s', len(text), b'JSON') + text + struct.pack('<I4s', len(binary), b'BIN\0') + binary


def rig_nodes(g, rig, rest=None, scale=1.0):
    require(len(rig.parents) == len(rig.names) > 0, 'Invalid rig')
    # Native upright is -Y. A proper rotation (not reflection) makes the GLB
    # itself Y-up, including in ordinary glTF viewers; no viewer-side flip.
    nodes = [{'name': 'ModelRoot', 'scale': [scale]*3, 'rotation': [1,0,0,0], 'children': []}]
    t, q = local_trs(rest, rig.parents) if rest is not None else (np.zeros((len(rig.parents), 3)), np.tile([0,0,0,1], (len(rig.parents), 1)))
    for i, parent in enumerate(rig.parents):
        require(-1 <= parent < i, 'Invalid rig hierarchy')
        # Matrix decomposition can round a unit component to 1 + one ULP;
        # glTF's JSON rotation schema requires each component within [-1, 1].
        nodes.append({'name': f'joint_{i:04}', 'translation': t[i].tolist(), 'rotation': np.clip(q[i], -1, 1).tolist()})
    for i, parent in enumerate(rig.parents):
        nodes[parent+1 if parent >= 0 else 0].setdefault('children', []).append(i+1)
    g.document.update(nodes=nodes, scenes=[{'nodes': [0]}], scene=0,
                      extras={'atpRig': {'version': 1, 'key': rig.key, 'parents': rig.parents,
                                         'jointNodes': list(range(1, len(rig.parents)+1)), 'coordinateSystem': 'native-Y-down-to-glTF-Y-up-RxPi'}})
    return nodes


def model_glb(model: Model) -> bytes:
    g = GLB(); count = len(model.positions); bone_count = len(model.rig.parents)
    require(bone_count <= 65536, 'Unsupported joint index range for UNSIGNED_SHORT')
    require(np.isfinite(model.scale) and model.scale > 0, 'Invalid model scale')
    require(model.positions.shape == (count,3) and model.texcoords.shape == (count,2), 'Invalid vertex arrays')
    require(model.joints.shape == model.weights.shape == (count,4), 'Invalid skin arrays')
    require(np.isfinite(model.weights).all() and (model.weights >= 0).all() and np.allclose(model.weights.sum(axis=1),1,atol=1e-6), 'Invalid normalized skin weights')
    require(np.equal(model.joints, np.floor(model.joints)).all() and (model.joints >= 0).all() and (model.joints < bone_count).all(), 'Invalid joint indices')
    require(model.bind_global.shape == (bone_count,4,4) and np.isfinite(model.bind_global).all(), 'Invalid bind array')
    require(np.allclose(model.bind_global[:,3,:], [0,0,0,1], atol=1e-6) and np.all(np.abs(np.linalg.det(model.bind_global)) > 1e-10), 'Invalid bind matrices')
    # Native skinning can weight the same joint more than once. glTF requires
    # unique positive influences; summing them preserves the linear blend.
    joints, weights = model.joints.copy(), model.weights.copy()
    for slot in range(1, 4):
        for previous in range(slot):
            duplicate = (joints[:, slot] == joints[:, previous]) & (weights[:, slot] > 0) & (weights[:, previous] > 0)
            weights[duplicate, previous] += weights[duplicate, slot]
            weights[duplicate, slot] = 0
    joints[weights == 0] = 0
    nodes = rig_nodes(g, model.rig, model.rest_global, model.scale)
    g.document['extras']['atpModel'] = {'identity': model.name, 'boneNames': model.rig.names}
    attributes = {'POSITION': g.accessor(model.positions, 'VEC3', target=34962, bounds=True),
                  'TEXCOORD_0': g.accessor(model.texcoords, 'VEC2', target=34962),
                  'JOINTS_0': g.accessor(joints, 'VEC4', component=5123, target=34962),
                  'WEIGHTS_0': g.accessor(weights, 'VEC4', target=34962)}
    g.document.update(materials=[], textures=[], images=[], samplers=[])
    texture_transform = False
    shared_clock = None
    for row in model.materials:
        properties = row.get('rkm_fields', {})
        shader = row.get('resolved_shader') or properties.get('Shader') or 'rkshader'
        require(shader in ('rkshader', 'rkshader_horn', 'rkponyhair0'), 'Unsupported native shader')
        if shader == 'rkshader_horn':
            require(row.get('native_horn_default_discard') is True and
                    not any(key.startswith('UserVector') for key in properties),
                    'Unverified native horn default')
        require(properties.get('BlendMode','opaque') in ('opaque','none','alpha'), 'Unsupported blend operation')
        require(properties.get('ClampMode','RK_REPEAT') in ('RK_REPEAT','RK_CLAMP'), 'Unsupported texture wrapping')
        require(all(properties.get(k,'1') in ('0','1') for k in ('Cull','DepthWrite','DepthTest')), 'Invalid native render state')
        raw = row['png_bytes']
        with Image.open(io.BytesIO(raw)) as image:
            require(image.format == 'PNG', 'Textures must be PNG')
            image.verify()
        i = len(g.document['materials'])
        g.document['images'].append({'bufferView': g.view(raw), 'mimeType': 'image/png'})
        wrap = 10497 if properties.get('ClampMode','RK_REPEAT') == 'RK_REPEAT' else 33071
        g.document['samplers'].append({'magFilter': 9729, 'minFilter': 9729, 'wrapS': wrap, 'wrapT': wrap})
        g.document['textures'].append({'source': i, 'sampler': i})
        g.document['materials'].append({'name': row['name'], 'pbrMetallicRoughness': {'baseColorTexture': {'index': i}, 'metallicFactor': 0, 'roughnessFactor': 1},
                                       'extensions': {'KHR_materials_unlit': {}},
                                       'alphaMode': 'BLEND' if properties.get('BlendMode') == 'alpha' else 'OPAQUE',
                                       'doubleSided': properties.get('Cull','1') == '0',
                                       'extras': {'atpNativeDepth': {'write': properties.get('DepthWrite','1') == '1', 'test': properties.get('DepthTest','1') == '1'}}})
        if shader == 'rkponyhair0':
            uv = row.get('native_hair_uv')
            require(isinstance(uv, dict) and set(uv) in ({'initial_gpu_x'}, {'initial_gpu_x', 'clock'}),
                    'Unverified native hair UV source')
            initial = uv['initial_gpu_x']
            require(isinstance(initial, (int, float)) and not isinstance(initial, bool) and np.isfinite(initial),
                    'Invalid native hair UV offset')
            material = g.document['materials'][-1]
            texture = material['pbrMetallicRoughness']['baseColorTexture']
            texture['extensions'] = {'KHR_texture_transform': {'offset': [0, -initial]}}
            texture_transform = True
            if 'clock' in uv:
                clock = uv['clock']
                require(isinstance(clock, dict) and set(clock) == {'version', 'initial', 'speed', 'min', 'max'},
                        'Invalid native UV clock')
                require(clock['version'] == 1 and not isinstance(clock['version'], bool) and
                        all(isinstance(clock[key], (int, float)) and not isinstance(clock[key], bool)
                            and np.isfinite(clock[key]) for key in ('initial', 'speed', 'min', 'max')) and
                        clock['speed'] > 0 and clock['min'] <= clock['max'], 'Invalid native UV clock')
                require(shared_clock is None or shared_clock == clock, 'Conflicting native UV clocks')
                shared_clock = clock
                material['extras']['atpNativeUVClock'] = dict(clock)
        if shader == 'rkshader_horn':
            # Constant native uniform x=0 discards every fragment. BLEND with
            # opacity zero would still write depth, so use a standard MASK.
            material = g.document['materials'][-1]
            material['pbrMetallicRoughness']['baseColorFactor'] = [1, 1, 1, 0]
            material.update(alphaMode='MASK', alphaCutoff=.001)
    primitives = []
    for p in model.primitives:
        idx = np.asarray(p['indices'])
        require(idx.ndim == 2 and idx.shape[1] == 3 and len(idx)>0 and np.equal(idx,np.floor(idx)).all() and idx.min() >= 0 and idx.max() < count, 'Invalid triangle indices')
        require(isinstance(p['material'], (int, np.integer)) and not isinstance(p['material'], (bool, np.bool_))
                and 0 <= p['material'] < len(model.materials), 'Invalid primitive material')
        # Native RK front-face convention is opposite glTF's CCW convention.
        # Reversing each triangle preserves the source-visible surface.
        if p.get('native_back_face', True): idx = idx[:, [0,2,1]]
        accessor = g.accessor(idx.reshape(-1), 'SCALAR', component=5123 if count < 65536 else 5125, target=34963)
        provenance = {'nativeMeshName': p['name']}
        if 'native_mesh_index' in p: provenance['nativeMeshIndex'] = p['native_mesh_index']
        material_index = int(p['material'])
        primitives.append({'attributes': attributes, 'indices': accessor, 'material': material_index, 'mode': 4, 'extras': provenance})
    require(primitives and model.materials, 'No visible geometry/materials')
    # glTF MAT4 accessors are column-major.
    inverse = np.linalg.inv(model.bind_global).transpose(0,2,1).reshape(bone_count,16)
    g.document['skins'] = [{'joints': list(range(1,bone_count+1)), 'inverseBindMatrices': g.accessor(inverse,'MAT4'), 'skeleton': 0}]
    g.document['meshes'] = [{'name': model.name, 'primitives': primitives}]
    g.document['scenes'][0]['nodes'].append(len(nodes)); nodes.append({'name': 'SkinnedModel', 'mesh': 0, 'skin': 0})
    g.document['extensionsUsed'] = ['KHR_materials_unlit']
    if texture_transform:
        g.document['extensionsUsed'].append('KHR_texture_transform')
    return g.finish()


def animation_glb(clip: Clip) -> bytes:
    frames_input = np.asarray(clip.global_frames)
    require(frames_input.dtype.kind in 'fiu' and frames_input.ndim == 4 and frames_input.shape[1:] == (len(clip.rig.parents), 4, 4), 'Invalid native animation frame layout')
    require(np.isfinite(clip.fps) and clip.fps > 0 and len(clip.global_frames) > 0, 'Invalid animation rate/length')
    g = GLB(); rig_nodes(g, clip.rig)
    t, q = local_trs(clip.global_frames, clip.rig.parents)
    count = len(t)
    # STEP plus a repeated endpoint holds the last native frame for 1/fps.
    times = np.arange(count+1) / clip.fps
    time_accessor = g.accessor(times, 'SCALAR', bounds=True)
    animation = {'name': clip.name, 'samplers': [], 'channels': []}
    for bone in range(len(clip.rig.parents)):
        for path, values, kind in [('translation',t[:,bone], 'VEC3'), ('rotation',q[:,bone], 'VEC4')]:
            values = np.concatenate([values, values[-1:]],axis=0)
            sampler = len(animation['samplers'])
            animation['samplers'].append({'input': time_accessor, 'output': g.accessor(values,kind), 'interpolation': 'STEP'})
            animation['channels'].append({'sampler': sampler, 'target': {'node': bone+1, 'path': path}})
    g.document['animations'] = [animation]
    g.document['extras']['atpClip'] = {'frameCount': count, 'fps': clip.fps, 'duration': count/clip.fps}
    # Standard STEP channels remain usable by ordinary/older glTF consumers.
    # Native-aware players interpolate these raw model-space samples first,
    # then derive parent-local TRS. Never normalize endpoint quaternions here.
    if clip.native_translations is not None or clip.native_rotations_xyzw is not None:
        native_t = np.asarray(clip.native_translations)
        native_q = np.asarray(clip.native_rotations_xyzw)
        shape = (count, len(clip.rig.parents))
        require(native_t.dtype.kind in 'fiu' and native_q.dtype.kind in 'fiu'
                and native_t.shape == (*shape, 3) and native_q.shape == (*shape, 4),
                'Invalid native sampling layout')
        require(np.allclose(pose_matrices(native_t, native_q), frames_input, rtol=0, atol=2e-5),
                'Native sampling poses differ from animation frames')
        g.document['extras']['atpClip']['nativeSampling'] = {
            'version': 1,
            'translations': g.accessor(native_t.reshape(-1, 3), 'VEC3'),
            'rotations': g.accessor(native_q.reshape(-1, 4), 'VEC4'),
        }
    return g.finish()
