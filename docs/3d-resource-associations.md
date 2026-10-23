# Native 3D generation in game-data

The normal `build_cdn` flow calls `generate_models` after Transformer/save and
before image generation. It reuses `transformer.gameObjectData`,
`transformer.defaultGameCampaign` and public GameData pony/pet/costume IDs.
With transform skipped, the stage reloads the same extracted tables using Luna
GameObjectData and reads the same campaign. `--skip model_gen` skips this stage.
There is no additional downloader, archive inventory, upload service or manual
list of models to update when game resources change.

Twilight Sparkle uses the winged RK selected by the native
`TwilightPrincess_3_Transformation` scene. Her original identity, controller and
animation library remain unchanged; her wardrobe and owner-derived costume
previews use the same winged model. This is a generation-time display choice.

The existing extractor remains responsible for obtaining the current inputs.
Generation does not claim that an arbitrary extracted subset is a complete
release. A locally missing file does not establish that the native resource is
absent. Recovered files come through the existing extraction workflow, not new
model-generation extraction logic. Tests against retained game sources and
private release-integrity records are kept outside this repository; none of those resources are fixtures.

The extractor keeps logging per-file errors and continuing with Luna's
`ARK.extract()`. It returns only RKM paths that raised during that invocation.
With model generation enabled, those paths become private generation diagnostics.
Each identity whose RK materials or declared additional actor depend on a failed
RKM is excluded while unaffected identities continue. Exact material names keep
precedence over the reviewed source-art suffix alternative; a failed candidate
cannot fall back to a different RKM or the native absent-RKM default. The check
conservatively includes hidden materials and the hair clock's first material.
An overall extraction exception still stops the build before model generation,
upload and the final DLC status update because its affected inputs are unknown.
A later same-name extraction does not clear an earlier failure. Non-RKM per-file
errors and builds using `--skip model_gen` retain their existing handling.
A natively absent RKM does not produce an extraction error, so the verified
default material remains supported. This does not check that every required
archive was downloaded.

`--skip extract` continues to use the existing files without this run's extraction
result. The caller is responsible for providing a complete, successfully
extracted input set for the selected game version; skipping is not an automatic
integrity check. No confirmation flag or persistent source index is added.

Normal generation uses `ponybook-preferred`: select the supported native Book
appearance, retaining the independently validated World preview if Book selection
fails. Missing Book XML uses the empty-controller rule below; other failures
remain in private diagnostics. Pets retain their existing editor-eye policy.
This does not implement the complete PonyBook UI or idle autoplay. The optional
native interaction profile below supplies blink, click and dizzy data to ATP.

All public pony, pet and costume identities are eligible for 3D generation,
including objects tagged `npc`. Tags and ordinary GameData entries are unchanged;
each identity still passes the same native association and conversion checks.
The 3D generation stage does not delete retained files; only current catalogue
references are uploaded.

## Responsibilities

- `model_associations.py`: resolve explicit resource filenames, shared CSV
  libraries, outfit ownership and attached actors. It does not decode RK models
  or hash resource files. Each selected RK is read and validated once, immediately
  before appearance selection, and reused by conversion. Bone compatibility and
  material checks remain in conversion. Only ANIM/cache identities and generated asset keys require hashes.
- `model_gen/selection.py`: choose a documented initial viewer pose, exact
  visible meshes and native preferred actions.
- `model_gen/interactions.py`: evaluate the associated library and compile each
  supported action's model-specific native events.
- `model_gen/additional.py`: validate one explicitly declared independent child
  actor, its complete indexed animation library and native relative placement.
- `model_gen/wardrobe.py`: one shared implementation of native ordered sets,
  slots, mesh names and linked parts for discovery and appearance selection.
- `model_gen/converter/`: convert a resolved association to model/animation GLBs.
  This migrated core has no standalone package, CLI, downloader or uploader.
- `model_gen/__init__.py`: stage complete bundles, collect exclusions and switch
  the local catalogue only after its referenced assets are ready.
- `sync.py`: reuse the existing upload function, thread pool and root manifest.
  GLBs are checked/uploaded before the 3D catalogue; the root manifest is last.

## Source associations

| Subject | Model | Animation library | Clips / appearance |
| --- | --- | --- | --- |
| Pony | `Model.MediumLOD` | `Animation.Rig_MediumLOD` | Valid associated CSV actions; native Animation/Conversation preferences and appearance |
| PonyPet | `Object.Model` | `Object.Animation` | Valid associated CSV actions; Idle/Walk/Run/EditorIdle preferences and reviewed empty-Idle rule below |
| PonySet | Unique owner's model | Same owner's library | `Sets.Sets` / `SubSets` ownership, `Parts` and `PonyPart` |
| AdditionalObject | `AdditionalObject.Model` | The same Model stem | One independent child; original CSV order and native placement |

The declared external library stem identifies **both** `.anim` and `.csv`.
`ANIM.header.name` is recorded but never used to substitute a CSV. Retained
native loader code appends both extensions to its same input stem. Real inputs
include misleading embedded names, so Luna's default implicit CSV lookup is not
an equivalent object-association API.

Equal bone counts are only a rejection check after finding an explicit native
association. ANIM contains no per-joint names/hierarchy. The generated rig key
combines the exact ANIM digest with the RK indexed hierarchy; it supports reuse
within that verified source relationship, not arbitrary retargeting.

### CSV validation

A shared parser serves discovery and conversion with the same clip-selection
policy. Luna now provides explicit-path, lossless CSV records; this module
retains its existing range, duplicate-conflict and partial-result rules. Discovery
retains valid, unambiguous clips and records invalid rows. **Any** bad or
conflicting declaration for a name invalidates that name completely. It never
keeps the first or last conflicting definition. An empty/missing library or a
library with no valid clips is unresolved. An invalid initial-pose clip rejects
the object; invalid optional actions are excluded with their CSV diagnostics.

The parser preserves the original first raw record before filtering. A blank,
malformed or conflicting first record cannot become a different first clip.
The low-level converter still rejects invalid requested CSV declarations. A child
AdditionalObject uses native numeric library indices, so its entire original CSV
must be valid and unique: blank, malformed or duplicate records reject that child
scene instead of filtering and renumbering its animations. After conversion, the
emitted child IDs must also exactly match that complete original sequence; a
clip that cannot be encoded rejects the child scene rather than changing its
initial index or random queue.

### Animation scope and reuse

Normal generation considers every valid clip in each object's explicitly
associated native CSV. This does not search another library or infer compatibility
from a clip name. Each Pony/Costume action still passes that model's event and
mesh checks; unsupported actions have explicit private diagnostics. Pets retain
their native absence of the Pony event controller. `preferredAnimations` preserves
the successfully exported native town/Conversation choices for the viewer's
initial action list; it does not restrict the full `animations` list. Native
click and dizzy controls keep their separate verified ID lists.

The cached export path isolates an individual action's encoding failure and
records it in `conversion_provenance.animation_exclusions`. Other valid actions
and the model remain available, and failed IDs are removed from preferences and
interaction lists. Invalid model geometry, materials, associations or the required
rest pose still reject the model. AdditionalObject libraries retain the stricter
complete-sequence rule above because their controller uses numeric indices.

Skeletal animation bytes are shared independently of model events. The cache key
includes the exact ANIM content, indexed rig hierarchy, clip name/range/FPS,
frame-boundary policy, encoder version and decoder dependencies. Bump
`AnimationCache`'s encoder version when skeletal encoding changes; Python source
files are not hashed. Different model bind matrices remain
in their model GLBs. Cached bytes carry no model-specific event list: native XML
and mesh targets are checked separately for every model, then attached to its
catalogue animation reference as described below.

The default local caches live beside the private report under
`raw/model-cache/animations` and `raw/model-cache/textures`, outside `dist`.
They can be reused across builds without replacing source discovery, validation
or the current output closure. A cache miss or invalid entry rebuilds from the
current checked sources. Animation decoding and generated bytes use bounded
in-memory reuse (64 MiB encoded GLBs and 128 MiB decoded libraries by default).
The local animation disk cache is disposable and has no automatic historical
pruning; operators may remove it outside a running build. Encoded textures have
the separate on-disk budget documented in
[the converter guide](../src/game_data/model_gen/converter/README.md#encoded-texture-cache).

An upload-enabled build can load the optional S3 animation index. Normal `sync`
still uploads and validates the selected GLBs before publishing the catalogue and
root manifest. Only after that succeeds is the optional cache index updated with
current published animation references, including child actors. Index/asset read
failures fall back to local conversion; index write failure is reported without
invalidating the completed asset publication. The uploader retains its existing
hash metadata and explicit override behavior. Local builds without
upload do not access this S3 cache. `--skip model_gen` skips cache loading and
index publication.

### Default appearance and outfits

Pony `Sets.Sets` terminates at its first empty value. Direct `PonySet.SubSets`
are registered too; nested subsets are outside the verified rule. The first
registered set supplies the default Body/Mane/Tail slots. A requested outfit
starts with those defaults and applies its explicit parts plus one LinkedPart
hop. Slot conflicts, missing part records and deeper unverified links fail.

`PonyPart.ModelName` names an RK **mesh**, not another RK file. Exact names
control visibility. A declared name absent from the RK is recorded as a native
name-lookup no-op; it is not replaced by a similar mesh or by the default part.
This also permits a declared empty-tail part to hide other registered tails.
Unknown ownership remains unresolved: no name matching or quest-clone preference.
An ATP outfit entry represents a complete outfit preview, not saved player
mixed equipment or an interactive wardrobe.

The World preview uses explicit Pony Eyes declarations or native category
defaults. Missing Eyes and explicit empty Open groups stay distinct. An explicitly empty Open group
leaves the reviewed native eye controller disabled: initialization, blink and
EyeSet paths do not hide the other groups. The RK meshes therefore retain their
initial visibility, subject to the separate wardrobe and MeshList controls.

A missing nonempty Open mesh is normally refused. A narrow exception covers a
single actual RK mesh without a wardrobe when **none** of the declared eye names,
their native literal `lod0` to `lod1` substitutions or native constructor defaults
match it. Every reviewed eye show/hide lookup is then a no-op. The check uses a
superset of possible names; it does not invent a replacement or generalize to
multiple meshes. Unknown states and malformed declarations remain errors.

For nonempty matching groups, exact array matching is the World static preview
policy. The reviewed 11.4.3c Pony constructor instead reads the first Item of each state
and replaces only the first literal `lod0` with `lod1`. Its world and PonyBook
controllers hide Closed/Happy/Frown during Open initialization. The selector adds
these native hides only when all four resulting names are nonempty, distinct,
exactly present, and native Open equals the preview's single matching Open mesh.
It retains the original array policy otherwise; a later nonempty Item cannot
replace a blank first Item for this additional rule. Added hides are recorded in
private selection provenance and remain subject to overlap checks.

This complete-controller rule hides extra expression layers without character-
specific mappings. When the first-Item controller does not resolve completely,
the explicit-array World preview is retained and is not presented as a complete
reproduction of the game's eye controller. No guessed case correction,
LOD-number parsing or cross-character replacement is applied.

The strict PonyBook profile uses the native first Item and first literal LOD
replacement directly. It applies Eyes, MeshList, then the same default/explicit
wardrobe policy, in native constructor order. When Book XML is present, the
resulting visible meshes must equal the already verified World selection. A
missing required Open mesh or an XML-controlled visibility disagreement retains
the World preview under `ponybook-preferred`, with a diagnostic.

If the exact Book XML is absent from the supplied inputs, use the reviewed native
empty controller: no XML mesh defaults or events, while Eyes, wardrobe and idle
selection still apply. World mesh defaults are not substituted. Private provenance
records `missing-input-native-empty-controller` and the requested filename.
This describes the supplied input set, not every resource mounted by the game;
it cannot distinguish an unprovided XML from an earlier download/extraction failure.
Existing error logging and continuation remain unchanged. A present malformed,
unreadable or ambiguous XML is not treated as absent.

MeshList `DefaultVisible` is applied to exact native subobjects. Missing default-
hidden subobjects are recorded; missing default-visible subobjects remain
unsupported. Controller overlaps without a verified ordering are rejected.

### Pose and event policy

The strict PonyBook profile displays the **first frame of the resolved
Conversation idle**, without autoplay. An absent `Conversation` group requests
`gen_idle_00`; a present group uses its exact `IdleAnimation` string. An empty or
rejected declaration fails strict Book selection. Only a true name miss may use
the verified original CSV row zero; there is no prefix expansion. If the idle
has events in either Book or World XML, strict selection rejects it and
`ponybook-preferred` retains the independently validated World preview.

The native constructor's exact `default` lookup and original-row-zero fallback
are validated separately, and its event-free clip remains an available action.
Private provenance records both resolutions and identifies the actual display
rest. AdditionalObject placement is computed from that generated parent rest;
its independent model and animation library remain unchanged.

The retained World preview uses explicit `Conversation.IdleAnimation`, otherwise
the first nonempty `LeftIdleAnimationList` entry, as its documented viewer policy.
With neither declared, it uses the native RK-load static pose. Its existing
CSV validation and exact-name/original-row-zero rules remain unchanged. No later
working idle is substituted for a rejected declaration.

World event selection is independent of the rig name. Explicit
`AnimationEventsXML` takes priority. With an empty field, `AI.Special_AI=2`
actively clears the event controller and MeshList pointers; this is positive
native absence evidence. Other reviewed integer kinds (`0`, `1`, `3`, `4`, `5`)
use `pony_type01.xml`; missing or unknown kinds are refused. PonyBook instead
loads the XML identified by `Animation.Rig_MediumLOD`.

Base Book selection retains World-validated, explicitly declared town actions,
excluding event-bearing actions until the interaction compiler below verifies
all their events. Constructor pose and later Book idle retain their existing
strict checks against both contexts. The converter independently
checks all selected event sources before producing a bundle, including
when the World branch has a positively absent controller. Required World XML
remains mandatory even when Book uses an empty controller. Unknown events remain
unsupported.

`ponybook-default` exposes strict selection; `ponybook-preferred` first requires
a valid World result, then records a concrete Book failure and retains that old
result when necessary. It cannot bypass an invalid World association, CSV,
visibility policy or dependency. The selected context and reason are private
provenance, rather than a claim that every generated actor uses Book rules.

Pet actors bypass the Pony XML event-controller path in the reviewed native
code; this is positive absence evidence, not a missing-file assumption. The
viewer uses `Object.AnimationIdle`; if it is empty, the original CSV first clip
is used, as in the reviewed native lookup. Unknown **nonempty** names are rejected
rather than reproducing the game's fallback. Static eye visibility uses the
native editor's exact `eyes_shut` hide rule on a model whose meshes start visible.
It does not infer aliases from names. This combines a declared world Idle with
editor eye visibility as an explicit viewer policy. `AnimationEditorIdle`, when
present, remains an explicitly selectable clip rather than silently replacing Idle.

Attached actors are an explicit **World-actor extension** to the preview,
separately traced through AdditionalObject.Model. Selecting a Book default
parent appearance does not claim that the native PonyBook shows the same child.
The AdditionalObject.Animation field is preserved without treating it uniformly as
a library or clip: real declarations include both clip names and other strings.
The reviewed attached-actor path starts at original CSV index zero and later
chooses a random library index, with repetition allowed. Generation supports one
independent child with its own rig, all default-visible meshes and complete
indexed clip library. Its scale, offsets and rotation policy come from native
fields; only the parent's exact RootBone X/Z displacement affects placement.
The client gives it an independent animation clock and shares the parent's
loading, cancellation and disposal lifecycle. A child failure excludes the
whole scene. This does not add recursive attachments or task controllers.
See [the precise native and viewer contract](3d-additional-actors.md), including
the deliberate removal of map-depth distortion for free 3D viewing.

### Evidence boundary and Luna ownership

The original controller rules were traced in a retained Android 11.4.2a binary
and checked against 11.4.3c tables/resources. The AdditionalObject creation,
queue, root lookup and matrix paths, the PonyBook constructor/idle/visibility
paths, and the special material paths described below were additionally traced
in the matching 11.4.3c / 1143002 ARM64 client.
These are static checks, not a claim to have run the current game client. A future
unknown structure must be diagnosed, not accepted through an inferred fallback. Campaign visual overrides on an object, owner or
wardrobe dependency are excluded pending verified merge semantics; ordinary
price/Enabled overrides are not reinterpreted as mesh visibility. Changes to
`AI.Special_AI` are also excluded because they can change the event controller.

Luna owns ARK, GameObjectData, RK, RKM, PVR and ANIM parsing and is reused at
those boundaries. The locked RK reader validates binary section layouts and
mesh references; game-data checks the parsed model for its required skinning
and UV layout without rereading the file or gating decoded RK header versions. Its RK submesh `unknown`
value has no established visibility meaning; its glTF export does not implement
game-object eye/wardrobe controllers.
The ANIM adapter now consumes Luna's decoded `Anim.raw_frames`, converts its
WXYZ quaternions to XYZW and retains the converter's 5.2 / type 0/1/4, exact
length and quaternion-validity requirements. Native static code confirms the
`0.031250953674316406` type-4 translation constant now used by Luna; the old
reader's `1/32` compensation is removed. See the converter README for layouts.

Luna [PR #12](https://github.com/ego-lay-atman-bay/luna-kit/pull/12) provides the
ANIM layout dispatch, fixed-array API and lossless CSV records. The dependency
temporarily pins that PR's published commit until it is merged upstream. Object
orchestration and viewer policy belong here; GLB serialization and validation
remain in the conversion core. No vendored Luna source or separate converter
package lock is added.

### Zero-weight geometry

The reviewed ordinary `rkshader` GPU skinning path sums all four explicit weighted
positions from zero. A triangle whose three vertices each have four exactly-zero
weights therefore collapses to a single point for every finite bone palette.
The converter removes only those unrenderable triangles, records their original
indices privately, and preserves the rig and every retained weight. It does not
assign root weights or add helper joints. A mixed zero/nonzero triangle, a model
with no remaining triangles, or an unreviewed shader is still refused.

This rule uses the supported `rkshader` material profile without requiring shader
source files. The proof covers the reviewed GPU path, not an unverified
CPU-skinning implementation, and does not extend to special shaders.

### Bounded special materials

Two additional source-verified behaviors are selected by RKM shader name.
Shader source files are not conversion inputs; arbitrary shaders and future
semantic changes are not automatically supported.
For `rkshader_horn`, an undeclared native UserVector binds GPU zero and its fragment
shader discards every fragment. The converter preserves the geometry but emits
standard glTF MASK with zero base alpha, which also prevents depth writes. An
explicit horn UserVector or task-controlled glow remains unsupported.

For `rkponyhair0`, the authored GPU vector supplies a static V offset via
`KHR_texture_transform`. Positive native `UserVectorSpeed` additionally requires
an `atpNativeUVClock` material extra. Its initial CPU value comes from original RK
material zero, even when that material's meshes are hidden; it is distinct from
the hair material's authored GPU value. The clock adds delta times speed and
resets to RangeMin only when greater than RangeMax. The viewer must tick it while
resting and pause it when hidden. Speed zero retains the static authored offset.
Only the verified scalar encoding and exact native UserVector fields are accepted;
ordinary materials are not replaced or visually animated by this clock.

## Outputs, failures and publication

Public output is `dist/models_3d.json` plus GLBs:

- Current main model: `dist/models/3d/<ObjectID>.glb`.
- Animation: `dist/models/3d/<animation-sha256>/<native-clip-name>.glb`.
- Anonymous AdditionalObject model: `dist/models/3d/<sha256>.glb`, since this
  internal actor has no game Object ID. Its animations use the named format.

The digest is computed from the unchanged GLB bytes. Exact names and case are
preserved; only `[A-Za-z0-9_-]+` is accepted, with unsupported names diagnosed
per action or identity rather than rewritten. The animation digest directory separates same-named clips from different rigs.
A main model has one fixed current-version address and is replaced on update. Byte-identical animations sharing a
native name use one key. Legacy flat hash and digest-directory model keys remain
readable. Direct calls to `convert_native` retain flat hash keys unless filename
options are supplied. Named model and animation keys must match their
catalogue Object ID and animation ID respectively. Models
contain geometry, textures and a rest skeleton; animations contain the
compatible skeleton and STEP tracks with optional native interpolation samples.
Normal cached generation places model-specific events on catalogue animation
references instead of inside shared animation GLBs. Normal generation emits
schema 4 with a shared `animationAssets` array. Each model's `animations` list
contains indices into that array, in its original validated order. Table entries
include the asset metadata and any event binding; only completely identical
references share a row. Different mesh targets or events retain distinct entries
while referencing the same skeletal GLB. `preferredAnimations` remains a list of
native clip IDs. This avoids repeating entire library metadata for every model.

Standalone converter bundles use inline animation references. Schema 1 models,
schema 2 material clocks/additional actors and schema 3 interaction profiles
remain supported; animation events use only the v2 catalogue binding. Additional actors use nested model/animation references with
their own rig key; shared hash assets still use the same namespace. Private source
names, selection evidence and exclusions go to
`raw/model-associations.json`, never public dist.

### Native interaction profile

Normal generation compiles the already resolved Pony/Costume model's native eye
names, PonyBook tap branches and the exact `arena_pony_defeat` CSV action. It
does not discover another rig or substitute a similarly named action. The
native PonyBook branches use `Conversation.InteractionAnimation` when the
Conversation block exists; otherwise they use reviewed `AI.Special_AI` branches.
The viewer randomly chooses among available native branch entries; it does not
reproduce the game's remote-service random gate or orientation-based choice.
The native branch evidence is Android client 11.4.2a at
`0x87337c..0x87347c` / `0x8736d0..0x873a44`, also used by the independently
validated reference viewer. No client binary is distributed here.

Interactive rows add
`interactions: {version: 1, clickAnimations: [...], dizzyAnimation?: id}`.
Catalogues with model-specific animation event bindings or SubObject mesh controls
use `schemaVersion: 4`;
consumers must support that version before these catalogues are published.
Schema 3 remains supported for models
whose supported interactions need neither separate event bindings nor SubObject
controls.
Every referenced ID must be in that row's successfully converted animations.
The model GLB adds `extras.atpInteractions` with version 1, native-index eye
groups, optional `subObjects` indices, `hiddenNodes` and any embedded emitter
profile/textures. Every retained
mesh has a separate skinned node carrying `atpNativeMeshIndex` and
`atpDefaultVisible`. Default-hidden eye and action-prop nodes are outside every scene; the
profile-aware reader explicitly adopts them into the default scene using the
same skeleton. Ordinary glTF readers keep the previous default appearance.
The reader must resolve these nodes before rendering or disposing the asset.

Cached and uncached generation emit event-free skeletal GLBs and add
`eventBinding: {version: 2, events: [...], visibility: [...]}` to event-bearing
animation references. Motion can share one asset across models while bindings
retain each model's mesh names and event sequence. Version 2 is the initial
production binding format; animation GLBs contain no embedded events. Coordinate
the first resource publication with the matching ATP viewer.

The producer resolves native SubObject IDs to that exact model's mesh names and
folds SubObject/EyeSet writes into `{meshName, times, values}` curves. Times are
seconds (`frame / fps`), values are booleans, and time zero starts from the model
default. Same-time writes keep native source order; later keys remain even if
their values repeat. All exact-name mesh matches are retained. Missing targets
cannot be replaced by another model's ID or another event file's declaration.
The callback `events` array retains only EyeSet expression/blink settings and
Emitter effect keys. ATP builds Boolean tracks directly from `visibility` and
uses that same array when baking downloads. Default visibility is restored when
an action finishes or is canceled.

Event frames are native CSV-relative integer indices, independent of fractional
pose interpolation. Explicit XML paths bind the compiler's approvals, and the
converter independently checks every exported event against those sources. An
unsupported event, missing target/eye state/effect dependency, conflict or
out-of-range event frame excludes that whole action with diagnostics; no bare
skeletal substitute is published. This version conservatively excludes
out-of-range records instead of implementing native clamp/equal-count behavior.

The finite dizzy burst supports only the verified
`mlp_arena_pony_defeat_02` controller, using Luna's native RKM/PVR readers and the
original texture. Its numeric particle parameters remain in the embedded
profile; there is no guessed head-bone binding or replacement artwork. Other
particle controllers, sound and scene events remain unsupported. SubObject
support only covers the declared mesh visibility events above; it does not create
missing prop geometry or add task/scene controllers. AdditionalObject actors retain their previous independent strict
controller and animation behavior; pets do not acquire PonyBook interactions.

Open/Closed geometry must both exist, be distinct, and agree with the verified
default eye mask before the consumer enables blinking. No eye geometry is
invented. If optional interactive geometry or dependencies cannot convert, the
normal generator retains its independently verified static model/event-free
actions and records `interaction-assets-unavailable`; it publishes no interaction
profile for that fallback. A failure of the original static conversion still
excludes that model. Fixed model URLs, cache revalidation and non-atomic upload
semantics are unchanged; frontend and resources should be published together.

`references_resolved` means explicit resource files and usable CSV references
were found (with resolved outfit ownership); it does not mean that the RK contents
or bone compatibility have been validated. `converted` records
successful local bundle generation. `generation.excluded` is the final per-ID
failure list. The separate `summary.discovery_blockers` is an unchanged snapshot
of the earlier association audit and must not be read as final rendering coverage.
In particular, its `additional_actor_conversion_not_run` key and the
audit's initial `renderer_supported: false` describe a child whose scene conversion
has not yet been checked; they are not final claims that every child is unsupported.
After success the identity's blockers are empty, `additional_actor.converted` and
`renderer_supported` are true, and current conversion provenance records the
verified placement. After failure, `generation.excluded` gives the actual reason.
The audit's retained older-client playback evidence and the later same-version
conversion evidence also remain distinct rather than overwriting history.

One bad supported input is excluded with its reason; it cannot create a success
row or partial bundle. If all requested identities fail, the local generation
stage preserves a previous catalogue. Source validation and read errors before
output validation exclude the affected identity. Unexpected programming errors,
staging writes and publication errors abort the stage. Publication
writes GLBs before replacing the catalogue, allowing fixed Object ID files to
be overwritten. Individual file replacement is atomic; the complete directory
is not. A later write failure can leave an old catalogue with replaced models.
Unreferenced local asset paths in either format are a **dry-run report only**; no deletion occurs.
This local guarantee does not undo the existing full build's earlier dist clean.

Sync validates paths, sizes and compatible catalogue references before
cloud calls. Fixed model keys use `Cache-Control: no-cache`; immutable animations
and anonymous models keep the existing 30-day cache. Unchanged bytes and matching
headers skip upload. Every 3D upload result is observed; 3D put/copy failures prevent
switching the catalogue/manifest, but do not undo fixed models already replaced. Existing non-3D upload behavior is retained,
so this is not a transaction across the complete CDN. Old immutable assets are not
deleted, and skipped generations do not invent remote cleanup. Production
retention policy still needs upstream agreement. Only the current fixed-address
model version is supported: a stale catalogue or a partial failed upload can
temporarily use mismatched model metadata. No automatic restoration or new version
management layer is introduced.

## Offline tests and local model review

After the [locked Python >=3.14 setup](../README.md#setup), run from this repository:

```sh
uv run --no-sync python -m unittest discover -s tests -v
```

These tests use self-authored native-format inputs and do not require game
resources or cloud credentials. They cover key conversion, native association,
PonyBook interaction, extraction-failure and upload-order regressions; they do
not demonstrate the appearance of real characters.

Offline tests bootstrap only the source package namespace to avoid CLI/cloud
initialization; they do not replace production dependencies. The pipeline wiring
and uploader tests execute actual functions with isolated/mocked external
services. Passing those tests does not claim a complete CLI download/extract/
Transformer/image/upload run, production R2 validation or physical Safari testing.

## Prepare real game resources for local review

If you already have a completed output from this PR containing `models_3d.json`
and every referenced file under `models/3d/`, use it directly with the
[ATP demo instructions](https://github.com/MeltyRio/all-the-ponies/blob/feature/3d-model-viewer/docs/3d-model-viewer.md#run-the-demo-with-game-models).
The viewer needs neither raw game files nor Python in that case. A normal older
game-data output without the catalogue cannot display 3D models.

To generate your own output, check out the producer PR in a new directory:

```sh
git clone --branch codex/3d-resource-association --single-branch \
  https://github.com/MeltyRio/game-data.git game-data-3d-review
cd game-data-3d-review
```

Complete the [Python 3.14+, uv and native dependency setup](../README.md#setup),
including the default dependency groups, then run:

```sh
uv sync --locked
uv run --no-sync game-data --help
uv run --no-sync game-data build --help
```

Use a complete, successfully extracted resource tree from the existing
game-data/Luna extraction workflow for one known game version. Keep its original
filenames, case and directories. Do not assemble a release by mixing historical
files with current tables. The important input categories are:

| Input | Why it is needed |
| --- | --- |
| `gameobjectdata.xml`, `shopdata.xml`, `gameobjectcategorydata.xml`, `defaultGameCampaign.json` | Object IDs, native model/animation references, outfit declarations and campaign data. |
| `data_ver.xml`, `questmanager.xml`, `*.loc`, and the other normal game-data tables, images and SWFs | The normal Transformer still processes the game's data before model generation. Keep its complete usual inputs, including extracted atlas images. |
| Referenced `.rk` models | Geometry, skeleton and named visible parts. |
| Declared library `.anim` and matching `.csv` | Animation frames and native clip names/ranges. These are resolved from native declarations, not paired by guessing. |
| Native `.rkm`, `.pvr`, `.alpha.pvr` and `.png` dependencies | Materials and textures. Retain RKM files that exist in the release; only a genuinely absent native RKM may use the verified default. |
| Referenced controller `.xml` | Appearance and animation event checks. |

For example, the verified 11.4.3c Twilight declaration names
`pony_twilight_set_lod1.rk`. Her display mapping uses
`pony_type01_twilightprincess_lod1.rk`, selected by the native transformation
scene, while retaining the `pony_type01.anim` / `pony_type01.csv` library and
`pony_type01.xml` controller context. Keep the winged model and its material and
texture dependencies in the extraction. These names illustrate the association; they
are not a minimal input pack for the whole build. APKs, client `.so` binaries,
private research reports, saved games and player accounts are not runtime
inputs to this extracted-input build.

Keep the input and output outside both source checkouts, for example:

```text
review-input/
  extracted/
    gameobjectdata.xml
    defaultGameCampaign.json
    ...the complete same-version extracted tree...
review-output/                  # a new destination for this build
```

`--raw` names `review-input`, not `review-input/extracted`. The existing extractor
normally reads archives under `raw/arks`; the following command reuses an
already extracted tree, so it requires no ARK or APK files in that directory.
From the game-data checkout, run:

```sh
uv run --no-sync game-data --env dev build \
  --version 11.4.3c \
  --raw /absolute/path/to/review-input \
  --output /absolute/path/to/review-output \
  --skip download extract image_gen
```

Replace the version with the version of your inputs. This uses the normal CLI,
Transformer and model generation. Use a new output path: the normal build clears
that destination before Transformer. It writes `model-associations.json` under
raw, outside the public output. `--skip extract` makes the caller responsible for
complete, successfully extracted inputs; it cannot detect earlier extraction
failures. If you only have archives, first obtain a successful extraction through
the existing workflow; this guide does not establish that an arbitrary archive
subset or a new download is complete.

`--skip image_gen` skips the later image-generation stage, not Transformer's own
image/SWF work. Java, FFDec and FFmpeg may still be required; pass
`--ffdec /absolute/path/to/ffdec.jar` if FFDec is not on PATH. Do not skip transform
for a new empty output. The explicit-version command above does not download or
upload, and needs no S3 credentials or Play account. Do not add `--upload` or
`--force`, or use the README's upload example/default Docker command for this
local review.

After a successful build, check that the intended pony has both a model and
animations before opening the viewer:

```sh
uv run --no-sync python - /absolute/path/to/review-output <<'PY'
import json
import sys
from pathlib import Path
from game_data.model_catalogue import animation_assets, resolve_animation

root = Path(sys.argv[1])
catalogue = json.loads((root / "models_3d.json").read_text())
row = catalogue["models"].get("Pony_Twilight_Sparkle")
if not row or not row.get("animations"):
    raise SystemExit("Twilight or her clips were not generated; inspect raw/model-associations.json")
shared = animation_assets(catalogue)
animations = [resolve_animation(reference, shared) for reference in row["animations"]]
for asset in [row, *animations]:
    if not (root / asset["path"]).is_file():
        raise SystemExit("Missing generated file: " + asset["path"])
print(row["path"])
print("Available animations:", ", ".join(clip["name"] for clip in animations))
PY
```

This is an availability check; the viewer separately checks GLB sizes, structure
and rig metadata. A successful build can exclude unsupported objects or actions.
For a missing ID, inspect `generation.excluded` and `identities[ObjectID]` in the
private `raw/model-associations.json` report. Do not substitute a similar model
or suppress its diagnostic to make the demo appear successful.

Point ATP's `MODEL_VIEWER_ASSETS` at the complete `review-output` and follow the
linked viewer instructions. Select Twilight and click a listed animation; the
parent starts at rest rather than autoplaying. No domain, bucket or deployment
is needed. Game resources and generated GLBs are not bundled with either PR;
keep them private and do not sync a partial review directory to production.

### Native component skin weights

Native weights need not sum to one. Conversion retains Luna's original components
and bone indices in `_NATIVE_WEIGHTS` / `_NATIVE_JOINTS`; a separate normalized
standard pair satisfies glTF transport requirements. ATP uses that normalized
`JOINTS_0` / `WEIGHTS_0` pair for both CPU and GPU rendering, including wardrobes
and additional actors. Client GLB downloads retain both pairs but use the standard
pair, as ordinary glTF importers do. The custom attributes preserve source data
without restoring native underweight deformation. Animation binding and visibility
v2 retain their existing contracts.
