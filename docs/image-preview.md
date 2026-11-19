# Images in the preview pane

An image is a document kind like markdown, HTML and PDF, with one difference that reshapes everything
around it: **it exists before it has bytes**. `generate_image` opens the tab the moment it knows the
output path and the requested size, draws for 20 to 180 seconds, then replaces the placeholder with the
file in the same rectangle. That sequence is what this document is about.

Related: `docs/preview-pane-handoff.md` (the pane itself, S1-S8), `docs/protocol.md` (the wire),
`~/.pi/agent/plans/pi-chat-imagegen-preview.md` (the plan this implements).

## The contract on the wire

Two persisted entries per image, plus ephemeral progress in between.

| Moment | Event | Says |
|---|---|---|
| draw starts | `entry_appended` `op:"open"`, `kind:"image"`, `state:"rendering"`, `canvas:"2048x2048"` | a tab, a shape, and no bytes |
| every ~400 ms | `tool_execution_update`, `partialResult.details.{doc, progress}` | phase, percent, step/total, ETA |
| draw ends | `entry_appended` `op:"update"`, `state:"ready"`, `file:"<docId>.png"`, `canvas` | the file, and the size that was actually rendered |
| draw fails | same entry with `state:"error"` and `note` | why, in the tool's own words |

`details.doc` on every update is what ties a progress tick to a tab. `v` stays at 2: `state`, `canvas`
and `watch` are additive fields, and a reader that predates them ignores them rather than
mis-rendering. `ENTRY_FIELDS` in `pi_chat/preview.py` is the filter that lets them survive a session
replay, and it is mirrored in `pi_chat/extension/preview.ts` and `normalize()` in `static/preview.js`.

**Progress is not persisted, on purpose.** sd-cli speaks every 400 ms; a 200-step draw is several
hundred entries, and every later reopen of that session would pay to load a percent that was stale the
moment the draw ended. The entry carries the state; the update stream carries the animation. If a future
change persists them, `tool_execution_update` must stay out of the session file.

Both writers are the tool itself (`~/.pi/agent/extensions/image-gen.ts`), not this repository: it emits
`pichat_preview` custom entries when `PI_CHAT_PREVIEW_DIR` is set, which is only ever true for a
pi-chat child. Terminal pi gets a progress line on the tool call instead and no pane at all.

## States, and what each one is allowed to claim

`static/preview.js` owns the state machine; `static/image-loader.js` owns the picture and the labels.

| `state` | Body | Header | Claims |
|---|---|---|---|
| `rendering` | dots + phase + percent | no Open, no Save | nothing about bytes; percent is capped at 99 |
| `ready`, not yet decoded | stilled dots behind the `<img>` | no Save | "Loading" |
| `ready`, decoded | the picture + zoom bar | Save, Open | the file is on screen |
| `error` | the tool's note, nothing else | no Save | the reason, once, in the stage (the note strip is suppressed so it does not appear twice) |
| `rendering` and quiet | dots + "No word from this draw" | no Save | that it might have died, not that it did |

Two rules worth defending:

- **The percent never reaches 100 while rendering.** sd-cli's own ceiling is 99 and the result carries
  100. After a draw the GPU handback can take a minute while the session is still generating tokens, so
  a bar parked at 99 is honest and one at 100 with no image is a lie.
- **`available: false` is not news about a missing file** while a document is `rendering` or `error`:
  both are states where the absence of bytes is expected. The "cleaned up after a while" copy belongs to
  a reopened session, and winning over a live draw is what made that copy appear mid-draw in jsdom.

## The placeholder is the picture's box

`boxFor()` in `static/image-loader.js` returns contain-with-no-upscale: the requested pixels, clamped to
the stage, and never enlarged past one image pixel per CSS pixel. That is exactly what the finished
`<img>` does, so **the swap moves nothing** - measured by `--stage image`, which fails if the image's
rectangle differs from the placeholder's by more than 2 px. It also makes the placeholder informative:
200x200 draws a small card, 1024x512 draws a wide one, 2048x2048 fills the column.

The one exception is `MIN_BOX` (168 px): below it the dots and the percent chip cannot share the box, so
a tiny request draws at the floor and the caption carries the real pixels. `floored` is returned rather
than hidden because the caller prints the difference.

Zoom and pan are a single `transform` on the `<img>`, with the stage `overflow: hidden`. Not a scroll
container, never a width or height change: the invariant is that nothing in this column may resize the
conversation (docs/composer.md), and `--stage image` measures `#messages` before and after a wheel zoom
and a drag, in a real browser, and fails if it moved by more than half a pixel.

## The dot matrix

One `<canvas>`, not ~1300 SVG circles: every dot changes size and colour each frame, and DOM churn of
that size inside the pane's grid track is exactly the layout risk the rest of this file avoids. DPR is
capped at 2.

The hexagon is the only lit thing (Ben, 2026-11-19). Inside it, dots grow and take the accent on a
falloff from the hexagon's centre that **plateaus and then gives up in the last quarter** - a straight
radial falloff leaves a round glow and loses the six sides, which two screenshots caught in sequence.
There is no outline: the dots are the edge. The shape climbs the field as the percent climbs, which is
what makes the matrix read as progress and not only as activity, and it keeps drifting sideways so it
never looks parked.

Colours come from `--doc-loader-accent` and `--doc-loader-base` in `theme.css` (Lava on Oat in light per
Ben, the lifted dark accent in dark) and are read once per start, per resize, and on a `data-theme`
mutation - never per frame, because `getComputedStyle` is a style resolution at 60 Hz.

`prefers-reduced-motion` keeps everything and moves nothing: the hexagon parks at the progress height as
a still frame, the level still fills via `setProgress`, the percent still counts, and `start()` paints
one frame instead of running rAF. The loop also stops when the pane is closed, when the tab is hidden,
and while a session is being replayed.

## Images are stored copies, never `path` references

`~/Images` is outside `preview.allowed_roots`, so an entry pointing at the original paints the pane's
"file is gone" state where the loader belongs. The tool copies the bytes into the connection's own scope
and names the file `<docId>.png`. A *referenced* image (`preview open path=…`) is allowed for
png/jpeg/webp/gif inside the roots. `.svg` is in neither list: it is script, and every other path into
this pane assumes model output is untrusted. The response CSP is `default-src 'none'; frame-ancestors
'none'`, and the parent CSP needs nothing new because `img-src 'self'` already covers it.

## How this is tested

| Surface | Covers |
|---|---|
| `tests/test_preview_api.py`, `test_preview_security.py` | the route: content type, unframable CSP, cross-profile 404, kind mismatch, svg refusal, freshness, replay carrying `state`/`canvas`/`watch` |
| `tools/test_preview_image.mjs` (25 claims, run by `tests/test_preview_image.py`) | the state machine against the real markup: refused kinds, dots-vs-picture, ephemeral progress, the download name, zoom and clamp, the error copy, and `boxFor`'s geometry table |
| `--stage image` (needs `PI_CHAT_PI_BIN=tools/fake_rpc_pi.py FAKE_RPC_IMAGE=1`) | the real wire through `app.js`: placeholder aspect equals the requested canvas, no fetch before bytes, the swap does not move the picture, zoom does not move the conversation |
| `--stage image-cancel` (add `FAKE_RPC_IMAGE_ABORT_AFTER=3`) | a cancelled draw stops pulsing, shows the reason once, and offers no Save |

The stub's progress stream is copied out of `data-samples/imagegen_rpc_capture.jsonl` - the real 512x512
draw, including the updates where the percent repeats while the weights load. It writes a hand-rolled
PNG into the connection's preview directory, because a `ready` state tested against a 404 proves nothing.

## Where to change things

| You are changing | File |
|---|---|
| the kind tables, CSP, entry filtering, freshness | `pi_chat/preview.py` |
| what the tool reports, and the preview entries it writes | `~/.pi/agent/extensions/image-gen.ts` |
| states, zoom, download, the stalled clock | `static/preview.js` |
| the dots, the labels, the placeholder geometry | `static/image-loader.js` |
| the loader's colours | `static/theme.css` |
| the pane's markup for this kind | `static/index.html` |
| the stubbed draw and its stages | `tools/fake_rpc_pi.py`, `tools/screenshot_ui.mjs` |
