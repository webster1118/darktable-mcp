"""Darktable MCP Server — photo editing tools for Claude."""
from __future__ import annotations

import io
import tempfile
from dataclasses import fields, replace
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer, Image
from PIL import Image as PILImage

from .darktable_cli import DarktableCli
from .dng_converter import is_probably_apple_proraw
from .edits import CropState, EditState, LocalAdjustmentState
from .processor import ALL_EXTENSIONS, RAW_EXTENSIONS, ImageProcessor
from .xmp_writer import write_xmp

mcp = MCPServer("DarktableMCP")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


DARKTABLE = DarktableCli.discover()


STARTING_POINTS: dict[str, dict] = {
    "apple_proraw_natural": {
        "description": (
            "Neutral Apple ProRAW starting point for Darktable's initially flat/dark "
            "conversion. Intended as a first-pass normalization, not a final look."
        ),
        "adjustments": {
            "exposure_ev": 0.6,
            "brightness": 10,
            "highlights": 8,
            "shadows": 28,
            "whites": 6,
            "blacks": -5,
            "sigmoid_contrast": 10,
            "sigmoid_skew": -5,
            "vibrance": 12,
            "saturation": 3,
            "clarity": 8,
            "dehaze": 6,
            "sharpness": 70,
            "sharpening_masking": 70,
            "noise_reduction": 2,
        },
    }
}


def _load_or_new(image_path: str) -> tuple[Path, EditState]:
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    state = EditState.load(path) or EditState(source_path=path)
    return path, state


def _looks_like_apple_proraw(path: Path) -> bool:
    return path.suffix.lower() == ".dng" and is_probably_apple_proraw(path)


def _recommended_starting_point(path: Path, state: EditState) -> Optional[dict]:
    if state.has_changes():
        return None
    if _looks_like_apple_proraw(path):
        profile = STARTING_POINTS["apple_proraw_natural"]
        return {
            "profile": "apple_proraw_natural",
            "reason": "Apple ProRAW DNGs often render dark/flat as a neutral Darktable starting point.",
            "adjustments": profile["adjustments"],
        }
    return None


def _apply_starting_point_to_state(state: EditState, profile_name: str) -> list[str]:
    profile = STARTING_POINTS.get(profile_name)
    if not profile:
        raise ValueError(f"Unknown starting point: {profile_name}")
    adjustments = profile["adjustments"]
    state.update(adjustments)
    return list(adjustments.keys())


def _cleanup_generated_files(path: Path, *, include_converted: bool = False) -> dict:
    """Delete MCP-generated temporary artifacts for one source image."""
    removed: list[str] = []
    errors: list[dict] = []

    candidates: list[Path] = []
    preview_dir = path.parent / ".darktable-mcp-preview"
    if preview_dir.is_dir():
        candidates.extend(
            item for item in preview_dir.iterdir()
            if item.is_file() and item.name.startswith(f"{path.stem}__")
        )

    preview_copy = path.with_name(path.stem + "__preview.jpg")
    if preview_copy.exists() and preview_copy.is_file():
        candidates.append(preview_copy)

    if include_converted:
        converted_dir = path.parent / ".darktable-mcp-converted"
        if converted_dir.is_dir():
            candidates.extend(
                item for item in converted_dir.iterdir()
                if item.is_file() and item.stem == path.stem
            )

    for candidate in candidates:
        try:
            candidate.unlink()
            removed.append(str(candidate))
        except OSError as exc:
            errors.append({"path": str(candidate), "error": str(exc)})

    return {"removed": removed, "errors": errors}


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

        # Use a predictable local cache directory instead of %TEMP%.
        preview_dir = path.parent / ".darktable-mcp-preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = preview_dir / f"{path.stem}__preview.jpg"

        result = _export_state_to_path(
            path=path,
            state=state,
            out_path=preview_path,
            format_name="jpeg",
            quality=85,
            max_dimension=max_size,
            overwrite=True,
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
        sharpening_masking=0.0,
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


def _unlink_if_exists(path: Path) -> None:
    """Remove a known render target before Darktable writes to it."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _finish_darktable_render(
    rendered_path: Path,
    state: EditState,
    output_path: Path,
    *,
    format_name: str = "jpeg",
    quality: int = 100,
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


def _export_state_to_path(
    *,
    path: Path,
    state: EditState,
    out_path: Path,
    format_name: str = "jpeg",
    quality: int = 100,
    use_darktable_cli: bool = True,
    write_xmp_sidecar: bool = True,
    max_dimension: Optional[int] = None,
    overwrite: bool = False,
    allow_dng_conversion: bool = True,
    config_directory: Optional[Path] = None,
) -> dict:
    """Render the current edit state to an exact destination path.

    This is the single source of truth for preview, histogram, analysis, and
    final export rendering.  Internal previews pass ``overwrite=True`` so stale
    fixed-name previews cannot be mistaken for a fresh render.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite:
        _unlink_if_exists(out_path)

    xmp_path = None
    xmp_error = None
    if write_xmp_sidecar:
        try:
            xmp_path = write_xmp(state)
        except Exception as exc:
            xmp_error = str(exc)

    if path.suffix.lower() in RAW_EXTENSIONS:
        if write_xmp_sidecar and xmp_path is None:
            return {
                "status": "error",
                "error": (
                    "Could not write the Darktable XMP sidecar for this RAW render. "
                    "Refusing to call darktable-cli without an explicit XMP because "
                    "that can render stale or unedited history."
                ),
                "xmp_error": xmp_error,
            }

        if not use_darktable_cli:
            return {
                "status": "error",
                "error": (
                    "RAW export requires darktable-cli for this workflow. "
                    "The rawpy fallback is disabled to keep preview and "
                    "final rendering consistent."
                ),
                "xmp_sidecar": str(xmp_path) if xmp_path else None,
            }

        if not DARKTABLE:
            return {
                "status": "error",
                "error": (
                    "darktable-cli was not found. "
                    "RAW export cannot continue without Darktable."
                ),
                "xmp_sidecar": str(xmp_path) if xmp_path else None,
            }

        finishing_needed = _needs_mcp_finishing(state)
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-render-") as tmp:
            darktable_out = Path(tmp) / out_path.name if finishing_needed else out_path
            if overwrite:
                _unlink_if_exists(darktable_out)

            result = _export_via_darktable_cli(
                src=path,
                dst=darktable_out,
                xmp=xmp_path,
                quality=quality,
                max_dimension=max_dimension,
                allow_dng_conversion=allow_dng_conversion,
                config_directory=config_directory,
            )

            if result.get("status") == "ok" and darktable_out != out_path:
                _finish_darktable_render(
                    darktable_out,
                    state,
                    out_path,
                    format_name=format_name,
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

    proc = ImageProcessor(path)
    img = proc.process(state)
    if max_dimension:
        img.thumbnail((max_dimension, max_dimension), PILImage.LANCZOS)
    _save_image(img, out_path, format_name, quality)

    return {
        "status": "ok",
        "output_path": str(out_path),
        "format": format_name,
        "size_bytes": out_path.stat().st_size,
        "xmp_sidecar": str(xmp_path) if xmp_path else None,
        "rendered_via": "pillow",
        "mcp_finishing_applied": False,
    }


def _image_metrics(image_path: Path) -> dict:
    import numpy as np

    def _summarize_region(region_arr: np.ndarray) -> dict:
        region_lum = 0.299 * region_arr[..., 0] + 0.587 * region_arr[..., 1] + 0.114 * region_arr[..., 2]
        region_mx = region_arr.max(axis=2)
        region_mn = region_arr.min(axis=2)
        region_sat = (region_mx - region_mn) / np.maximum(region_mx, 1.0)
        region_rg = region_arr[..., 0] - region_arr[..., 1]
        region_yb = 0.5 * (region_arr[..., 0] + region_arr[..., 1]) - region_arr[..., 2]
        region_colorfulness = (
            (region_rg.std() ** 2 + region_yb.std() ** 2) ** 0.5
            + 0.3 * (region_rg.mean() ** 2 + region_yb.mean() ** 2) ** 0.5
        )
        region_grad_y, region_grad_x = np.gradient(region_lum)
        region_gradient = np.sqrt(region_grad_x * region_grad_x + region_grad_y * region_grad_y)
        region_laplacian = np.gradient(region_grad_x)[1] + np.gradient(region_grad_y)[0]
        return {
            "mean": round(float(region_lum.mean()), 2),
            "p50": round(float(np.percentile(region_lum, 50)), 2),
            "p95": round(float(np.percentile(region_lum, 95)), 2),
            "saturation_mean": round(float(region_sat.mean()), 3),
            "colorfulness": round(float(region_colorfulness), 3),
            "red_mean": round(float(region_arr[..., 0].mean()), 2),
            "green_mean": round(float(region_arr[..., 1].mean()), 2),
            "blue_mean": round(float(region_arr[..., 2].mean()), 2),
            "contrast_span": round(float(np.percentile(region_lum, 95) - np.percentile(region_lum, 5)), 2),
            "detail_gradient_mean": round(float(region_gradient.mean()), 3),
            "detail_laplacian_var": round(float(region_laplacian.var()), 3),
        }

    with PILImage.open(image_path) as img:
        width, height = img.size
        rgb = img.convert("RGB")
        metric_img = rgb.copy()
        metric_img.thumbnail((1600, 1600), PILImage.LANCZOS)
        arr = np.array(metric_img, dtype=np.float32)
    metric_height, metric_width = arr.shape[:2]
    lum = 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
    mx = arr.max(axis=2)
    mn = arr.min(axis=2)
    saturation = (mx - mn) / np.maximum(mx, 1.0)
    rg = arr[..., 0] - arr[..., 1]
    yb = 0.5 * (arr[..., 0] + arr[..., 1]) - arr[..., 2]
    colorfulness = (
        (rg.std() ** 2 + yb.std() ** 2) ** 0.5
        + 0.3 * (rg.mean() ** 2 + yb.mean() ** 2) ** 0.5
    )
    grad_y, grad_x = np.gradient(lum)
    gradient = np.sqrt(grad_x * grad_x + grad_y * grad_y)
    laplacian = np.gradient(grad_x)[1] + np.gradient(grad_y)[0]
    regions = {
        "sky_top": arr[: max(2, int(metric_height * 0.30)), :],
        "mountains_mid": arr[int(metric_height * 0.18): max(int(metric_height * 0.62), int(metric_height * 0.18) + 2), :],
        "lake_lower_mid": arr[int(metric_height * 0.58): max(int(metric_height * 0.78), int(metric_height * 0.58) + 2), :],
        "foreground_bottom": arr[int(metric_height * 0.72):, :],
        "center_subject": arr[
            int(metric_height * 0.20): max(int(metric_height * 0.68), int(metric_height * 0.20) + 2),
            int(metric_width * 0.15): max(int(metric_width * 0.85), int(metric_width * 0.15) + 2),
        ],
    }
    return {
        "width": width,
        "height": height,
        "aspect_ratio": round(float(width / height), 4) if height else None,
        "mean": round(float(lum.mean()), 2),
        "p05": round(float(np.percentile(lum, 5)), 2),
        "p50": round(float(np.percentile(lum, 50)), 2),
        "p95": round(float(np.percentile(lum, 95)), 2),
        "saturation_mean": round(float(saturation.mean()), 3),
        "colorfulness": round(float(colorfulness), 3),
        "red_mean": round(float(arr[..., 0].mean()), 2),
        "green_mean": round(float(arr[..., 1].mean()), 2),
        "blue_mean": round(float(arr[..., 2].mean()), 2),
        "shadow_clip_pct": round(float((lum <= 5).mean() * 100), 3),
        "highlight_clip_pct": round(float((lum >= 250).mean() * 100), 3),
        "contrast_span": round(float(np.percentile(lum, 95) - np.percentile(lum, 5)), 2),
        "detail_gradient_mean": round(float(gradient.mean()), 3),
        "detail_gradient_p95": round(float(np.percentile(gradient, 95)), 3),
        "detail_laplacian_var": round(float(laplacian.var()), 3),
        "regions": {
            name: _summarize_region(region_arr)
            for name, region_arr in regions.items()
            if region_arr.size
        },
    }


def _luminance_metrics(image_path: Path) -> dict:
    return _image_metrics(image_path)


def _metric_delta(current: dict, baseline: dict) -> dict:
    keys = [
        "mean",
        "p05",
        "p50",
        "p95",
        "saturation_mean",
        "colorfulness",
        "red_mean",
        "green_mean",
        "blue_mean",
        "contrast_span",
        "detail_gradient_mean",
        "detail_gradient_p95",
        "detail_laplacian_var",
    ]
    return {
        key: round(float(current[key] - baseline[key]), 3)
        for key in keys
        if key in current and key in baseline
    }


def _regional_metric_delta(current: dict, baseline: dict) -> dict:
    current_regions = current.get("regions") or {}
    baseline_regions = baseline.get("regions") or {}
    return {
        region_name: _metric_delta(region_metrics, baseline_regions[region_name])
        for region_name, region_metrics in current_regions.items()
        if region_name in baseline_regions
    }


def _reference_match_suggestions(delta: dict, regional_delta: dict) -> list[str]:
    suggestions: list[str] = []
    if delta.get("saturation_mean", 0.0) < -0.02 or delta.get("colorfulness", 0.0) < -4.0:
        suggestions.append(
            "Overall color is less lively than the reference. Prefer vibrance/color balance first; "
            "use saturation carefully and check sky/water for artificial color."
        )
    if delta.get("p50", 0.0) < -4.0 and delta.get("mean", 0.0) >= -2.0:
        suggestions.append(
            "Average brightness is close but midtones are low. Lift brightness/shadows or adjust sigmoid skew "
            "instead of pushing exposure/highlights."
        )
    if delta.get("p05", 0.0) > 4.0:
        suggestions.append(
            "Shadow floor is higher than the reference. Deepen blacks or reduce shadow lift to restore depth."
        )
    if delta.get("detail_gradient_mean", 0.0) < -0.8:
        suggestions.append(
            "Measured fine detail is lower than the reference. Increase sharpening/local clarity/dehaze, "
            "or reduce denoise if the image looks smeared."
        )

    region_actions = {
        "sky_top": {
            "dark": "Sky is darker than the reference; brighten it locally only if the visual reference supports it.",
            "flat": "Sky is flatter/less colorful than the reference; adjust sky vibrance/blue depth locally.",
            "detail": "Sky/upper haze lacks edge separation; use very gentle local dehaze/contrast and avoid halos.",
        },
        "mountains_mid": {
            "dark": "Mountains are darker than the reference; use a mountain/center local lift rather than global exposure.",
            "flat": "Mountains lack punch; add local contrast/clarity/dehaze on rock and green slopes.",
            "detail": "Mountain detail is low; add local clarity/dehaze/sharpening on the mountain region.",
        },
        "lake_lower_mid": {
            "dark": "Lake is darker than the reference; brighten lake midtones locally without lifting the whole frame.",
            "flat": "Lake color is less lively than the reference; increase lake vibrance/saturation locally but avoid neon cyan.",
            "detail": "Lake region detail/edge separation differs; keep water smooth and prioritize natural color over texture.",
        },
        "foreground_bottom": {
            "dark": "Foreground is darker than the reference; lift shadows locally while preserving believable tree depth.",
            "flat": "Foreground color/depth differs; tune local contrast and greens without making trees gray.",
            "detail": "Foreground detail is low; use local clarity/sharpening and keep denoise minimal.",
        },
        "center_subject": {
            "dark": "Center subject is darker than the reference; lift midtones locally or adjust sigmoid/brightness.",
            "flat": "Center subject is flatter or less colorful; use targeted contrast and vibrance rather than global shifts.",
            "detail": "Center subject detail is low; add local clarity/dehaze on the main subject area.",
        },
    }
    for region_name, region_delta in regional_delta.items():
        actions = region_actions.get(region_name)
        if not actions:
            continue
        if region_delta.get("mean", 0.0) < -6.0 or region_delta.get("p50", 0.0) < -8.0:
            suggestions.append(actions["dark"])
        if region_delta.get("saturation_mean", 0.0) < -0.03 or region_delta.get("colorfulness", 0.0) < -5.0:
            suggestions.append(actions["flat"])
        if region_delta.get("detail_gradient_mean", 0.0) < -1.5:
            suggestions.append(actions["detail"])

    return suggestions


def _diagnostic_warnings(
    current: dict,
    baseline: Optional[dict] = None,
    state: Optional[EditState] = None,
    *,
    fast: bool = False,
) -> list[str]:
    warnings: list[str] = []
    if fast:
        warnings.append(
            "Fast render mode disables sharpening and noise reduction. Do a non-fast render "
            "before judging final sharpness or comparing detail to a reference."
        )
    if baseline:
        delta = _metric_delta(current, baseline)
        regional_delta = _regional_metric_delta(current, baseline)
        if delta.get("mean", 0.0) < -5.0:
            warnings.append(
                "Current render is darker than the unedited Darktable render; "
                "increase exposure/brightness/shadows unless that was intentional."
            )
        if delta.get("p95", 0.0) < -10.0:
            warnings.append(
                "Bright tones are lower than the unedited render; sunny daylight edits "
                "usually need a higher p95/whites value while avoiding harsh clipping."
            )
        if delta.get("contrast_span", 0.0) < -10.0:
            warnings.append(
                "Current render is flatter than the unedited render; add contrast, "
                "sigmoid contrast, clarity, or local contrast if a crisp result is wanted."
            )
        if delta.get("detail_gradient_mean", 0.0) < -1.0:
            warnings.append(
                "Current render has less measured edge/detail contrast than the comparison render; "
                "increase sharpening, clarity/local contrast, or dehaze, and reduce denoise if over-smoothed."
            )
        for region_name, region_delta in regional_delta.items():
            label = region_name.replace("_", " ")
            if region_delta.get("mean", 0.0) < -6.0:
                warnings.append(
                    f"{label} region is darker than the comparison render; use a targeted local adjustment "
                    "rather than changing the whole image if only this area is wrong."
                )
            if region_delta.get("saturation_mean", 0.0) < -0.03:
                warnings.append(
                    f"{label} region is less saturated than the comparison render; adjust regional color/vibrance "
                    "if the visual reference supports it."
                )
            if region_delta.get("colorfulness", 0.0) < -5.0:
                warnings.append(
                    f"{label} region is less colorful than the comparison render; inspect regional hue/chroma, "
                    "not just global saturation."
                )
            if region_delta.get("detail_gradient_mean", 0.0) < -1.5:
                warnings.append(
                    f"{label} region has less measured detail than the comparison render; use local clarity, "
                    "dehaze, or sharpening instead of only global tone changes."
                )
    if state:
        if state.crop is None:
            warnings.append(
                "No crop is currently set. If the intended output is 16:9, call crop_image "
                "or apply_edit_recipe with crop top/bottom before export."
            )
        if not state.local_adjustments:
            warnings.append(
                "No local adjustments are currently set. If sky, mountains, water, or foreground "
                "need separate treatment, add local masks/adjustments before export."
            )
    return warnings


def _render_preview_to_file(
    path: Path,
    state: EditState,
    output_path: Path,
    max_dimension: int = 900,
    render_source: Optional[Path] = None,
    config_directory: Optional[Path] = None,
    fast: bool = False,
) -> dict:
    preview_state = replace(state)
    if fast:
        preview_state.adjustments = replace(
            state.adjustments,
            sharpness=0.0,
            noise_reduction=0.0,
        )

    return _export_state_to_path(
        path=path,
        state=preview_state,
        out_path=output_path,
        format_name="jpeg",
        quality=85,
        max_dimension=max_dimension,
        overwrite=True,
        allow_dng_conversion=render_source is None,
        config_directory=config_directory,
    )


def _render_current_edits_to_file(
    path: Path,
    state: EditState,
    output_path: Path,
    max_dimension: int,
    fast: bool = False,
) -> tuple[dict, bytes]:
    result = _render_preview_to_file(path, state, output_path, max_dimension=max_dimension, fast=fast)
    if result.get("status") != "ok":
        return result, b""
    return result, output_path.read_bytes()


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
    info["recommended_starting_point"] = _recommended_starting_point(path, state)
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
        "workflow": "bridge only; the client/Claude should decide iterative edits",
        "starting_points": {
            name: {
                "description": profile["description"],
                "adjustments": profile["adjustments"],
            }
            for name, profile in STARTING_POINTS.items()
        },
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
            "sharpening_masking",
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
def get_current_edits(image_path: str) -> dict:
    """Return the current non-destructive MCP edit sidecar for an image."""
    try:
        _path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"status": "error", "error": str(e)}
    return {
        "status": "ok",
        "edits": state.to_dict(),
        "mcp_finishing_needed": _needs_mcp_finishing(state),
        "recommended_starting_point": _recommended_starting_point(_path, state),
    }


@mcp.tool()
def apply_starting_point(
    image_path: str,
    profile: str = "apple_proraw_natural",
    only_if_unedited: bool = True,
) -> dict:
    """Apply a neutral RAW starting point before creative editing.

    The default ``apple_proraw_natural`` profile compensates for the dark/flat
    starting render commonly seen when Apple ProRAW DNGs are opened through a
    neutral Darktable CLI workflow. It is not a final style preset.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"status": "error", "error": str(e)}

    if profile not in STARTING_POINTS:
        return {
            "status": "error",
            "error": f"Unknown starting point profile: {profile}",
            "available_profiles": sorted(STARTING_POINTS),
        }

    if only_if_unedited and state.has_changes():
        return {
            "status": "skipped",
            "reason": "Image already has edits; pass only_if_unedited=false to apply anyway.",
            "edits": state.to_dict(),
        }

    applied = _apply_starting_point_to_state(state, profile)
    state.save()
    return {
        "status": "ok",
        "profile": profile,
        "applied": applied,
        "is_probably_apple_proraw": _looks_like_apple_proraw(path),
        "edits": state.to_dict(),
        "note": STARTING_POINTS[profile]["description"],
    }


@mcp.tool()
def convert_dng_if_needed(image_path: str, output_directory: Optional[str] = None, force: bool = False) -> dict:
    """Convert a DNG with Adobe DNG Converter when useful for Darktable.

    In normal editing, export/render tools do this automatically for likely
    Apple ProRAW DNGs or as a retry when Darktable rejects a DNG. This tool is
    exposed for explicit diagnostics and batch preparation.
    """
    path = Path(image_path)
    if not path.exists():
        return {"status": "error", "error": f"Image not found: {image_path}"}
    if path.suffix.lower() != ".dng":
        return {"status": "skipped", "reason": "not a DNG", "path": str(path)}
    if not DARKTABLE or not DARKTABLE.dng_converter:
        return {"status": "error", "error": "Adobe DNG Converter is not available."}
    should_convert = force or DARKTABLE._should_preconvert_dng(path)
    if not should_convert:
        return {
            "status": "skipped",
            "reason": "DNG does not look like Apple ProRAW and force=false",
            "path": str(path),
        }
    out_dir = Path(output_directory) if output_directory else path.parent / ".darktable-mcp-converted"
    result = DARKTABLE._convert_dng_for_darktable(path, out_dir)
    result["source_path"] = str(path)
    return result


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
def render_and_analyze(
    image_path: str,
    max_size: int = 1200,
    fast: bool = False,
    compare_to_original: bool = True,
) -> list:
    """Render current edits and return both the preview image and analysis metrics.

    This is the main bridge tool for an external editor loop: Claude should
    apply edits, call this tool, inspect the returned image/metrics, then decide
    the next edit pass itself. By default this also renders an unedited baseline
    preview first and returns objective deltas so the client can notice when an
    edit accidentally gets darker/flatter than the original conversion.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return [{"status": "error", "error": str(e)}]

    preview_dir = path.parent / ".darktable-mcp-preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{path.stem}__analysis.jpg"

    baseline_metrics = None
    baseline_result = None
    if compare_to_original:
        baseline_path = preview_dir / f"{path.stem}__analysis_original.jpg"
        baseline_state = EditState(source_path=path)
        baseline_result, _baseline_bytes = _render_current_edits_to_file(
            path,
            baseline_state,
            baseline_path,
            max_size,
            fast=fast,
        )
        if baseline_result.get("status") == "ok":
            baseline_metrics = _image_metrics(baseline_path)

    result, img_bytes = _render_current_edits_to_file(path, state, preview_path, max_size, fast=fast)
    if result.get("status") != "ok":
        return [result]

    analysis = _image_metrics(preview_path)
    analysis.update({
        "status": "ok",
        "preview_path": str(preview_path),
        "max_size": max_size,
        "fast": fast,
        "edits": state.to_dict(),
        "render": result,
        "baseline": baseline_metrics,
        "baseline_render": baseline_result,
        "delta_from_original": _metric_delta(analysis, baseline_metrics) if baseline_metrics else None,
        "regional_delta_from_original": _regional_metric_delta(analysis, baseline_metrics) if baseline_metrics else None,
        "diagnostic_warnings": _diagnostic_warnings(analysis, baseline_metrics, state, fast=fast),
    })
    return [analysis, Image(data=img_bytes, format="jpeg")]


@mcp.tool()
def compare_to_reference(
    image_path: str,
    reference_image_path: str,
    max_size: int = 1200,
    fast: bool = False,
) -> dict:
    """Render current edits and compare tone/color/detail metrics to a reference JPEG."""
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"status": "error", "error": str(e)}
    reference = Path(reference_image_path)
    if not reference.exists():
        return {"status": "error", "error": f"Reference image not found: {reference_image_path}"}

    preview_dir = path.parent / ".darktable-mcp-preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{path.stem}__compare.jpg"
    result, _img_bytes = _render_current_edits_to_file(path, state, preview_path, max_size, fast=fast)
    if result.get("status") != "ok":
        return result

    current = _image_metrics(preview_path)
    reference_metrics = _image_metrics(reference)
    delta = _metric_delta(current, reference_metrics)
    regional_delta = _regional_metric_delta(current, reference_metrics)
    return {
        "status": "ok",
        "preview_path": str(preview_path),
        "reference_path": str(reference),
        "fast": fast,
        "current": current,
        "reference": reference_metrics,
        "delta": delta,
        "regional_delta": regional_delta,
        "suggested_next_steps": _reference_match_suggestions(delta, regional_delta),
        "diagnostic_warnings": _diagnostic_warnings(current, reference_metrics, state, fast=fast),
    }


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
    sharpening_masking: Optional[float] = None,
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
    sharpening_masking : float
        Edge masking for sharpening (0 to 100). Similar in intent to Lightroom's
        sharpening masking slider: higher values protect smooth areas such as sky.
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
        "sharpening_masking": sharpening_masking,
        "noise_reduction": noise_reduction,
        "vignette": vignette,
        "clarity": clarity,
        "dehaze": dehaze,
    }.items() if v is not None}

    state.update(params)
    state.save()

    return {"status": "ok", "applied": list(params.keys()), "edits": state.to_dict()}


@mcp.tool()
def apply_edit_recipe(
    image_path: str,
    adjustments: Optional[dict] = None,
    crop: Optional[dict] = None,
    output_name: Optional[str] = None,
    clear_local_masks: bool = False,
) -> dict:
    """Apply a generic edit recipe to an image sidecar.

    This is intentionally not a creative preset. It is a bridge operation that
    lets the client/Claude set any supported adjustment fields, optional crop
    coordinates, and an output name before rendering/analyzing again.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"status": "error", "error": str(e)}

    applied: list[str] = []
    if adjustments:
        valid_adjustments = {field.name for field in fields(state.adjustments)}
        unknown = sorted(set(adjustments) - valid_adjustments)
        if unknown:
            return {
                "status": "error",
                "error": f"Unknown adjustment field(s): {', '.join(unknown)}",
                "supported_adjustments": sorted(valid_adjustments),
            }
        state.update(adjustments)
        applied.extend(adjustments.keys())

    if crop is not None:
        valid_crop = {field.name for field in fields(CropState)}
        unknown = sorted(set(crop) - valid_crop)
        if unknown:
            return {
                "status": "error",
                "error": f"Unknown crop field(s): {', '.join(unknown)}",
                "supported_crop_fields": sorted(valid_crop),
            }
        current = state.crop or CropState()
        crop_data = {field.name: getattr(current, field.name) for field in fields(CropState)}
        crop_data.update(crop)
        new_crop = CropState(**crop_data)
        if new_crop.right <= new_crop.left or new_crop.bottom <= new_crop.top:
            return {"status": "error", "error": "Invalid crop: right must be > left and bottom must be > top."}
        state.crop = new_crop
        applied.append("crop")

    if output_name is not None:
        state.output_name = Path(output_name).stem or output_name
        applied.append("output_name")

    if clear_local_masks:
        state.local_adjustments = []
        applied.append("clear_local_masks")

    state.save()
    return {
        "status": "ok",
        "applied": applied,
        "edits": state.to_dict(),
        "native_xmp_first": True,
        "mcp_finishing_needed": _needs_mcp_finishing(state),
    }


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
def cleanup_temporary_files(image_path: str, include_converted: bool = False) -> dict:
    """Remove MCP-generated temporary preview/analysis files for one source image.

    This removes files in `.darktable-mcp-preview` and the viewer preview copy
    named `original__preview.jpg`. It does not remove the original image, MCP
    edit sidecar, Darktable XMP, or final exports. Pass `include_converted=true`
    to also remove explicit files in `.darktable-mcp-converted` for this source.
    """
    path = Path(image_path)
    if not path.exists():
        return {"status": "error", "error": f"Image not found: {image_path}"}

    result = _cleanup_generated_files(path, include_converted=include_converted)
    result["status"] = "ok" if not result["errors"] else "partial"
    return result


@mcp.tool()
def export_image(
    image_path: str,
    output_directory: Optional[str] = None,
    format: str = "jpeg",
    quality: int = 100,
    use_darktable_cli: bool = True,
    write_xmp_sidecar: bool = True,
    max_dimension: Optional[int] = None,
    cleanup_temporary: bool = True,
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
        JPEG quality 1–100 (default 100).  Ignored for PNG/TIFF.
    use_darktable_cli : bool
        Try to use darktable-cli for export when available (better RAW rendering).
    write_xmp_sidecar : bool
        Write a Darktable-compatible XMP sidecar alongside the source file.
    max_dimension : int, optional
        Resize so the longest edge is at most this many pixels.
    cleanup_temporary : bool
        Remove MCP-generated preview/analysis files for this image after a
        successful final export (default true).
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

    result = _export_state_to_path(
        path=path,
        state=state,
        out_path=out_path,
        format_name=format,
        quality=quality,
        use_darktable_cli=use_darktable_cli,
        write_xmp_sidecar=write_xmp_sidecar,
        max_dimension=max_dimension,
    )
    if result.get("status") == "ok" and out_path.exists():
        metrics = _image_metrics(out_path)
        result["metrics"] = metrics
        result["diagnostic_warnings"] = _diagnostic_warnings(metrics, state=state)
        if cleanup_temporary:
            result["cleanup"] = _cleanup_generated_files(path)
    return result


def _export_via_darktable_cli(
    src: Path,
    dst: Path,
    xmp: Optional[Path],
    quality: int,
    max_dimension: Optional[int] = None,
    allow_dng_conversion: bool = True,
    config_directory: Optional[Path] = None,
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

    return DARKTABLE.export(
        src,
        dst,
        xmp,
        quality=quality,
        max_dimension=max_dimension,
        allow_dng_conversion=allow_dng_conversion,
        config_directory=config_directory,
    )


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
