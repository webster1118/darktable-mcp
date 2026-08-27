"""Darktable MCP Server — photo editing tools for Claude."""
from __future__ import annotations

import io
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer, Image
from PIL import Image as PILImage

from .darktable_cli import DarktableCli
from .edits import CropState, EditState, LocalAdjustmentState
from .processor import ALL_EXTENSIONS, RAW_EXTENSIONS, ImageProcessor
from .xmp_writer import write_xmp

mcp = MCPServer("DarktableMCP")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


DARKTABLE = DarktableCli.discover()

VACATION_PROFILE = {
    "exposure_ev": 0.65,
    "brightness": 6.0,
    "contrast": 6.0,
    "highlights": 20.0,
    "shadows": 28.0,
    "whites": 5.0,
    "sigmoid_contrast": 18.0,
    "sigmoid_skew": -8.0,
    "vibrance": 8.0,
    "clarity": 8.0,
    "dehaze": 18.0,
    "sharpness": 18.0,
    "noise_reduction": 6.0,
}


def _load_or_new(image_path: str) -> tuple[Path, EditState]:
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    state = EditState.load(path) or EditState(source_path=path)
    return path, state


def _quick_preview(path: Path, state: EditState, max_size: int):
    """
    Render a preview using Darktable for RAW files so Claude sees the same
    rendering engine that is used for the final RAW export.

    Raster images continue to use the existing Pillow-based pipeline.
    """

    # RAW files MUST go through Darktable.
    if path.suffix.lower() in RAW_EXTENSIONS:
        if not DARKTABLE:
            raise RuntimeError(
                "Darktable CLI was not found. "
                "RAW previews require darktable-cli; "
                "rawpy is intentionally not used for RAW previews."
            )

        # Create/update the Darktable XMP from the current MCP edit state.
        xmp_path = write_xmp(state)

        # Use a predictable local cache directory instead of %TEMP%.
        preview_dir = path.parent / ".darktable-mcp-preview"
        preview_dir.mkdir(parents=True, exist_ok=True)

        preview_path = preview_dir / f"{path.stem}__preview.jpg"

        # Delete an old preview so we know the output was freshly rendered.
        if preview_path.exists():
            try:
                preview_path.unlink()
            except OSError:
                pass

        result = _export_via_darktable_cli(
            src=path,
            dst=preview_path,
            xmp=xmp_path,
            quality=85,
            max_dimension=max_size,
        )

        if result.get("status") != "ok":
            raise RuntimeError(
                "Darktable failed to render the RAW preview: "
                + str(result)
            )

        if not preview_path.exists():
            raise RuntimeError(
                f"Darktable reported success but preview was not created: "
                f"{preview_path}"
            )

        _finish_darktable_render(preview_path, state, preview_path, max_dimension=max_size)
        img_bytes = preview_path.read_bytes()

        return Image(data=img_bytes, format="jpeg"), img_bytes

    # Non-RAW files can continue to use the existing Pillow pipeline.
    proc = ImageProcessor(path)
    img = proc.process(state, preview_size=max_size)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)

    return Image(data=buf.getvalue(), format="jpeg"), buf.getvalue()


def _darktable_finish_state(state: EditState) -> EditState:
    """Return the adjustment state still needed after Darktable renders RAW."""
    finishing = replace(state)
    finishing.adjustments = replace(
        state.adjustments,
        exposure_ev=0.0,
        black_level=0.0,
        temperature_kelvin=None,
        tint=0.0,
        brightness=0.0,
        contrast=0.0,
        highlights=0.0,
        shadows=0.0,
        whites=0.0,
        blacks=0.0,
        sigmoid_contrast=0.0,
        sigmoid_skew=0.0,
        saturation=0.0,
        vibrance=0.0,
        sharpness=0.0,
        noise_reduction=0.0,
        vignette=0.0,
        clarity=0.0,
        dehaze=0.0,
    )
    if _crop_is_native_darktable_safe(finishing):
        finishing.crop = None
    return finishing


def _crop_is_native_darktable_safe(state: EditState) -> bool:
    return (
        state.crop is not None
        and abs(state.crop.rotation) <= 0.01
        and not state.local_adjustments
    )


def _needs_mcp_finishing(state: EditState) -> bool:
    adj = state.adjustments
    return (
        bool(state.local_adjustments)
        or bool(state.crop and not _crop_is_native_darktable_safe(state))
    )


def _save_image(img: PILImage.Image, output_path: Path, format_name: str, quality: int) -> None:
    normalized = format_name.lower()
    if normalized in ("jpeg", "jpg"):
        img.save(output_path, format="JPEG", quality=quality, optimize=True)
    elif normalized == "png":
        img.save(output_path, format="PNG", optimize=True)
    elif normalized in ("tiff", "tif"):
        img.save(output_path, format="TIFF")
    else:
        img.save(output_path, format="JPEG", quality=quality, optimize=True)


def _finish_darktable_render(
    rendered_path: Path,
    state: EditState,
    output_path: Path,
    *,
    format_name: str = "jpeg",
    quality: int = 92,
    max_dimension: Optional[int] = None,
) -> None:
    """Apply MCP-side finishing after a Darktable RAW render."""
    with PILImage.open(rendered_path) as src_img:
        img = src_img.convert("RGB")

    finishing_state = _darktable_finish_state(state)
    proc = ImageProcessor(rendered_path)
    img = proc._apply_post_raw(img, finishing_state.adjustments)
    img = proc._apply_local_adjustments(img, finishing_state.local_adjustments)
    if finishing_state.crop:
        img = proc._apply_crop(img, finishing_state.crop)
    if max_dimension:
        img.thumbnail((max_dimension, max_dimension), PILImage.LANCZOS)
    _save_image(img, output_path, format_name, quality)


def _luminance_metrics(image_path: Path) -> dict:
    import numpy as np

    with PILImage.open(image_path) as img:
        arr = np.array(img.convert("RGB"), dtype=np.float32)
    lum = 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
    return {
        "mean": round(float(lum.mean()), 2),
        "p05": round(float(np.percentile(lum, 5)), 2),
        "p50": round(float(np.percentile(lum, 50)), 2),
        "p95": round(float(np.percentile(lum, 95)), 2),
        "shadow_clip_pct": round(float((lum <= 5).mean() * 100), 3),
        "highlight_clip_pct": round(float((lum >= 250).mean() * 100), 3),
        "contrast_span": round(float(np.percentile(lum, 95) - np.percentile(lum, 5)), 2),
    }


def _apply_vacation_feedback(state: EditState, metrics: dict) -> list[str]:
    """Adjust the vacation profile from measured preview luminance."""
    adj = state.adjustments
    changes: list[str] = []

    if metrics["mean"] < 108 or metrics["p50"] < 92:
        old = adj.exposure_ev
        adj.exposure_ev = min(adj.exposure_ev + 0.3, 1.45)
        if adj.exposure_ev != old:
            changes.append("raised exposure")

        old = adj.shadows
        adj.shadows = min(adj.shadows + 12.0, 70.0)
        if adj.shadows != old:
            changes.append("lifted shadows")

        old = adj.brightness
        adj.brightness = min(adj.brightness + 3.0, 20.0)
        if adj.brightness != old:
            changes.append("raised brightness")

    if metrics["p05"] < 18 and metrics["shadow_clip_pct"] > 0.05:
        old = adj.shadows
        adj.shadows = min(adj.shadows + 8.0, 75.0)
        if adj.shadows != old:
            changes.append("opened deep shadows")

    if metrics["p95"] > 235 or metrics["highlight_clip_pct"] > 0.5:
        old = adj.highlights
        adj.highlights = min(adj.highlights + 12.0, 65.0)
        if adj.highlights != old:
            changes.append("protected highlights")
    elif metrics["p95"] < 198:
        old = adj.whites
        adj.whites = min(adj.whites + 5.0, 25.0)
        if adj.whites != old:
            changes.append("raised whites")

    if metrics["contrast_span"] < 125 and metrics["highlight_clip_pct"] < 0.75:
        old = adj.dehaze
        adj.dehaze = min(adj.dehaze + 8.0, 42.0)
        if adj.dehaze != old:
            changes.append("added dehaze")

        old = adj.clarity
        adj.clarity = min(adj.clarity + 4.0, 28.0)
        if adj.clarity != old:
            changes.append("added local contrast")

    return changes


def _render_feedback_preview(path: Path, state: EditState, output_path: Path, max_dimension: int = 900) -> dict:
    if path.suffix.lower() not in RAW_EXTENSIONS:
        proc = ImageProcessor(path)
        img = proc.process(state, preview_size=max_dimension)
        _save_image(img, output_path, "jpeg", 85)
        return {"status": "ok", "rendered_via": "pillow"}

    if not DARKTABLE:
        return {"status": "error", "error": "darktable-cli is not available"}

    xmp_path = write_xmp(state)
    darktable_out = output_path.with_name(output_path.stem + "__darktable.jpg")
    result = _export_via_darktable_cli(
        src=path,
        dst=darktable_out,
        xmp=xmp_path,
        quality=85,
        max_dimension=max_dimension,
    )
    if result.get("status") == "ok":
        _finish_darktable_render(
            darktable_out,
            state,
            output_path,
            format_name="jpeg",
            quality=85,
            max_dimension=max_dimension,
        )
    return result


def _auto_tune_vacation_photo(path: Path, state: EditState, iterations: int = 3) -> list[dict]:
    history: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="darktable-mcp-feedback-") as tmp:
        tmp_path = Path(tmp)
        for index in range(iterations):
            preview_path = tmp_path / f"vacation-feedback-{index}.jpg"
            result = _render_feedback_preview(path, state, preview_path)
            if result.get("status") != "ok":
                history.append({
                    "iteration": index + 1,
                    "status": "error",
                    "error": result.get("error", "preview render failed"),
                })
                break

            metrics = _luminance_metrics(preview_path)
            changes = _apply_vacation_feedback(state, metrics)
            history.append({
                "iteration": index + 1,
                "status": "ok",
                "metrics": metrics,
                "changes": changes,
            })
            if not changes:
                break
    return history


def _save_and_open_preview(path: Path, img_bytes: bytes) -> str:
    """Save preview to disk next to the source file and open it."""
    preview_path = path.with_name(path.stem + "__preview.jpg")
    preview_path.write_bytes(img_bytes)
    try:
        import os
        os.startfile(str(preview_path))   # Windows: opens in default image viewer
    except Exception:
        pass
    return str(preview_path)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_images(directory: str) -> list[dict]:
    """List all supported image files (RAW and raster) in *directory*.

    Returns a list of dicts with keys: path, name, type, has_edits, output_name.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        return [{"error": f"Directory not found: {directory}"}]

    results = []
    for file in sorted(dir_path.iterdir()):
        if file.suffix.lower() not in ALL_EXTENSIONS:
            continue
        state = EditState.load(file)
        results.append({
            "path": str(file),
            "name": file.name,
            "type": "raw" if file.suffix.lower() in RAW_EXTENSIONS else "image",
            "has_edits": state.has_changes() if state else False,
            "output_name": state.output_name if state else None,
        })
    return results


@mcp.tool()
def get_image_info(image_path: str) -> dict:
    """
        Return detailed metadata (EXIF, dimensions, camera info) and current edit state for an image.
        
        IMPORTANT:
        Claude does NOT need direct access to the file.
        Pass the absolute Windows path to the MCP server.
        The MCP server reads the file and returns the preview.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    proc = ImageProcessor(path)
    info = proc.get_info()
    info["edits"] = state.to_dict()
    info["darktable_cli_available"] = DARKTABLE is not None
    info["darktable_cli_path"] = str(DARKTABLE.executable) if DARKTABLE else None
    return info


@mcp.tool()
def get_darktable_status() -> dict:
    """Return Darktable CLI discovery and MCP-sidecar support details."""
    return {
        "darktable_cli_available": DARKTABLE is not None,
        "darktable_cli_path": str(DARKTABLE.executable) if DARKTABLE else None,
        "adobe_dng_converter_available": DARKTABLE.dng_converter is not None if DARKTABLE else False,
        "adobe_dng_converter_path": str(DARKTABLE.dng_converter.executable) if DARKTABLE and DARKTABLE.dng_converter else None,
        "dng_conversion_mode": "auto; override with DARKTABLE_MCP_DNG_CONVERSION=auto|always|never",
        "raw_rendering": "darktable-cli" if DARKTABLE else "unavailable",
        "vacation_profile_feedback": "render-measure-adjust loop",
        "mask_support": ["linear_gradient"],
        "xmp_supported_adjustments": [
            "exposure_ev",
            "black_level",
            "temperature_kelvin",
            "tint",
            "brightness",
            "contrast",
            "highlights",
            "shadows",
            "whites",
            "blacks",
            "saturation",
            "vibrance",
            "sigmoid_contrast",
            "sigmoid_skew",
            "dehaze",
            "crop_without_rotation",
            "sharpness",
            "noise_reduction",
            "clarity",
            "vignette",
        ],
        "mcp_finishing_adjustments": [
            "rotated_crop",
            "linear_gradient_masks",
        ],
        "xmp_not_yet_mapped": [
            "darktable-native masks",
            "darktable-native crop rotation",
        ],
    }


@mcp.tool()
def get_image_preview(image_path: str, max_size: int = 1200) -> list:
    """Render the image with current edits.

    IMPORTANT:
    Claude does NOT need direct access to the file.
    Pass the absolute Windows path to the MCP server.
    The MCP server reads the file and returns the preview.

    RAW/DNG files are rendered through Darktable CLI so the preview shown
    to Claude uses the same RAW rendering engine as the final export.

    JPEG/PNG/etc. continue to use the existing Pillow pipeline.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return [f"Error: {e}"]

    try:
        _img, img_bytes = _quick_preview(path, state, max_size)
    except Exception as e:
        return [f"Error rendering preview: {e}"]

    preview_path = _save_and_open_preview(path, img_bytes)

    return [
        (
            f"Preview saved to: {preview_path}\n"
            "Opening in your image viewer now..."
        ),
        Image(data=img_bytes, format="jpeg"),
    ]


@mcp.tool()
def apply_adjustments(
    image_path: str,
    # Exposure
    exposure_ev: Optional[float] = None,
    black_level: Optional[float] = None,
    highlight_recovery: Optional[float] = None,
    shadow_lift: Optional[float] = None,
    # White balance
    temperature_kelvin: Optional[float] = None,
    tint: Optional[float] = None,
    # Tone
    contrast: Optional[float] = None,
    brightness: Optional[float] = None,
    highlights: Optional[float] = None,
    shadows: Optional[float] = None,
    whites: Optional[float] = None,
    blacks: Optional[float] = None,
    sigmoid_contrast: Optional[float] = None,
    sigmoid_skew: Optional[float] = None,
    # Colour
    saturation: Optional[float] = None,
    vibrance: Optional[float] = None,
    # Detail
    sharpness: Optional[float] = None,
    noise_reduction: Optional[float] = None,
    # Effects
    vignette: Optional[float] = None,
    clarity: Optional[float] = None,
    dehaze: Optional[float] = None,
) -> dict:
    """Apply one or more non-destructive adjustments to an image.

    IMPORTANT:
    Claude does NOT need direct access to the file.
    Pass the absolute Windows path to the MCP server.
    The MCP server reads the file and returns the preview.
        
    Only the parameters you supply are changed; the rest keep their current values.
    Call get_image_preview afterwards to see the result.

    Parameters
    ----------
    exposure_ev : float
        Exposure in stops (-5 to +5).  0 = no change.
    black_level : float
        Lift or crush the black point (0.0–0.5).
    highlight_recovery : float
        Recover blown highlights (0.0–1.0).
    shadow_lift : float
        Open up dark shadows (0.0–1.0).
    temperature_kelvin : float
        Colour temperature (2000–12000 K).  ~5500 K is daylight, ~3200 K is tungsten.
    tint : float
        Green (positive) / magenta (negative) tint correction (-100 to +100).
    contrast : float
        Global contrast (-100 to +100).
    brightness : float
        Global brightness (-100 to +100).
    highlights : float
        Recover or boost highlights (-100 to +100).
    shadows : float
        Open shadows (-100 to +100).
    whites : float
        White-point adjustment (-100 to +100).
    blacks : float
        Black-point adjustment (-100 to +100).
    sigmoid_contrast : float
        Sigmoid-style tone curve contrast (-100 to +100).
    sigmoid_skew : float
        Sigmoid-style tone curve midpoint skew (-100 to +100).
    saturation : float
        Global colour saturation (-100 to +100).  0 = original.
    vibrance : float
        Selective saturation boost for muted colours (-100 to +100).
    sharpness : float
        Sharpening strength (0 to 100).
    noise_reduction : float
        Noise reduction strength (0 to 100).
    vignette : float
        Vignette: negative values darken edges, positive lighten (-100 to +100).
    clarity : float
        Local contrast / clarity (-100 to +100).
    dehaze : float
        Haze reduction and mid-detail contrast (0 to 100).
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    params = {k: v for k, v in {
        "exposure_ev": exposure_ev,
        "black_level": black_level,
        "highlight_recovery": highlight_recovery,
        "shadow_lift": shadow_lift,
        "temperature_kelvin": temperature_kelvin,
        "tint": tint,
        "contrast": contrast,
        "brightness": brightness,
        "highlights": highlights,
        "shadows": shadows,
        "whites": whites,
        "blacks": blacks,
        "sigmoid_contrast": sigmoid_contrast,
        "sigmoid_skew": sigmoid_skew,
        "saturation": saturation,
        "vibrance": vibrance,
        "sharpness": sharpness,
        "noise_reduction": noise_reduction,
        "vignette": vignette,
        "clarity": clarity,
        "dehaze": dehaze,
    }.items() if v is not None}

    state.update(params)
    state.save()

    return {"status": "ok", "applied": list(params.keys()), "edits": state.to_dict()}


@mcp.tool()
def crop_image(
    image_path: str,
    left: Optional[float] = None,
    top: Optional[float] = None,
    right: Optional[float] = None,
    bottom: Optional[float] = None,
    aspect_ratio: Optional[str] = None,
) -> dict:
    """Crop an image.

    Provide either:
    - *left*, *top*, *right*, *bottom* as normalised coordinates (0.0–1.0), or
    - *aspect_ratio* as a string like "16:9", "4:3", "1:1", "3:2" for a centre crop.

    Both methods can be combined (apply aspect ratio first, then fine-tune coordinates).
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    crop = state.crop or CropState()

    if aspect_ratio:
        try:
            w_ratio, h_ratio = (float(x) for x in aspect_ratio.split(":"))
        except ValueError:
            return {"error": f"Invalid aspect_ratio format '{aspect_ratio}'. Use e.g. '16:9'."}

        # Current crop area dimensions (in normalised space)
        cw = crop.right - crop.left
        ch = crop.bottom - crop.top
        centre_x = crop.left + cw / 2
        centre_y = crop.top + ch / 2

        target_ar = w_ratio / h_ratio
        current_ar = cw / ch if ch > 0 else 1.0

        if current_ar > target_ar:
            new_w = ch * target_ar
            new_h = ch
        else:
            new_w = cw
            new_h = cw / target_ar

        crop.left = max(0.0, centre_x - new_w / 2)
        crop.top = max(0.0, centre_y - new_h / 2)
        crop.right = min(1.0, centre_x + new_w / 2)
        crop.bottom = min(1.0, centre_y + new_h / 2)

    if left is not None:
        crop.left = max(0.0, float(left))
    if top is not None:
        crop.top = max(0.0, float(top))
    if right is not None:
        crop.right = min(1.0, float(right))
    if bottom is not None:
        crop.bottom = min(1.0, float(bottom))

    if crop.right <= crop.left or crop.bottom <= crop.top:
        return {"error": "Invalid crop: right must be > left and bottom must be > top."}

    state.crop = crop
    state.save()

    return {"status": "ok", "crop": {
        "left": crop.left, "top": crop.top,
        "right": crop.right, "bottom": crop.bottom,
        "rotation": crop.rotation,
    }}


@mcp.tool()
def rotate_image(image_path: str, degrees: float) -> dict:
    """Rotate / straighten the image by *degrees* (clockwise).

    Positive = clockwise, negative = counter-clockwise.
    Typical use: small corrections like +0.5 or -1.2 to straighten horizons.
    Large rotations (90, 180, 270) are also supported.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    crop = state.crop or CropState()
    crop.rotation = degrees
    state.crop = crop
    state.save()

    return {"status": "ok", "rotation_degrees": degrees}


@mcp.tool()
def reset_crop(image_path: str) -> dict:
    """Remove all crop and rotation from the image, restoring the full frame."""
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    state.crop = None
    state.save()
    return {"status": "ok", "message": "Crop reset to full frame."}


@mcp.tool()
def add_gradient_mask(
    image_path: str,
    name: str,
    start_x: float = 0.5,
    start_y: float = 0.0,
    end_x: float = 0.5,
    end_y: float = 1.0,
    invert: bool = False,
    opacity: float = 1.0,
    exposure_ev: float = 0.0,
    brightness: float = 0.0,
    contrast: float = 0.0,
    highlights: float = 0.0,
    shadows: float = 0.0,
    whites: float = 0.0,
    blacks: float = 0.0,
    sigmoid_contrast: float = 0.0,
    sigmoid_skew: float = 0.0,
    saturation: float = 0.0,
    vibrance: float = 0.0,
    clarity: float = 0.0,
    dehaze: float = 0.0,
) -> dict:
    """Add or replace a reusable linear-gradient local adjustment mask.

    Coordinates are normalized image positions from 0.0 to 1.0. The adjustment
    is strongest at the end point, fades toward the start point, and can be
    inverted for sky-style top-down masks.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    if not name.strip():
        return {"error": "Mask name cannot be empty."}
    if not 0.0 <= opacity <= 1.0:
        return {"error": "opacity must be between 0.0 and 1.0."}

    local = LocalAdjustmentState(
        name=name.strip(),
        start_x=float(start_x),
        start_y=float(start_y),
        end_x=float(end_x),
        end_y=float(end_y),
        invert=bool(invert),
        opacity=float(opacity),
        exposure_ev=float(exposure_ev),
        brightness=float(brightness),
        contrast=float(contrast),
        highlights=float(highlights),
        shadows=float(shadows),
        whites=float(whites),
        blacks=float(blacks),
        sigmoid_contrast=float(sigmoid_contrast),
        sigmoid_skew=float(sigmoid_skew),
        saturation=float(saturation),
        vibrance=float(vibrance),
        clarity=float(clarity),
        dehaze=float(dehaze),
    )
    state.local_adjustments = [
        item for item in state.local_adjustments
        if item.name.lower() != local.name.lower()
    ]
    state.local_adjustments.append(local)
    state.save()
    return {"status": "ok", "mask": local.__dict__, "edits": state.to_dict()}


@mcp.tool()
def reset_masks(image_path: str) -> dict:
    """Remove all MCP-side local adjustment masks from an image."""
    try:
        _path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    removed = len(state.local_adjustments)
    state.local_adjustments = []
    state.save()
    return {"status": "ok", "removed": removed}


@mcp.tool()
def rename_output(image_path: str, new_name: str) -> dict:
    """Set the output filename stem (without extension) for the exported image.

    For example: rename_output("/photos/DSC001.NEF", "golden_hour_lake")
    will export as golden_hour_lake.jpg (or whichever format you choose in export_image).
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    # Strip any extension the user may have included
    stem = Path(new_name).stem or new_name
    state.output_name = stem
    state.save()
    return {"status": "ok", "output_name": stem}


@mcp.tool()
def reset_edits(image_path: str) -> dict:
    """Reset all adjustments, crop, and output name — back to the unedited original."""
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    sidecar = path.with_name(path.stem + ".mcp.json")
    if sidecar.exists():
        sidecar.unlink()

    return {"status": "ok", "message": "All edits reset to original."}


@mcp.tool()
def edit_vacation_photo(
    image_path: str,
    output_directory: Optional[str] = None,
    quality: int = 92,
    max_dimension: Optional[int] = None,
) -> dict:
    """Auto-tune the vacation-photo profile and export a 16:9 JPEG.

    The tool starts with a restrained summer-light profile, renders preview
    passes, measures luminance, adjusts exposure/shadows/highlights, and then
    exports the final image with a centered 16:9 crop.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    state.update(VACATION_PROFILE)
    state.crop = CropState(left=0.0, top=0.125, right=1.0, bottom=0.875)
    state.local_adjustments = [
        LocalAdjustmentState(
            name="sky_protection",
            start_x=0.5,
            start_y=0.02,
            end_x=0.5,
            end_y=0.52,
            invert=True,
            opacity=0.55,
            exposure_ev=-0.25,
            highlights=28.0,
            sigmoid_contrast=12.0,
            saturation=4.0,
            clarity=8.0,
            dehaze=6.0,
        ),
        LocalAdjustmentState(
            name="mountain_dehaze",
            start_x=0.5,
            start_y=0.18,
            end_x=0.5,
            end_y=0.72,
            opacity=0.38,
            contrast=8.0,
            sigmoid_contrast=12.0,
            clarity=18.0,
            dehaze=32.0,
        ),
        LocalAdjustmentState(
            name="foreground_lift",
            start_x=0.5,
            start_y=0.45,
            end_x=0.5,
            end_y=1.0,
            opacity=0.45,
            exposure_ev=0.18,
            shadows=18.0,
            contrast=4.0,
            clarity=10.0,
            dehaze=8.0,
        ),
    ]
    if not state.output_name:
        state.output_name = f"{path.stem}_vacation"
    state.save()

    feedback_history = _auto_tune_vacation_photo(path, state)
    state.save()

    result = export_image(
        image_path=str(path),
        output_directory=output_directory,
        format="jpeg",
        quality=quality,
        use_darktable_cli=True,
        write_xmp_sidecar=True,
        max_dimension=max_dimension,
    )
    result["profile"] = "vacation"
    result["feedback_history"] = feedback_history
    result["applied"] = list(VACATION_PROFILE.keys()) + ["adaptive_feedback", "crop_16_9"]
    result["edits"] = state.to_dict()
    return result


@mcp.tool()
def export_image(
    image_path: str,
    output_directory: Optional[str] = None,
    format: str = "jpeg",
    quality: int = 92,
    use_darktable_cli: bool = True,
    write_xmp_sidecar: bool = True,
    max_dimension: Optional[int] = None,
) -> dict:
    """Export the edited image to a file.

    IMPORTANT:
    Claude does NOT need direct access to the file.
    Pass the absolute Windows path to the MCP server.
    The MCP server reads the file and returns the preview.

    Parameters
    ----------
    image_path : str
        Source image path.
    output_directory : str, optional
        Destination folder.  Defaults to the same folder as the source.
    format : str
        Output format: "jpeg", "png", "tiff" (default "jpeg").
    quality : int
        JPEG quality 1–100 (default 92).  Ignored for PNG/TIFF.
    use_darktable_cli : bool
        Try to use darktable-cli for export when available (better RAW rendering).
    write_xmp_sidecar : bool
        Write a Darktable-compatible XMP sidecar alongside the source file.
    max_dimension : int, optional
        Resize so the longest edge is at most this many pixels.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    out_dir = Path(output_directory) if output_directory else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = state.output_name or path.stem
    ext_map = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "tiff": ".tif", "tif": ".tif"}
    ext = ext_map.get(format.lower(), ".jpg")
    out_path = out_dir / (stem + ext)

    # Avoid collision
    counter = 1
    while out_path.exists():
        out_path = out_dir / f"{stem}_{counter}{ext}"
        counter += 1

    # Optionally write Darktable XMP sidecar
    xmp_path = None
    if write_xmp_sidecar:
        try:
            xmp_path = write_xmp(state)
        except Exception as exc:
            xmp_path = None  # non-fatal

        # RAW files: use Darktable exclusively.
    if path.suffix.lower() in RAW_EXTENSIONS:
        if not use_darktable_cli:
            return {
                "status": "error",
                "error": (
                    "RAW export requires darktable-cli for this workflow. "
                    "The rawpy fallback is disabled to keep preview and "
                    "final rendering consistent."
                ),
            }

        if not DARKTABLE:
            return {
                "status": "error",
                "error": (
                    "darktable-cli was not found. "
                    "RAW export cannot continue without Darktable."
                ),
        }

        darktable_out = out_path
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-render-") as tmp:
            finishing_needed = _needs_mcp_finishing(state)
            if finishing_needed:
                darktable_out = Path(tmp) / out_path.name

            result = _export_via_darktable_cli(
                src=path,
                dst=darktable_out,
                xmp=xmp_path,
                quality=quality,
                max_dimension=max_dimension,
            )

            if result.get("status") == "ok" and darktable_out != out_path:
                _finish_darktable_render(
                    darktable_out,
                    state,
                    out_path,
                    format_name=format,
                    quality=quality,
                    max_dimension=max_dimension,
                )

        if result.get("status") == "ok":
            result.update({
                "output_path": str(out_path),
                "size_bytes": out_path.stat().st_size,
                "xmp_sidecar": str(xmp_path) if xmp_path else None,
                "mcp_finishing_applied": finishing_needed,
            })
            return result

        return {
            "status": "error",
            "error": "Darktable RAW export failed.",
            "details": result,
            "xmp_sidecar": str(xmp_path) if xmp_path else None,
        }

    # Non-RAW files continue with the Pillow path.
    proc = ImageProcessor(path)
    img = proc.process(state)

    if max_dimension:
        img.thumbnail((max_dimension, max_dimension), PILImage.LANCZOS)

    save_kwargs: dict = {}
    if format.lower() in ("jpeg", "jpg"):
        save_kwargs = {
            "format": "JPEG",
            "quality": quality,
            "optimize": True,
        }
    elif format.lower() == "png":
        save_kwargs = {
            "format": "PNG",
            "optimize": True,
        }
    elif format.lower() in ("tiff", "tif"):
        save_kwargs = {
            "format": "TIFF"
        }

    img.save(out_path, **save_kwargs)

    return {
        "status": "ok",
        "output_path": str(out_path),
        "format": format,
        "size_bytes": out_path.stat().st_size,
        "xmp_sidecar": str(xmp_path) if xmp_path else None,
        "rendered_via": "pillow",
    }


def _export_via_darktable_cli(
    src: Path,
    dst: Path,
    xmp: Optional[Path],
    quality: int,
    max_dimension: Optional[int] = None,
) -> dict:
    """
    Render/export an image using darktable-cli.

    Windows paths are converted to forward-slash form before being passed
    to darktable-cli. This avoids backslash escaping/path parsing problems.
    """

    if not DARKTABLE:
        return {
            "status": "error",
            "error": "darktable-cli is not available",
        }

    return DARKTABLE.export(src, dst, xmp, quality=quality, max_dimension=max_dimension)


@mcp.tool()
def get_histogram(image_path: str) -> dict:
    """Compute a brightness/channel histogram for the image (with current edits).

    IMPORTANT:
    Claude does NOT need direct access to the file.
    Pass the absolute Windows path to the MCP server.
    The MCP server reads the file and returns the preview.
        
    Returns per-channel (R, G, B) and luminance histograms as 256-bin arrays.
    Useful for analysing exposure, clipping, and tonal distribution.
    """
    import numpy as np

    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    try:
        _img, img_bytes = _quick_preview(path, state, 800)
        with PILImage.open(io.BytesIO(img_bytes)) as preview_img:
            img = preview_img.convert("RGB")
            arr = np.array(img, dtype=np.uint8)
    except Exception as e:
        return {"error": f"Could not render histogram source image: {e}"}

    def _hist(channel: np.ndarray) -> list[int]:
        counts, _ = np.histogram(channel.ravel(), bins=256, range=(0, 256))
        return counts.tolist()

    r_hist = _hist(arr[..., 0])
    g_hist = _hist(arr[..., 1])
    b_hist = _hist(arr[..., 2])
    lum = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]).astype(np.uint8)
    lum_hist = _hist(lum)

    clipped_highlights = int((arr == 255).all(axis=2).sum())
    clipped_shadows = int((arr == 0).all(axis=2).sum())
    total_pixels = arr.shape[0] * arr.shape[1]

    return {
        "r": r_hist,
        "g": g_hist,
        "b": b_hist,
        "luminance": lum_hist,
        "clipped_highlights_px": clipped_highlights,
        "clipped_shadows_px": clipped_shadows,
        "total_pixels": total_pixels,
        "highlight_clip_pct": round(clipped_highlights / total_pixels * 100, 2),
        "shadow_clip_pct": round(clipped_shadows / total_pixels * 100, 2),
    }


@mcp.tool()
def copy_settings(source_image_path: str, target_image_path: str) -> dict:
    """Copy all edit settings (adjustments + crop) from one image to another.

    Useful for batch-editing a set of photos shot under the same conditions.
    The output_name is NOT copied — each image keeps its own name.
    """
    try:
        src_path, src_state = _load_or_new(source_image_path)
        tgt_path, tgt_state = _load_or_new(target_image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    from dataclasses import replace, asdict
    tgt_state.adjustments = replace(src_state.adjustments)
    tgt_state.crop = replace(src_state.crop) if src_state.crop else None
    tgt_state.save()

    return {"status": "ok", "copied_to": str(tgt_path), "edits": tgt_state.to_dict()}
