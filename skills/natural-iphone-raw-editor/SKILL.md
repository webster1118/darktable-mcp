---
name: natural-iphone-raw-editor
description: Edit iPhone RAW/ProRAW and travel photos through the darktable-mcp bridge with a natural, memory-faithful look; use when the user describes a desired photo style rather than exact slider values.
---

# Natural iPhone RAW Editor

Use this skill when the user wants Claude/Codex to edit iPhone RAW/ProRAW/DNG photos in Darktable according to a subjective description such as "sunny," "professional," "natural," "like I remember the place," "not HDR," or "not too poppy."

The MCP server is only the bridge to Darktable. Do the creative judgment in the model loop:

1. Understand the requested look.
2. Inspect the current render with `render_and_analyze`.
3. Apply a small set of native Darktable edits with `apply_edit_recipe` or `apply_adjustments`.
4. Render/analyze again.
5. Compare the image to the user's words and any reference image.
6. Iterate until the edit is close, then export full size with `export_image`.

Prefer native Darktable/XMP adjustments. Do not use MCP local masks unless the user asks for local masking or the global tools cannot solve a specific visible problem.

## Useful bridge tools

- `get_darktable_status`: confirm Darktable, Adobe DNG Converter, native XMP support, and fallback status.
- `get_image_info`: inspect dimensions and current sidecar edits.
- `render_and_analyze`: render the current edit and return preview plus tone/color metrics.
- `compare_to_reference`: compare the current render with a reference JPEG when the user provides one.
- `apply_edit_recipe`: set several supported adjustments/crop/output name in one generic operation.
- `apply_adjustments`: set explicit slider-like fields.
- `crop_image`, `rotate_image`, `reset_crop`: geometry.
- `convert_dng_if_needed`: explicit ProRAW conversion diagnostics; normal render/export already auto-converts or retries when needed.
- `export_image`: final output. Do not set `max_dimension` unless the user asks for resizing.

## Editing taste

For "natural sunny professional" iPhone travel photos:

- Aim for bright midtones without flattening the whole image.
- Preserve the feeling of sunlight; do not over-protect highlights until the photo becomes gloomy.
- Keep blacks and deep forest/tree areas believable. Avoid lifting shadows so far that the photo turns grey.
- Use dehaze and clarity moderately for distant mountains or haze, but avoid crunchy HDR texture.
- Prefer vibrance before heavy saturation. Increase saturation only when colors are clearly dull.
- Watch water and sky separately by eye. Turquoise water should stay clean, not radioactive; blue sky should stay natural, not cyan-plastic.
- If a reference edit exists, match its tonal/color direction, not necessarily every metric exactly.

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
    "sharpness": 12,
    "noise_reduction": 5
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

Stop when the rendered preview satisfies the user's description. Export full size by omitting `max_dimension`.
