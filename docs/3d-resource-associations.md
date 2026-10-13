# Native 3D generation in game-data

The normal `build_cdn` flow calls `generate_models` after Transformer/save and
before image generation. It reuses `transformer.gameObjectData`,
`transformer.defaultGameCampaign` and public GameData pony/pet/costume IDs.
With transform skipped, the stage reloads the same extracted tables using Luna
GameObjectData and reads the same campaign. `--skip model_gen` skips this stage.
There is no additional downloader, archive inventory, upload service or manual
list of models to update when game resources change.

The existing extractor remains responsible for obtaining the current inputs.
Generation does not claim that an arbitrary extracted subset is a complete
release. A locally missing file does not establish that the native resource is
absent. Recovered files come through the existing extraction workflow, not new
model-generation extraction logic. Tests against retained game sources and
private release-integrity records are kept outside this repository; none of those resources are fixtures.

The extractor keeps logging per-file errors and continuing with Luna's
`ARK.extract()`. It returns only RKM paths that raised during that invocation.
With model generation enabled, any such failure stops the build before
Transformer, model/image generation, upload and the final DLC status update;
an overall extraction exception also stops that build. A later same-name
extraction does not clear an earlier failure. Non-RKM per-file errors and builds
using `--skip model_gen` retain their existing handling. A natively absent RKM
does not produce an extraction error, so the verified default material remains
supported. This does not check that every required archive was downloaded.

`--skip extract` continues to use the existing files without this run's extraction
result. The caller is responsible for providing a complete, successfully
extracted input set for the selected game version; skipping is not an automatic
integrity check. No confirmation flag or persistent source index is added.

Normal generation uses `ponybook-preferred`: a strictly verified PonyBook default
is selected when its native visibility agrees with the independently validated
World preview. Otherwise that World result is retained with the precise Book
failure in private diagnostics. Pets retain their existing editor-eye policy.
This does not implement the complete PonyBook UI, idle autoplay or interactions.

The build excludes objects with the existing public `npc` tag from the 3D
catalogue. It keeps their ordinary GameData entries and native discovery
diagnostics. Names and other tags do not determine this exclusion. The 3D
generation stage does not delete retained files; only current catalogue
references are uploaded.

## Responsibilities

- `model_associations.py`: follow explicit object references, validate source
  dependencies, audit shared libraries, outfit ownership and attached actors.
- `model_gen/selection.py`: choose a documented initial viewer pose, exact
  visible meshes and explicitly declared clips that the basic viewer supports.
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
| Pony | `Model.MediumLOD` | `Animation.Rig_MediumLOD` | Explicit `Animation.*AnimationList` and Conversation references |
| PonyPet | `Object.Model` | `Object.Animation` | Explicit Idle/Walk/Run/EditorIdle fields; reviewed empty-Idle rule below |
| PonySet | Unique owner's model | Same owner's library | `Sets.Sets` / `SubSets` ownership, `Parts` and `PonyPart` |
| AdditionalObject | `AdditionalObject.Model` | The same Model stem | One independent child; original CSV order and native placement |

The declared external library stem identifies **both** `.anim` and `.csv`.
`ANIM.header.name` is recorded but never used to substitute a CSV. Retained
native loader code appends both extensions to its same input stem. Real inputs
include misleading embedded names, so Luna's current implicit CSV lookup is not
an equivalent object-association API.

Equal bone counts are only a rejection check after finding an explicit native
association. ANIM contains no per-joint names/hierarchy. The generated rig key
combines the exact ANIM digest with the RK indexed hierarchy; it supports reuse
within that verified source relationship, not arbitrary retargeting.

### CSV validation

A shared parser serves discovery and conversion because current Luna's CSV
reader has no equivalent explicit-path, lossless validation API. Discovery
retains valid, unambiguous clips and records invalid rows. **Any** bad or
conflicting declaration for a name invalidates that name completely. It never
keeps the first or last conflicting definition. An empty/missing library or a
library with no valid clips is unresolved. An invalid initial-pose clip rejects
the object; invalid optional actions are excluded with their CSV diagnostics.

The parser preserves the original first raw record before filtering. A blank,
malformed or conflicting first record cannot become a different first clip.
The low-level converter still rejects every invalid requested clip. A child
AdditionalObject uses native numeric library indices, so its entire original CSV
must be valid and unique: blank, malformed or duplicate records reject that child
scene instead of filtering and renumbering its animations.

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
wardrobe policy, in native constructor order. It selects this profile only when
the resulting visible meshes equal the already verified World selection. A
missing required Open mesh or a visibility disagreement retains the World
preview under `ponybook-preferred`, with a diagnostic; it is not labelled a
verified Book appearance.

MeshList `DefaultVisible` is applied to exact native subobjects. Missing default-
hidden subobjects are recorded; missing default-visible subobjects remain
unsupported. Controller overlaps without a verified ordering are rejected.

### Pose and event policy

The strict PonyBook profile uses the native **constructor static pose**. The RK
drawable loader queries the exact name `default`; only a true name miss selects
the original CSV row zero. It samples that clip's first frame and stops the
animation layer. A rejected `default`, an invalid original first row when needed,
or an event-bearing selected pose is not a name miss and cannot select another
working clip. The private record preserves the requested name and resolution.
This chain was traced in the matching 11.4.3c ARM64 client; it does not establish
which frame appears on screen after the native UI's first update.

Conversation idle is separate. An absent `Conversation` group requests the
native `gen_idle_00` default; a present group uses its exact `IdleAnimation`
string. An explicitly empty or unsupported declaration fails strict Book
selection. There is no CSV prefix expansion. Exact lookup, including the
reviewed original-row-zero rule for a true name miss, is validated independently
of the constructor pose. A valid event-free idle is available as an action;
generation does not reproduce the later idle/blink controller or autoplay it.

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

Book selection retains World-validated, explicitly declared town actions, then
excludes actions requiring events in either XML context. Constructor pose and
later Book idle are checked against both contexts too. The converter independently
validates and hashes both sources before producing a bundle, including when the
World branch has a positively absent controller. Missing extracted XML never
means that events are absent. Unknown events remain unsupported.

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

Luna already owns ARK, GameObjectData, RK, RKM and PVR parsing and is reused at
those boundaries. Its RK submesh `unknown` value has no established visibility
meaning; its glTF export does not implement game-object eye/wardrobe controllers.
The converter retains strict ANIM 0/1/4 decoding because current Luna's frame
reader assumes the type-4 layout. Native static code confirms the retained
`0.031250953674316406` type-4 translation constant; replacing it with `1/32`
would change decoded positions. See the converter README for byte layouts.

Potential Luna contributions are explicit CSV-path/lossless row support and
correct ANIM layout dispatch. Object orchestration and viewer policy belong
here; GLB serialization and validation remain in the conversion core. No Luna
fork, independent package lock or third pull request is required by this change.

### Zero-weight geometry

The reviewed ordinary `rkshader` GPU skinning path sums all four explicit weighted
positions from zero. A triangle whose three vertices each have four exactly-zero
weights therefore collapses to a single point for every finite bone palette.
The converter removes only those unrenderable triangles, records their original
indices privately, and preserves the rig and every retained weight. It does not
assign root weights or add helper joints. A mixed zero/nonzero triangle, a model
with no remaining triangles, or an unreviewed shader is still refused.

This exception requires the exact reviewed vertex/core/fragment shader digests;
those files are obtained by the existing normal extractor and included in source
provenance. Changed or missing shaders fail closed only for affected inputs.
Ordinary models do not gain these dependencies. The proof covers the reviewed
GPU path, not an unverified CPU-skinning implementation. This zero-weight rule
remains specific to ordinary `rkshader`; it is not extended to special shaders.

### Bounded special materials

Two additional source-verified shader behaviors are supported, with exact reviewed
shader digests included in private dependencies. Other revisions fail closed.
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
per identity rather than rewritten. The animation digest directory separates same-named clips from different rigs.
A main model has one fixed current-version address and is replaced on update. Byte-identical animations sharing a
native name use one key. Legacy flat hash and digest-directory model keys remain
readable. Direct calls to `convert_native` retain flat hash keys unless filename
options are supplied. Named model and animation keys must match their
catalogue Object ID and animation ID respectively. Models
contain geometry, textures and a rest skeleton; animations contain only the
compatible skeleton and STEP tracks. Ordinary models keep schemaVersion 1 and
their existing row contract. A catalogue with an `additionalActor` or native
material UV clock uses schemaVersion 2, which requires the corresponding ATP
viewer capability. Additional actors use nested model/animation references with
their own rig key; shared hash assets still use the same namespace. Private source
names, hashes, selection evidence and exclusions go to
`raw/model-associations.json`, never public dist.

`model_animation_resolved` means an explicit RK/ANIM pair passed structural
checks. `references_resolved` additionally requires usable CSV references (and
resolved outfit ownership); it does not mean renderable. `converted` records
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
stage preserves a previous catalogue. Unexpected programming/I/O failures
abort the stage. All bundles are validated before local publication. Publication
writes GLBs before replacing the catalogue, allowing fixed Object ID files to
be overwritten. Individual file replacement is atomic; the complete directory
is not. A later write failure can leave an old catalogue with replaced models.
Unreferenced local asset paths in either format are a **dry-run report only**; no deletion occurs.
This local guarantee does not undo the existing full build's earlier dist clean.

Sync validates paths, sizes, hashes and compatible catalogue references before
cloud calls. Fixed model keys use `Cache-Control: no-cache`; immutable animations
and anonymous models keep the existing 30-day cache. Unchanged bytes and matching
headers skip upload. Every 3D upload result is observed; 3D put/copy failures prevent
switching the catalogue/manifest, but do not undo fixed models already replaced. Existing non-3D upload behavior is retained,
so this is not a transaction across the complete CDN. Old immutable assets are not
deleted, and skipped generations do not invent remote cleanup. Production
retention policy still needs upstream agreement. Only the current fixed-address
model version is supported: a stale catalogue or a partial failed upload can
temporarily fail digest verification. No automatic restoration or new version
management layer is introduced.

## Safe tests and demonstration

After the [locked Python >=3.14 setup](../README.md#setup), run from this repository:

```sh
uv run --no-sync python -m unittest discover -s tests -v
uv run --no-sync python tests/make_model_demo.py --output /tmp/atp-model-demo
```

The second command requires a new directory and uses self-authored object tables,
RK/ANIM, material and texture bytes. It calls the same generation stage, writing
private `raw/` and public `assets/` separately. Point ATP's `MODEL_VIEWER_ASSETS`
at that `assets/` folder for its validator and actual Chromium playback tests.
Never run full-data sync against a fixture-only directory.

Offline tests bootstrap only the source package namespace to avoid CLI/cloud
initialization; they do not replace production dependencies. The pipeline wiring
and uploader tests execute actual functions with isolated/mocked external
services. Passing those tests does not claim a complete CLI download/extract/
Transformer/image/upload run, production R2 validation or physical Safari testing.
