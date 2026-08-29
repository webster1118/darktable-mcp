---
name: pweber-natural-travel-batch
description: Batch-edit Peter Weber's iPhone ProRAW/DNG travel photos through darktable-mcp using his Lightroom Adobe Standard vacation baseline; use for natural bright travel edits, not stylized HDR or exact Lightroom replication.
---

# Pweber Natural Travel Batch

Edit Peter Weber's iPhone ProRAW/DNG travel photos through darktable-mcp. Use the MCP as the bridge to Darktable. Start from Peter's deterministic Lightroom-to-Darktable preset translation, then make image-specific exposure, white balance, color, and local-mask refinements.

Do not rediscover the whole look from taste words on every image. The useful workflow is preset translation first, small intelligent corrections second.

## Lightroom-to-Darktable baseline

Peter's normal Lightroom workflow is:

1. Apply the vacation preset.
2. Set exposure.
3. Set white balance.
4. Perform color correction.
5. Add local masks for focus/unfocus or region-specific refinement.

Mirror that order in Darktable/MCP.

Important calibration facts:

- Lightroom may auto-select `Apple ProRAW` on import, but Peter changes the profile to `Adobe Standard`.
- Calibrate to Lightroom `Adobe Standard`, not Lightroom `Apple ProRAW`, unless Peter explicitly asks otherwise.
- Darktable's converted ProRAW base is about `1.2 EV` darker than Peter's Lightroom Adobe Standard base.
- Treat `+1.2 EV` as profile/base compensation, not as the creative exposure decision.
- Add Peter's per-photo exposure decision on top: `darktable exposure_ev = 1.2 + photo_exposure_ev`.
- Example: if Peter would set Lightroom exposure to `+0.2 EV`, use Darktable `+1.4 EV`. If he would set Lightroom exposure to `+1.2 EV`, use Darktable `+2.4 EV`.

When available, call `apply_pweber_lightroom_preset(image_path, photo_exposure_ev=...)` instead of manually recreating the baseline. This applies the translated vacation preset and performs the exposure math directly.

## Target taste

Deliver a sunny, clear, vivid, professional photograph that still looks believable and memory-faithful.

- Preserve the feeling of strong daylight. Do not make the whole image dull merely to recover every highlight.
- Prefer clean, polished contrast with bright midtones, controlled highlights, and real black depth.
- Keep trees and foreground readable without lifting them into grey or allowing large blocked-black areas.
- Give mountains and rocks presence with restrained clarity/dehaze; avoid halos, crunchy texture, and an HDR look.
- Keep greens rich and natural, not fluorescent.
- Make skies clearly blue rather than pale grey-blue, but avoid cyan-plastic color.
- Keep lake water clean turquoise/blue where appropriate. It may be vivid, but must not become neon or uniformly painted.
- Prefer vibrance and regional color separation over heavy global saturation.
- Use sharpening near 70 and sharpening masking near 70 as Peter's normal final-detail baseline. Judge sharpness only in a non-fast render.

## Batch strategy

Maintain a consistent visual family, not identical slider values.

1. Enumerate the requested source files and preserve originals. Use a unique output suffix such as `_pweber_natural_v1`; never overwrite an existing export unless explicitly asked.
2. Call `get_darktable_status` once at batch start. Call `get_image_info` for every image.
3. For Peter's vacation style, call `apply_pweber_lightroom_preset` first. Choose `photo_exposure_ev` as the per-photo exposure Peter would normally set after the preset; start at `0.0` if unknown, not by removing the `+1.2 EV` compensation.
4. If the batch has varied scenes, calibrate on up to three representative images: bright sky/water, shadow-heavy foreground, and mountains or distant detail. When Peter is present, show these before propagating the style direction.
5. Re-evaluate exposure, white balance, contrast, vibrance, and haze per image.
6. Add local native masks only where regions need separate treatment. Do not reuse mask coordinates across images.
7. Iterate with `render_and_analyze(fast=true, compare_to_original=false, max_size=900 or 1200)` for each image. Inspect both the rendered pixels and regional metrics.
8. Use `compare_to_reference` when a relevant reference is supplied. Match its tonal and color direction while preserving Peter's preference for a natural result.
9. Run `render_and_analyze(fast=false, compare_to_original=false, max_size=1200)` for every image before export. Check composition, mask edges, sky smoothness, detail, clipping, and the entire lower edge.
10. Export only clean checkpoints at full resolution with JPEG quality 100, no `max_dimension`, XMP sidecars enabled, and temporary preview cleanup enabled.

If an image fails, isolate it and continue safe work on the rest of the batch. Report failed or skipped files and the reason; do not silently export a compromised image.

## Per-image corrections

- Exposure: adjust on top of the `+1.2 EV` base compensation, not instead of it.
- White balance: set after the preset/exposure baseline. Correct warmth if it contaminates sky or water.
- Color correction: prefer regional color separation over global saturation.
- Sky: separate locally if needed. Brighter is not the same as bluer; inspect blue dominance and red/blue balance.
- Mountains: add restrained local clarity/dehaze only when distance looks hazy or soft.
- Lake/water: use a path or carefully placed gradient following the shoreline; avoid neon cyan.
- Trees/foreground: lift shadows enough to reveal structure, then retain meaningful blacks.
- Composition: do not impose one crop on a batch. For wide lake landscapes with excessive empty sky, a 16:9 crop can work, but preview it.

## Quality gates

- Treat `reference_quality_gates.must_not_finalize=true` as blocking unless Peter explicitly accepts the mismatch.
- Reject results that are gloomy, flat, globally over-saturated, neon, heavily shadow-lifted, haloed, crunchy, or visibly HDR-like.
- Before declaring a batch complete, report output paths, dimensions, export quality, XMP creation, skipped files, and image-specific deviations.

## Learning loop

Treat Peter's feedback as evidence about taste, not as universal slider values. After accepted batches, summarize only stable preferences that recur across multiple images and propose narrow skill updates.
