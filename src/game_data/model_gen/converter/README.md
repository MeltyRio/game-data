# Model conversion core

This module migrates the conversion core from ATP PR #35 into game-data. It
turns one already resolved RK model, ANIM library and CSV clip declaration into
an in-memory skinned model GLB and independent animation GLBs. The surrounding
game-data stage owns native resource association, default appearance selection,
staging and publication. This module has no package installer, CLI or uploader.

## Inputs and API

```python
from game_data.model_gen.converter.export import convert_native

bundle = convert_native(
    "extracted/actor.rk", "extracted/library.anim", "extracted/library.csv",
    ["idle"], identity="Pony_Example", visible_meshes=["body"],
    rest_name="idle", event_path="extracted/world.xml",
    additional_event_paths=["extracted/library.xml"],
)
```

The caller establishes the model/library association from original resource
metadata and selects the visible meshes explicitly. Equal bone counts alone do
not establish compatibility. Materials and textures must be alongside the RK.
An explicit `rest_frame` is available when a verified native rule supplies it.
`events_absent=True` requires source evidence; a missing file in an incomplete
extraction is not proof of absence. These are low-level arguments, not manual
per-character configuration for the generation pipeline.

`additional_event_paths` is an optional collection of explicit XML paths. Every
distinct additional source is parsed and validated for all selected actions and
the rest clip using the same strict event checks as `event_path`. Missing or
malformed XML, unsupported selected attributes and selected native events fail
conversion by default. `events_absent=True` applies only to the primary source and never
skips these additional checks. Repeated resolved paths are checked once; source
names and hashes remain private provenance. The default empty collection
preserves the existing single-source API and does not change GLB serialization.

The normal stage uses these inputs for its `ponybook-preferred` display policy:
the first frame of a verified event-free PonyBook idle and Book visibility,
retaining World-validated actions only after both event contexts are checked.
A Book selection failure retains an independently validated World preview with
a diagnostic; this choice belongs to the caller, not a converter fallback. Pets retain their existing
editor-eye policy, and attached actors remain a separate World extension.
The converter does not simulate PonyBook idle autoplay or UI interactions.

`AssetBundle.catalogue` is ordinary JSON data, and `AssetBundle.files` maps asset
keys to GLB bytes. Conversion does not write files. `AssetBundle.provenance` preserves source
names and digests for local diagnostics; public GLBs and catalogue rows omit
that provenance and all local paths.

## Existing ATP resource contract

The catalogue contains a `models` object keyed by ATP identity. Ordinary
bundles use `schemaVersion: 1`; a required native material UV clock uses version2.
The surrounding generator also uses version2 for declared additional actors.
An explicit, source-bound native interaction profile uses version3. Its caller
supplies retained meshes, native eye indices, click/dizzy IDs, compiled events
and exact source XML digest approvals. The converter validates the retained
indices and compares every compiled event to its parsed original record. Unknown
or omitted events are errors. Hidden eye nodes remain outside all default scenes
and are listed in `extras.atpInteractions.hiddenNodes`; consumers must attach
these nodes to the default scene before rendering or releasing them. Animation
GLBs carry `atpClip.eventProfile = "ponybook-v1"` and ordered `events` without
changing standard STEP channels or native interpolation samples. The normal
pipeline supplies these low-level arguments automatically; users do not maintain
per-character maps. See the [pipeline contract](../../../../docs/3d-resource-associations.md#native-interaction-profile).
Normal generation uses the cached export path: skeletal GLBs omit model events,
which stay on catalogue animation references. It then emits schema 4 with an
`animationAssets` table and per-model integer indices, deduplicating exact
references including event bindings. Standalone bundles retain inline references.
The ATP consumer supports both representations and legacy embedded events.
Older readers must reject the new version rather than show an incomplete scene. Each row has `path`, `sha256`, `bytes`, `rigKey` and `animations`.
Animation rows also have `id`, `name` and `duration` in seconds. Paths are asset
keys resolved by ATP's existing `createAssetUrl`. Normal generation uses
`models/3d/<ObjectID>.glb` for the current model and
`models/3d/<animation-sha256>/<native-clip-name>.glb` for animations. The animation
directory is the SHA-256 of its unchanged GLB bytes, separating same-named clips
from different rigs. Exact native spelling and case are preserved and must match
`[A-Za-z0-9_-]+`; unsupported names fail with a diagnostic, without escaping,
shortening or substitution. Anonymous AdditionalObject models have no Object ID
and retain `models/3d/<sha256>.glb`; their animations use native clip names.
Textures and buffers are embedded. Byte-identical animations with the same
native name share one asset key.

The standalone `convert_native` API keeps legacy flat hash paths by default.
`model_filename=identity` selects the fixed Object ID path;
`animation_filenames=True` selects named, content-addressed animation paths.
These options change only asset keys, never GLB bytes or catalogue identity.
Readers and sync also accept earlier flat and digest-directory model paths.
Named files must match their exact catalogue Object ID or animation ID; digests,
sizes and rig compatibility are verified against the actual data.

Fixed model keys represent only the current version and use `Cache-Control:
no-cache`. Animations and anonymous models retain the existing 30-day cache.
Replacing models before the final catalogue is not a multi-file transaction:
a later local write or upload failure can leave a previous catalogue referring
to already replaced model bytes. Cached older catalogues can fail validation
while versions switch. There is no automatic recovery, retained model version
contract or remote cleanup in this implementation.
The catalogue has no JSON byte-hash contract, so parsing and compacting it does
not invalidate GLB digests. GLB bytes must reach hosting unchanged.

## Reader reuse and retained format handling

Luna Kit reads RK geometry and bones, RKM fields and PVR textures. The existing
game-data `uv.lock` is the sole authority for the Luna source revision; this
module adds no independent pin or vendored dependency. Local provenance names
that lock and the `luna_kit.model.RKModel` reader rather than reporting the old
ATP package's Luna revision.

The caller supplies the complete current extraction closure. In that data set,
an absent RKM uses the verified 11.4.3c native material-loader default: Luna's
`parse_rkm` requests the RK's original material name as an exact same-name PVR,
without inventing an RKM or substituting a PNG or another texture name. Empty
source fields preserve native `rkshader`, opaque blending, culling, depth
write/test and repeat wrapping; Luna's different dataclass cull/depth defaults
are not copied. Private material provenance records `native-default-without-rkm`
and hashes only actual source files. Existing RKM files remain authoritative;
malformed, unreadable, non-file or escaped paths are errors. A missing file in
an incomplete or failed extraction is not proof of native absence. The original
first-material RKM required for the separately reviewed hair clock stays strict.

The RK section guard validates bounds and layouts before passing bytes to Luna.
Accepted header pairs are 5.0, 5.1, 5.2 and 6.2 with the checked section layouts;
this is not a promise to accept arbitrary files with those header values. Luna
reads face data sequentially, so non-contiguous face ranges are rejected.

At the current lock (`f36858db203b4650079ec1f9e9614cc0dd43441d`),
Luna's RK magic and face-width checks are Python assertions; it does not check
version, duplicate section tags, submesh offsets or matching section counts.
Its ANIM reader does not check version, declared frame type or trailing bytes.
The adapter's explicit checks therefore remain necessary, including under
`python -O`; successful Luna decoding alone does not establish these guarantees.

ANIM 5.2 retains the converter's strict little-endian type 0/1/4 handling:

- Type 0, 32 bytes: XYZW float quaternion, XYZ float translation and a unit
  float lane. Non-unit values in that lane are rejected.
- Type 1, 22 bytes: XYZ float translation, W float, XYZ int16 quaternion
  components divided by 32767.
- Type 4, 11 bytes: XYZ int16 translation times 0.031250953674316406, W int16
  divided by 32767, XYZ int8 divided by 127.

Payload length, finite values and quaternion validity are checked. The current
Luna animation reader assumes the type 4 record size and does not dispatch on
`frame_type`; replacing this decoder with it would lose type 0/1 support.
The retained decoder is a narrow format adaptation, not a separate archive or
game-data pipeline. Type 4's packed layout is checked against Luna's reader;
types 0/1 have synthetic byte-record tests and preserve the previously reviewed
converter layout. Those tests alone do not prove every game asset's semantics.

Unsupported or invalid PVR decoding is reported as a native-input failure so
normal generation can exclude that object. A required external alpha image must
match the base image dimensions; the converter rejects a mismatch instead of
using Luna's default behavior of ignoring it. This is a stricter conversion
validation policy, not a claim of a Luna defect or a known current-game failure.
Embedded alpha still takes precedence, and valid same-size sidecars use Luna's
pixel conversion unchanged.

## Encoded texture cache

`generate_models` uses `raw/model-cache/textures` with the normal report path.
Its `cache_dir` argument changes the cache root; `texture_cache_bytes` sets the
committed PNG-entry budget (default 512 MiB, zero disables reuse). The cache is
outside `dist` and is never included in the published asset closure.

Keys include exact primary/alpha file content, the conversion recipe, Luna's
version/VCS origin, Pillow, texture2ddecoder and PNG zlib versions. Entries contain
encoded PNG bytes and dimensions, not retained decoded images. A hit verifies
length, digest and PNG chunk checksums without expanding pixels. Writes replace
one complete entry atomically, prune oldest entries to the byte budget, and skip
entries larger than 64 MiB. Corrupt entries or cache I/O failures fall back to
source conversion; generation diagnostics count hits, misses, writes, invalid
entries and I/O errors. Source changes during decoding cannot populate an old key.
Bump the `native-png-v1` recipe in `native.py` when conversion semantics change.

One local Python 3.14 measurement across 12 real PVR textures (64/512/1024px,
including alpha sidecars) produced identical PNG digests: cold cache 0.496 s and
95.4 MB peak process RSS; warm new process 0.0554 s and 51.8 MB. The encoded cache
occupied 3.44 MB. This measures the texture phase only, with a single run per mode;
it does not establish full-generation speed or physical cold-disk performance.

## Numerical and output decisions

- Native poses remain global model-space until parent-local TRS is derived and
  its reconstruction checked. Model bind matrices provide the inverse binds;
  animation frames do not replace the model's bind pose.
- A proper 180-degree X rotation maps native negative-Y-up to glTF positive-Y-up.
  RK triangles reverse winding for glTF's counterclockwise convention. UVs and
  decoded texture pixels retain the original converter's behavior.
- Four influences are validated. Only UNORM16 normalization residue is
  corrected. Repeated positive influences for one joint are merged without
  changing skinning. Mixed triangles containing zero-weight vertices remain
  unsupported. The strictly bounded all-zero case is described below.
- Ordinary `rkshader` uses unlit textured materials with source culling,
  wrapping and alpha mode. Depth-write/test switches remain material extras
  because glTF does not standardize them. The two reviewed special shader
  branches below preserve their native default behavior; unknown shaders fail.
- Independent animation GLBs contain no geometry or images. STEP tracks target
  stable indexed joint names as a standard glTF fallback. A repeated final key
  preserves the held final frame and `frameCount / fps` duration.
- Both GLBs carry an `atpRig` record. Its key includes the native ANIM content
  digest and indexed parent hierarchy. Consumers compare that key before binding
  tracks. This check does not establish the native association by itself.

### Native frame interpolation

Native conversions additionally emit
`extras.atpClip.nativeSampling = {version: 1, translations, rotations}`. The
last two fields are FLOAT accessor indices: VEC3 model-space positions and VEC4
decoded XYZW quaternions, respectively. Both contain `frameCount * jointCount`
entries in frame-major, joint-major order. These are native coordinates, before
the existing GLB root's Rx(pi) rotation.

Version 1 interpolates adjacent model-space positions linearly and quaternions
with shortest-path NLERP: negate the second endpoint only when their dot product
is negative, mix the decoded components, then normalize. **Do not normalize the
endpoints before mixing:** compressed native quaternions retain small length
differences. Convert the resulting model-space transforms to parent-local TRS
after interpolation. Parent-local glTF LINEAR/SLERP is not equivalent. This
profile follows the reviewed Android 11.4.3c sampler; it does not claim native
multi-layer blending or controller/queue semantics.

The last sample remains held until the existing clip duration; there is no wrap
to the first frame or another CSV clip. The converter verifies that raw samples
reproduce the exported keyframe poses. Model rigs, associations and resource
paths are unchanged. Matrix-only `Clip` callers omit this optional metadata and
retain STEP. Consumers may use STEP when metadata is absent, but must reject an
unknown version or malformed native samples rather than silently ignoring them.

## All-zero skin triangles

The reviewed ordinary `rkshader` GPU path accumulates all four explicit weights
from zero and then applies the world/view/projection matrix to
`vec4(skinnedPos, 1)`. A triangle with three strictly all-zero weight records
therefore collapses to the same model-space origin at every pose. It has no
rasterized area. The converter omits only these triangles, records their original
indices and ordinals in private provenance, and reuses the existing used-vertex
compaction. Empty primitives are omitted; an entirely empty model is rejected.
No weight is assigned to a guessed bone, and rig keys and animation GLBs retain
the existing contract.

This branch applies to the supported `rkshader` material profile without requiring
shader source files. A triangle mixing zero and nonzero vertices or a nonzero
normalization error remains unsupported.

The evidence is the Android 11.4.2a GPU material/skin-buffer path plus the
reviewed 11.4.3c shader sources; the CPU skinning fallback and future source
revisions are not claimed as verified. This zero-weight exception is restricted
to ordinary `rkshader`; special-shader zero-weight triangles remain unsupported.

## Reviewed native material controls

The following rules were verified against the 11.4.3c / 1143002 native client
and shader sources. RKM shader names select these implemented material behaviors;
conversion does not load or execute shader source files. Unknown shaders and
unsupported controls remain errors; future shader semantics are not automatically
supported.

For `rkshader_horn`, an undeclared native GPU UserVector is zero. The reviewed
fragment shader discards every pixel when x < .001. With no explicit RKM vector,
the converter retains the mesh, rig, texture and weights and emits standard glTF
MASK with alpha zero and cutoff .001. This preserves the inactive default without
writing depth. Explicit vectors and task-driven glow require an unsupported
controller; selected native event actions require the explicit source-bound interaction profile.

For `rkponyhair0`, the caller supplies the native object's `user_vector` dict
with exactly `UserVectorSpeed`, `RangeMin` and `RangeMax`. The authored GPU x is
exported as `KHR_texture_transform.offset = [0, -x]`. Positive speed also emits
`extras.atpNativeUVClock = {version: 1, initial, speed, min, max}`. The initial
clock comes from the **original RK material zero's CPU x**, even if its mesh is
hidden, whereas the initial texture offset comes from that hair material's GPU
x. Undeclared CPU storage is one; undeclared GPU upload is zero. These are
separate native states, not interchangeable defaults.

All hair materials share the same clock. Each active viewer frame computes
`next = clock + delta * speed`; only `next > max` resets to `min`. Resting models
continue the clock; hidden scenes pause it. The first ordinary material is only
the CPU clock source: its reviewed shader does not consume this vector, so its
exported material stays unchanged. Unknown first-material shaders are rejected.
Only absent vectors and the observed `UserVector0_0` scalar encoding are supported.
Explicit zero speed preserves the static authored offset and needs no clock.

Clock metadata is also retained in private `provenance.material_uv_clock`. A
clock-bearing bundle uses catalogue schema2 so a previous client cannot silently
freeze required animation. This is a basic preview material contract, not an
implementation of tasks, crystal effects, arbitrary native uniforms or shaders.

## Tests and limits

From the game-data repository, using its existing environment:

```sh
python -m unittest discover -s tests/converter -t tests -v
```

The tests expose the `game_data` package namespace without its CLI initializer to avoid
initializing unrelated CLI/cloud dependencies. `tests/native_fixture.py` creates
a self-authored checkerboard flag with two joints. It contains no game artwork
or native game bytes. Key regressions cover bind and skinning math, embedded
GLBs, independent STEP clips, native sampling, resource paths and native material defaults.

Props, companion orchestration, event execution, clothing assembly and task
scenes remain outside this core. Special shaders are limited to the reviewed
default-horn and native hair-clock rules above. Selected clips with unapproved events, unknown formats,
missing dependencies, non-rigid poses and incompatible layouts fail explicitly.
No future-version compatibility is implied. Newly converted resources still
require independent glTF validation and browser render/playback verification.

## License and origin

This code was migrated from the GPL-3.0-only converter submitted in ATP PR #35,
including its existing local converter-derived math and ANIM format handling.
It remains GPL-3.0-only under the game-data repository's root `LICENSE` (the
original ATP converter and game-data license files are identical). Luna Kit is
GPL-3.0. NumPy (BSD) and Pillow (MIT-CMU) remain separate dependencies. No Luna
source, game resource or private audit evidence is vendored here.

### Explicit native sampling at a CSV upper boundary

The default `convert_native(..., frame_boundary='reject')` still rejects selected
CSV ranges beyond the ANIM frame count. `frame_boundary='native-clamp'` is an
explicit alternative used by the verified AdditionalObject controller path. It
retains each original nonnegative, ordered CSV start/end/FPS declaration and
samples `min(source_index, source_frame_count - 1)`. Repeated output samples are
copies of an existing source pose, not additional native animation frames. The
full declared duration and numeric clip order remain unchanged. Private
`native_frame_boundary` provenance records the declaration, clamped sample count,
source frame count and client evidence. No other caller opts into this policy.

This follows the verified 11.4.3c / 1143002 ARM64 client: ANIM loader `0x245254`
reads frame count into resource `+0x40`; `0x2451a4` reads exactly frame count ×
joints × native stride bytes; sampler `0x24c8d8` caps both source indices before
dispatching native formats; CSV constructor `0x26a504` and layer advance
`0x24cd84` retain the declared `end - start + 1` and FPS. Only already-supported
ANIM types 0, 1 and 4 are exposed. Negative ranges, malformed/conflicting rows,
nonfinite rates and unknown layouts remain rejected.

This mode has explicit export resource budgets: at most **10,000 declared frames
per clip** and **1,000,000 joint-frame samples per conversion**, including one
rest sample per joint. The checks run before expanded index/transform allocation.
They are exporter limits, not claims about the game's format. Oversized intervals
remain unsupported instead of causing an unbounded output allocation. Ordinary
strict conversions and the GLB bytes of in-range clips are unchanged.
