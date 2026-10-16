# Declared AdditionalObject actors

The normal `generate_models` stage can emit one additional actor declared by the
selected native Pony. This also applies to a uniquely resolved outfit's owner.
It reuses the existing association discovery, extracted resources, converter,
content-addressed GLBs and uploader. There is no attachment inventory, downloader,
player-state lookup, alternate build command or task scene graph.

## Native source and controller boundary

The reviewed Android ARM64 client is 11.4.3c / 1143002. Its library SHA-256 is
`d4fc2e7650ae866735d4a9a1da4b900262bdcf188f1d34fb9c0ebe6b08609c29`.
These are static control-flow and data checks, not execution of the native game.

The creation path `0x333b60..0x333bc4` calls `0x34edd4`: the exact
`AdditionalObject.Model` stem is passed as both the RK and ANIM library argument.
The external library also selects the CSV. `AdditionalObject.Animation` is kept
in the private association report but is not interpreted as a file or chosen
clip; real declarations contain both clip names and other labels there.

The generic actor has its own RK rig and default visible meshes. It does not
instantiate the Pony eye, wardrobe or XML event controller. The child constructor
is `0x408638`, with its world-matrix method `0x408948`. It is not skinned to the
owner, even when their bone counts happen to match.

Creation starts at original CSV index zero. At completion,
`0x3387d0..0x338828` obtains the library count and starts `rand() % count` at normal
speed. The viewer implements this first-then-random-index strategy with its own
random source, not a reproduction of a particular game RNG sequence. A random
choice may repeat the previous index.

Every original child CSV record must currently be valid and unique. The shared
CSV reader records raw and empty record counts before filtering; malformed rows,
blank records, conflicting names or merged duplicate rows exclude the complete
child scene. This conservative restriction prevents filtered rows from changing
the native numeric indices or their probabilities. Ordinary selected-animation
validation is unchanged. Child tails use only the explicit policy described in
[Native CSV tail sampling](#native-csv-tail-sampling).

## Display context

The exported scene follows the verified world-actor rules, adapted for a free 3D
preview by removing map-depth compression as described below. `LockRotate`
selects the native fixed or inherited heading; it is not a camera-facing
billboard. Native material culling remains effective when the camera orbits.

PonyBook creates a different `ConversationPony` object (factory kind 133,
constructor `0x87932c`), with separate model selection, Eyes, idle and XML-event
paths. Its model selection can depend on the owned appearance, and its event
setup derives the XML name from `Rig_MediumLOD`. These rules belong to that
context; individual model, eye or event choices must not be mixed into the
world-actor export to bypass an unresolved source or controller requirement.

This preview does not reproduce PonyBook or promise pixel-for-pixel equivalence
with the game. No native runtime screenshot comparison has established that
claim; source, numeric and browser checks have narrower scopes.

## Coordinates

Both generated GLBs use scale 1 and rotate native Y-down to GLTF Y-up with
`C = diag(1, -1, -1, 1)`. The child's placement is independent of the parent GLB's
skeleton. The native `Model.RootBone` name must match exactly one parent joint.
The public `parentRoot` is that zero-based native joint ordinal, not a GLTF node
index; the client resolves it through `atpRig.jointNodes`.

The verified constructor reference is a stationary world actor at map position
zero. Native owner scale is `Model.Scale`; child scale and offsets come directly
from `AdditionalObject.Scale`, `OffsetX`, `OffsetY` and the boolean `LockRotate`.
`0x33392c` sets owner scale, `0x333988` selects layer 2, and `0x333a68` supplies the
constructor heading. The world method `0x7ef874` applies current root X/Z
compensation and owner offsets `[4, -2*32, 0]`. Root Y is not compensated.

For column vectors the native rotation constructors use:

```text
Ry(a) = [[cos(a), 0, -sin(a)], [0, 1, 0], [sin(a), 0, cos(a)]]
Rx(a) = [[1, 0, 0], [0, cos(a), sin(a)], [0, -sin(a), cos(a)]]
```

These signs differ from Three.js's positive-angle convention. Exact source
float32 angles, promoted to numbers, are:

```text
ground X = 0.0872664675116539
ground Y = 0.7853981852531433
heading  = 0.19634954631328583
locked child heading = 1.5707963705062866
```

`0x7eee28` builds the native ground basis. It also compresses map depth by
`0.10000000149011612`. The free 3D preview intentionally removes this map-depth
distortion from the relative linear transform while preserving native placement.
This is a documented display policy, not a claim of game screen-pixel parity.

Let `R = Rx(groundX) * Ry(groundY) * Ry(heading)`, `So` be owner scale and `Sc`
child scale. `K = Ry(heading)` when `LockRotate` is false, otherwise
`Ry(lockedChildHeading)`. For current root position `r = [rootX, 0, rootZ]`:

```text
linear      = (Sc / So) * inverse(R) * K
translation = r + inverse(R) * [OffsetX - 4, OffsetY + 64, 0] / So
GLTF placement = C * affine(linear, translation) * inverse(C)
```

The catalogue stores the matrix at the parent rest pose. The viewer adds the
current-minus-rest parent-root X/Z position in GLTF coordinates each frame.
This preserves the native root compensation without modifying the parent's
existing GLB or animations. Neither the root rotation nor its Y translation is
inherited by the child. Camera orbit rotates the view of the entire scene; it
does not change the native constructor-heading reference.

The generator obtains the exact rest root by accumulating the already validated
parent GLB's local joint transforms, excluding the GLB `ModelRoot` coordinate
rotation. It does not read the native binary a second time or guess a root by name
prefix, index zero or bone count.

## Publication and viewer contract

A row can contain:

```text
additionalActor: {
  model: { path, sha256, bytes, rigKey, animations: (number | AnimationRef)[] },
  transform: number[16], // column-major GLTF-space rest matrix
  parentRoot: number,   // zero-based native parent joint ordinal
  controller: "native-first-then-random-v1"
}
```

The nested model's animation rig keys match that model, not its parent. There
are no nested children. Normal generation uses `schemaVersion: 4` with a shared
`animationAssets` table. Parent and child animation lists contain indices into
that table; resolving them preserves the child's original CSV queue order.
Legacy inline `AnimationRef` entries remain readable. Direct exports use schema
1 for ordinary models, schema 2 for additional actors or material UV clocks,
and schema 3 for the earlier PonyBook interaction profile. Schema 4 is required
for the shared table or separate event bindings/SubObject controls, so older
clients reject these catalogues instead of silently omitting behavior. The child
retains the same strict independent controller described here.

Both parent and child bundles must finish successfully, and all their asset keys
and content digests must validate, before any scene files are staged. Shared child bundles
are reused only within the current build; placement is computed separately for
each owner. A failed child excludes the whole affected identity. No converted
child is inserted as a public top-level GameData identity.

The child has an independent animation mixer and lifetime. It can keep playing
while the parent rests, resets or switches its selected action. It shares the
existing scene, camera, renderer, page-hidden pause and disposal lifecycle.
Fetching, cancellation, hash validation, source GLTF cleanup and final disposal
must cover the complete scene; child clips never enter the parent's action menu.
Initial framing covers both actors. Future motion can still exceed initial
bounds, as with the existing single-actor framing limitation.

## Validation

`tests/test_model_additional.py` uses self-authored RK/ANIM/CSV inputs and the normal generation entry point. It covers independent rigs and native clip order, child-asset preservation when the parent rest pose changes, missing child resources, unencodable child clips, and failed child materials.

Real-resource coverage and browser/GPU acceptance are recorded separately in
private audit evidence. The existence of a valid association or a successful
format conversion alone is not proof of a supported compound scene. Unrelated
missing parent event files, unsupported shaders or other validation failures
continue to exclude the affected identity.

## Native CSV tail sampling

AdditionalObject alone explicitly requests the converter's `native-clamp` frame
boundary policy. Discovery retains its strict CSV diagnostics; preparation
revalidates the complete original CSV through the same shared parser under the
native policy, without filtering rows or matching error-message text. Blank,
malformed, duplicate and conflicting rows still prevent the whole child library
from being accepted, because native playback chooses numeric indices. Original
CSV order and duration remain intact. Export budgets are 10,000 declared frames
per clip and 1,000,000 total joint-frame samples including rest, enforced before
sample expansion. They are resource limits rather than native-format limits.

The same-version native loader/sampler/clock chain is documented in the converter
README. In the observed `bat_488502` library, the final `260..340` CSV clip has
81 samples at 15 FPS even though the ANIM has only 261 frames. The native sampler
repeatedly reads actual frame 260 for this 5.4-second clip. Conversion preserves
that behavior and all four numeric clip entries; it neither invents the missing
motion nor discards the final entry. The content author's intent cannot be
inferred from this native handling rule. Private child provenance records the
source frame count, original range and repeated-sample count.
