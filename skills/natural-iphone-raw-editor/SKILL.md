---
name: natural-iphone-raw-editor
description: Edit iPhone RAW/ProRAW and travel photos through the darktable-mcp bridge with a natural, memory-faithful look; use when the user describes a desired photo style rather than exact slider values.
---

# Natural iPhone RAW Editor

Use this skill when the user wants Claude/Codex to edit iPhone RAW/ProRAW/DNG photos in Darktable according to a subjective description such as "sunny," "professional," "natural," "like I remember the place," "not HDR," or "not too poppy."

The MCP server is only the bridge to Darktable. Do the creative judgment in the model loop:

1. Understand the requested look.
2. Inspect the image with `get_image_info`; if an Apple ProRAW starting point is recommended, apply it before creative edits.
3. Inspect the current render with `render_and_analyze`.
4. Apply a small set of native Darktable edits with `apply_edit_recipe` or `apply_adjustments`.
5. Render/analyze again.
6. Compare the image to the user's words and any reference image.
7. Iterate until the edit is close, then export full size with `export_image`.

Prefer native Darktable/XMP adjustments. Start globally, but use local adjustments when distinct regions such as sky, mountains, water, foreground, or trees need separate treatment.

## Useful bridge tools

- `get_darktable_status`: confirm Darktable, Adobe DNG Converter, native XMP support, and fallback status.
- `get_image_info`: inspect dimensions and current sidecar edits.
- `apply_starting_point`: apply a neutral first-pass profile, especially `apple_proraw_natural`, before creative edits when Apple ProRAW starts too dark/flat.
- `render_and_analyze`: render the current edit and return preview plus tone/color metrics.
- `compare_to_reference`: compare the current render with a reference JPEG when the user provides one. Read `regional_delta`, color/detail metrics, `diagnostic_warnings`, and `suggested_next_steps`.
- `apply_edit_recipe`: set several supported adjustments/crop/output name in one generic operation.
- `apply_adjustments`: set explicit slider-like fields.
- `crop_image`, `rotate_image`, `reset_crop`: geometry.
- `convert_dng_if_needed`: explicit ProRAW conversion diagnostics; normal render/export already auto-converts or retries when needed.
- `export_image`: final output. Use `quality=100` and do not set `max_dimension` unless the user asks for resizing.
- `cleanup_temporary_files`: manually remove MCP-generated previews/analysis renders if needed. Final export cleans these by default.

## Editing taste

For "natural sunny professional" iPhone travel photos:

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

Then call `render_and_analyze`. Adjust from the rendered result:

- Too dark overall: raise `exposure_ev` and/or `brightness`.
- Bright enough but dull: raise `whites`, `vibrance`, or a little `dehaze`.
- Washed out/flat: lower `shadows`, lower `blacks`, or increase `sigmoid_contrast`.
- Harsh/HDR: reduce `clarity`, `dehaze`, `sigmoid_contrast`, and saturation.
- Sky/water too artificial: reduce saturation/vibrance or adjust temperature/tint.
- Too soft compared with reference: raise `sharpness` toward 70, keep `sharpening_masking` around 70 to protect sky/noise, increase clarity/dehaze locally on mountains/trees, and avoid unnecessary denoise.
- Global metrics close but the image still feels wrong: inspect `regional_delta` and follow `suggested_next_steps`. Common fixes are sky saturation/blue depth, mountain clarity/dehaze/detail, lake brightness/color, and foreground shadow depth.

Stop only when the rendered preview satisfies the user's description and reference comparison has no important `diagnostic_warnings` or `suggested_next_steps` left. Export full size with `quality=100` and by omitting `max_dimension`.
