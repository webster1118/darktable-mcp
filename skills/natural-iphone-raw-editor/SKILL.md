---
name: natural-iphone-raw-editor
description: Edit iPhone RAW/ProRAW and travel photos through the darktable-mcp bridge with a natural, memory-faithful look; use when the user describes a desired photo style rather than exact slider values.
---

# Natural iPhone RAW Editor

Use this skill when the user wants Claude/Codex to edit iPhone RAW/ProRAW/DNG photos in Darktable according to a subjective description such as "sunny," "professional," "natural," "like I remember the place," "not HDR," or "not too poppy."

The MCP server is only the bridge to Darktable. Do the creative judgment in the model loop:

1. Understand the requested look.
2. Inspect the image with `get_image_info`; if an Apple ProRAW starting point is recommended, apply it before creative edits.
3. Inspect the current render with `render_and_analyze`. For normal iterations use `fast=true` and keep `compare_to_original=false`; enable baseline/reference comparisons only for the first render or checkpoint checks.
4. Apply a small set of native Darktable edits with `apply_edit_recipe` or `apply_adjustments`.
5. Render/analyze again.
6. Compare the image to the user's words and any reference image.
7. Iterate until the edit is close, then export full size with `export_image`.

Prefer native Darktable/XMP adjustments. Start globally, but use local adjustments when distinct regions such as sky, mountains, water, foreground, or trees need separate treatment.
Linear-gradient, ellipse, path, brush, and parametric local adjustments are written as native Darktable masks when the adjustment fields map to native modules. MCP local mask coordinates are still relative to the visible rendered frame after crop; the XMP writer translates safe drawn masks back to Darktable's source-image coordinates. Fine crop rotation is written natively through Darktable's rotate-and-perspective module. Judge and place masks using the cropped preview, not the original uncropped DNG coordinates.

## Useful bridge tools

- `get_darktable_status`: confirm Darktable, Adobe DNG Converter, native XMP support, and fallback status.
- `get_image_info`: inspect dimensions and current sidecar edits.
- `apply_starting_point`: apply a neutral first-pass profile, especially `apple_proraw_natural`, before creative edits when Apple ProRAW starts too dark/flat.
- `apply_pweber_lightroom_preset`: apply Peter's Lightroom Adobe Standard vacation baseline. It uses Darktable `+1.2 EV` as profile/base compensation and adds the requested per-photo exposure on top.
- `render_and_analyze`: render the current edit and return preview plus tone/color metrics. For speed during iteration, call it with `fast=true`, `max_size` around 900-1200, and `compare_to_original=false`. Use `compare_to_original=true` only for first/checkpoint renders because it costs an extra Darktable render.
- `compare_to_reference`: compare the current render with a reference JPEG when the user provides one. Read `regional_delta`, color/detail metrics, `diagnostic_warnings`, and `suggested_next_steps`.
- `apply_edit_recipe`: set several supported adjustments/crop/output name in one generic operation.
- `apply_adjustments`: set explicit slider-like fields.
- `crop_image`, `rotate_image`, `reset_crop`: geometry.
- `add_gradient_mask`: add a native Darktable drawn gradient mask; use this for sky, water, broad mountain, and foreground regional corrections.
- `add_ellipse_mask`: add a native Darktable ellipse mask; use this for circular/oval subjects, bright patches, or localized regions.
- `add_path_mask`: add a native Darktable path mask from points; use this for mountains, shorelines, buildings, or irregular regions when a gradient is too broad.
- `add_brush_mask`: add a native Darktable brush mask from stroke points; use this for loose hand-painted refinements.
- `add_parametric_mask`: add a native Darktable parametric mask by luminance/color channel; use this for tonal/color targeting, then verify with region metrics.
- `add_ai_object_mask`: currently reports unsupported from sidecar-only CLI. Use path/ellipse/brush instead.
- `convert_dng_if_needed`: explicit ProRAW conversion diagnostics; normal render/export already auto-converts or retries when needed.
- `export_image`: final output. Use `quality=100` and do not set `max_dimension` unless the user asks for resizing.
- `cleanup_temporary_files`: manually remove MCP-generated previews/analysis renders if needed. Final export cleans these by default.

## Performance workflow

- Apple ProRAW conversion is expensive. The first render may still be slow, but converted files are cached in `.darktable-mcp-converted` and should be reused by later preview/final export passes.
- Darktable CLI also uses a reusable MCP config/cache directory by default, so repeated renders should avoid recreating the whole Darktable environment.
- Use `render_and_analyze(fast=true, compare_to_original=false)` for most creative iterations. Fast mode disables final sharpening/noise reduction and uses Darktable's non-HQ render path, so it is for tone/color/mask placement, not final detail judgment.
- Before deciding the edit is finished, run one normal render/check: `render_and_analyze(fast=false, compare_to_original=true)` or `compare_to_reference(fast=false)` when a reference exists.
- For batches, do not run a full creative feedback loop on every image. Edit one representative image, copy/apply the recipe to similar images, run quick checks on outliers, then perform one final full-size `export_image(quality=100)` pass per selected photo.
- If batch exports are later parallelized, use separate Darktable config directories per worker to avoid Darktable cache/catalog contention.

## Editing taste

For "natural sunny professional" iPhone travel photos:

- Peter's Lightroom workflow is: apply preset, set exposure, set white balance, perform color correction, then add local masks for focus/unfocus areas. Follow this order when using his personal preset translation.
- Peter uses Lightroom's `Adobe Standard` profile, even though Lightroom may auto-select `Apple ProRAW` on import. Do not calibrate to Lightroom's Apple ProRAW profile unless he explicitly asks.
- In Darktable, Peter's Lightroom Adobe Standard base is approximately `+1.2 EV` brighter than the converted ProRAW/Darktable base. Treat this as profile compensation, not creative exposure.
- When translating Peter's Lightroom exposure decision, use `darktable exposure_ev = 1.2 + photo_exposure_ev`. Example: if Peter would set Lightroom exposure to `+0.2`, use Darktable/MCP `+1.4 EV`; if he would set `+1.2`, use `+2.4 EV`.
- Prefer `apply_pweber_lightroom_preset(photo_exposure_ev=...)` when editing in Peter's vacation style so the MCP performs this exposure math directly.
- Apple ProRAW often starts too dark and flat in a neutral Darktable render. If `get_image_info` or `get_current_edits` recommends `apple_proraw_natural`, call `apply_starting_point` before subjective editing. Treat this as RAW normalization, not the final look.
- Aim for bright midtones without flattening the whole image.
- Preserve the feeling of sunlight; do not over-protect highlights until the photo becomes gloomy.
- Keep blacks and deep forest/tree areas believable. Avoid lifting shadows so far that the photo turns grey.
- Use dehaze and clarity moderately for distant mountains or haze, but avoid crunchy HDR texture.
- Prefer vibrance before heavy saturation. Increase saturation only when colors are clearly dull.
- Watch water and sky separately by eye. Turquoise water should stay clean, not radioactive; blue sky should stay natural, not cyan-plastic.
- If a reference edit exists, match its tonal/color direction, not necessarily every metric exactly.
- For final full-quality iPhone RAW exports, treat the user's normal Lightroom detail baseline as sharpening 70 and masking 70. In MCP terms, start final detail passes around `sharpness: 70` and `sharpening_masking: 70`, then adjust by comparing real detail metrics and inspecting the image. This masking means edge-protected sharpening, not a local subject mask.
- Do not judge final sharpness from `fast=true` renders. Fast renders disable sharpening/noise reduction for speed. Use `compare_to_reference(..., fast=false)` when a reference is available.
- When a reference is available, do not stop just because global metrics are close. Inspect `regional_delta`, `colorfulness`, channel means, and `suggested_next_steps` for sky, mountains, lake, foreground, and center subject. If one region is off, prefer a targeted local adjustment over shifting the whole image.
- When `compare_to_reference` returns `reference_quality_gates.must_not_finalize=true`, do not export or call the result finished unless the user explicitly accepts the mismatch. Fix the named failures first.
- For sky matching, inspect `blue_dominance` and `red_blue_gap`, not only saturation. If the sky is pale/grey, raise blue separation with a stronger sky-only mask and avoid global warmth that pushes red into the sky.
- For a visible sky after a 16:9 crop, use a top-down local gradient on the cropped frame (for example `start_y: 0.0`, `end_y: 0.25` to `0.35`, `invert: true`) and verify the sky region changes in `regional_delta`; do not reuse tiny source-frame sky masks that may miss the cropped sky. If repeated sky requests barely change the result, make one stronger `add_gradient_mask` pass and compare the sky region numerically before changing global color.
- If the sky/mountains/trees need a more precise local edit than a gradient, use `add_path_mask` or `add_brush_mask` instead of pushing global saturation/clarity too far.
- Avoid combining fine crop rotation with local drawn masks unless necessary; verify mask placement carefully because that path may fall back to MCP finishing for correctness.
- Keep noise reduction conservative for bright daylight iPhone RAW unless noise is visible. Too much denoise can produce the soft, smeared look the user dislikes.

## Practical iteration pattern

Start with broad global edits, then refine:

```text
apply_edit_recipe({
  "adjustments": {
    "exposure_ev": 0.5,
    "brightness": 6,
    "highlights": 15,
    "shadows": 15,
    "whites": 5,
    "blacks": -5,
    "sigmoid_contrast": 10,
    "sigmoid_skew": -5,
    "vibrance": 12,
    "saturation": 4,
    "dehaze": 8,
    "clarity": 6,
    "sharpness": 50,
    "sharpening_masking": 70,
    "noise_reduction": 2
  },
  "crop": {"top": 0.125, "bottom": 0.875},
  "clear_local_masks": true
})
```

Then call `render_and_analyze(fast=true, compare_to_original=false)`. Adjust from the rendered result:

- Too dark overall: raise `exposure_ev` and/or `brightness`.
- Bright enough but dull: raise `whites`, `vibrance`, or a little `dehaze`.
- Washed out/flat: lower `shadows`, lower `blacks`, or increase `sigmoid_contrast`.
- Harsh/HDR: reduce `clarity`, `dehaze`, `sigmoid_contrast`, and saturation.
- Sky/water too artificial: reduce saturation/vibrance or adjust temperature/tint.
- Too soft compared with reference: raise `sharpness` toward 70, keep `sharpening_masking` around 70 to protect sky/noise, increase clarity/dehaze locally on mountains/trees, and avoid unnecessary denoise.
- Global metrics close but the image still feels wrong: inspect `regional_delta` and follow `suggested_next_steps`. Common fixes are sky saturation/blue depth, mountain clarity/dehaze/detail, lake brightness/color, and foreground shadow depth.

Stop only after a non-fast checkpoint render satisfies the user's description and reference comparison has no important `diagnostic_warnings` or `suggested_next_steps` left. Export full size with `quality=100` and by omitting `max_dimension`.
