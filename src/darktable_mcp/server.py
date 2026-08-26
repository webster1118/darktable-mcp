"""Darktable MCP Server — photo editing tools for Claude."""
from __future__ import annotations

import io
import os
import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer, Image
from PIL import Image as PILImage

from .edits import CropState, EditState
from .processor import ALL_EXTENSIONS, RAW_EXTENSIONS, ImageProcessor
from .xmp_writer import write_xmp

mcp = MCPServer("DarktableMCP")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_dt_cli_windows() -> Optional[str]:
    """Find darktable-cli.exe on Windows."""

    candidates = [
        # User installation
        Path.home() / "AppData" / "Local" / "Programs" / "darktable" / "bin" / "darktable-cli.exe",

        # Standard machine installation
        Path(r"C:\Program Files\darktable\bin\darktable-cli.exe"),

        # 32-bit installation
        Path(r"C:\Program Files (x86)\darktable\bin\darktable-cli.exe"),

        # Explicit environment variable
        Path(os.environ["DARKTABLE_CLI"])
        if os.environ.get("DARKTABLE_CLI")
        else None,
    ]

    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)

    return None

DARKTABLE_CLI = shutil.which("darktable-cli") or _find_dt_cli_windows()


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
        if not DARKTABLE_CLI:
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

        img_bytes = preview_path.read_bytes()

        return Image(data=img_bytes, format="jpeg"), img_bytes

    # Non-RAW files can continue to use the existing Pillow pipeline.
    proc = ImageProcessor(path)
    img = proc.process(state, preview_size=max_size)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)

    return Image(data=buf.getvalue(), format="jpeg"), buf.getvalue()


def _save_and_open_preview(path: Path, img_bytes: bytes) -> str:
    """Save preview to disk next to the source file and open it."""
    import os
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
    info["darktable_cli_available"] = DARKTABLE_CLI is not None
    return info


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
    # Colour
    saturation: Optional[float] = None,
    vibrance: Optional[float] = None,
    # Detail
    sharpness: Optional[float] = None,
    noise_reduction: Optional[float] = None,
    # Effects
    vignette: Optional[float] = None,
    clarity: Optional[float] = None,
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
        "saturation": saturation,
        "vibrance": vibrance,
        "sharpness": sharpness,
        "noise_reduction": noise_reduction,
        "vignette": vignette,
        "clarity": clarity,
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

        if not DARKTABLE_CLI:
            return {
                "status": "error",
                "error": (
                    "darktable-cli was not found. "
                    "RAW export cannot continue without Darktable."
                ),
            }

        result = _export_via_darktable_cli(
            src=path,
            dst=out_path,
            xmp=xmp_path,
            quality=quality,
            max_dimension=max_dimension,
        )

        if result.get("status") == "ok":
            result["xmp_sidecar"] = str(xmp_path) if xmp_path else None
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

    if not DARKTABLE_CLI:
        return {
            "status": "error",
            "error": "darktable-cli is not available",
        }

    src = Path(src).resolve()
    dst = Path(dst).resolve()

    width = str(max_dimension) if max_dimension else "0"
    height = str(max_dimension) if max_dimension else "0"

    # IMPORTANT:
    # Use forward slashes for Windows paths passed to darktable-cli.
    darktable_exe = Path(DARKTABLE_CLI).resolve().as_posix()

    src_arg = src.as_posix()
    dst_arg = dst.as_posix()
    
    cmd = [
        darktable_exe,
        src_arg,
    ]

    if xmp:
        xmp_arg = Path(xmp).resolve().as_posix()
        cmd.append(xmp_arg)

    cmd.extend([
        dst_arg,
        "--width",
        width,
        "--height",
        height,
        "--hq",
        "true",
        "--core",
        "--conf",
        f"plugins/imageio/format/jpeg/quality={quality}",
    ])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "error": "darktable-cli timed out after 180 seconds.",
            "command": cmd,
        }

    except OSError as exc:
        return {
            "status": "error",
            "error": f"Could not start darktable-cli: {exc}",
            "command": cmd,
        }

    if result.returncode != 0:
        return {
            "status": "error",
            "error": "darktable-cli failed.",
            "returncode": result.returncode,
            "stdout": result.stdout[-3000:],
            "stderr": result.stderr[-3000:],
            "command": cmd,
        }

    if not dst.exists():
        return {
            "status": "error",
            "error": (
                "darktable-cli returned success but did not create "
                f"the expected output file: {dst}"
            ),
            "stdout": result.stdout[-3000:],
            "stderr": result.stderr[-3000:],
            "command": cmd,
        }

    return {
        "status": "ok",
        "output_path": str(dst),
        "rendered_via": "darktable-cli",
        "size_bytes": dst.stat().st_size,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }


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
