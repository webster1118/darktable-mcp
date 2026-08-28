"""Write Darktable-compatible XMP sidecar files from an EditState.

Darktable stores per-module history items in its XMP sidecars.  Each item's
``darktable:params`` field is a little-endian hex-encoded binary blob whose
layout matches the module's C struct (version-dependent).

We currently write module payloads verified against Darktable 5.6:
  - exposure   (modversion 6)
  - temperature (modversion 4)
  - basicadj   (modversion 2)
  - colorbalancergb (modversion 5)
  - sigmoid    (modversion 3)
  - hazeremoval (modversion 3)
  - toneequal  (modversion 2)
  - crop       (modversion 3, without rotation)
  - sharpen    (modversion 1)
  - denoiseprofile (modversion 12)
  - bilat      (modversion 3)
  - vignette   (modversion 4)

Other MCP adjustments are applied after the Darktable render by the server's
Pillow finishing pass until their Darktable 5.6 module payloads are verified.
"""
from __future__ import annotations

import math
import struct
from html import escape
from pathlib import Path
from .edits import CropState, EditState, LocalAdjustmentState

# ---------------------------------------------------------------------------
# Blendop default (7 bytes + padding, version 7)
# Values from a vanilla Darktable export with no blending.
# ---------------------------------------------------------------------------
_BLENDOP_DEFAULT = (
    "00000000"  # mask_mode = DEVELOP_MASK_DISABLED (0)
    "00000000"  # blend_mode = DEVELOP_BLEND_NORMAL (0)
    "000000000000000000000000"  # opacity = 1.0 → we encode as float below
    "00000000"  # mask_id = 0
    "07000000"  # blendop_version = 7
    "00000000" * 16  # feathering / details padding
)

# Simpler constant pulled from real darktable output
_BLENDOP_V7 = (
    "0c000000"  # mask_mode DEVELOP_MASK_ENABLED | DEVELOP_MASK_MASK_CONDITIONAL
    "0c000000"  # DEVELOP_BLEND_NORMAL8
    + "0000803f"  # opacity 1.0 (float LE)
    + "00000000"  # mask_id
    + "00000000" * 30  # padding to 140 bytes
)

# Use a safe minimal blendop blob that darktable accepts
def _blendop() -> str:
    # 140-byte blendop_params_v7: just set opacity=1.0, rest zeros
    buf = bytearray(140)
    # mask_mode = 0 (DEVELOP_MASK_DISABLED)
    struct.pack_into("<I", buf, 0, 0)
    # blend_mode = 12 (DEVELOP_BLEND_NORMAL_UNBOUNDED in older DT; 0 = passthrough)
    struct.pack_into("<I", buf, 4, 0)
    # opacity = 1.0
    struct.pack_into("<f", buf, 8, 1.0)
    return buf.hex()


def _masked_blendop(mask_id: int, opacity: float = 1.0, invert: bool = False) -> str:
    """Legacy blendop version 7 with a drawn-mask reference.

    Darktable 5.6 upgrades v7 blend params internally.  The v7 layout is small
    and stable enough for the MCP writer: mask mode, blend mode, opacity,
    combine mode, mask id, blendif, blur radius, reserved fields, then the
    neutral blendif curve values.
    """
    buf = bytearray(300)
    # DEVELOP_MASK_ENABLED | DEVELOP_MASK_MASK
    struct.pack_into("<I", buf, 0, 3)
    # DEVELOP_BLEND_NORMAL2 in modern Darktable; accepted as legacy normal.
    struct.pack_into("<I", buf, 4, 0x18)
    # Darktable's blend opacity is stored as a percentage.
    struct.pack_into("<f", buf, 8, max(0.0, min(1.0, opacity)) * 100.0)
    # DEVELOP_COMBINE_NORM_EXCL or DEVELOP_COMBINE_INV_EXCL
    struct.pack_into("<I", buf, 12, 1 if invert else 0)
    struct.pack_into("<I", buf, 16, mask_id)
    struct.pack_into("<I", buf, 20, 0)
    struct.pack_into("<f", buf, 24, 0.0)
    offset = 44
    for _ in range(16):
        struct.pack_into("<ffff", buf, offset, 0.0, 0.0, 1.0, 1.0)
        offset += 16
    return buf.hex()


def _parametric_blendop(
    local: LocalAdjustmentState,
    opacity: float = 1.0,
    *,
    mask_id: int = 0,
) -> str:
    """Legacy blendop version 7 with a Darktable parametric mask."""
    buf = bytearray.fromhex(_masked_blendop(mask_id, opacity, local.invert))
    mask_mode = 1 | 4
    if mask_id:
        mask_mode |= 2
    struct.pack_into("<I", buf, 0, mask_mode)
    channel = _parametric_channel_index(local.parametric_channel)
    struct.pack_into("<I", buf, 20, 1 << channel)
    offset = 44 + channel * 16
    low = max(0.0, min(1.0, local.parametric_low))
    low_soft = max(0.0, min(1.0, local.parametric_low_soft))
    high_soft = max(0.0, min(1.0, local.parametric_high_soft))
    high = max(0.0, min(1.0, local.parametric_high))
    values = sorted([low, low_soft, high_soft, high])
    struct.pack_into("<ffff", buf, offset, *values)
    return buf.hex()


def _parametric_channel_index(channel: str) -> int:
    normalized = channel.strip().lower().replace("-", "_")
    channels = {
        "luminance": 0,
        "gray": 0,
        "grey": 0,
        "red": 1,
        "green": 2,
        "blue": 3,
        "output_luminance": 4,
        "output_gray": 4,
        "output_grey": 4,
        "output_red": 5,
        "output_green": 6,
        "output_blue": 7,
        "hue": 8,
        "saturation_channel": 9,
        "chroma": 9,
        "lightness": 10,
    }
    return channels.get(normalized, 0)


# ---------------------------------------------------------------------------
# Module param encoders
# ---------------------------------------------------------------------------

def _encode_exposure(exposure_ev: float, black: float = 0.0) -> str:
    """exposure module version 6.

    struct dt_iop_exposure_params_t {
        dt_iop_exposure_mode_t mode;  // int32, 0=MANUAL
        float black;
        float exposure;
        float deflicker_percentile;
        float deflicker_target_level;
    }
    """
    data = struct.pack("<iffff", 0, black, exposure_ev, 50.0, -4.0)
    return data.hex()


def _encode_sigmoid(contrast: float, skew: float = 0.0) -> str:
    """sigmoid module version 3.

    struct dt_iop_sigmoid_params_t {
        float middle_grey_contrast;
        float contrast_skewness;
        float display_white_target;
        float display_black_target;
        int32 color_processing;
        float hue_preservation;
        float red_inset, red_rotation;
        float green_inset, green_rotation;
        float blue_inset, blue_rotation;
        float purity;
        int32 base_primaries;
    }
    """
    middle_grey_contrast = 1.5 + max(-100.0, min(100.0, contrast)) / 100.0 * 2.0
    middle_grey_contrast = max(0.1, min(10.0, middle_grey_contrast))
    contrast_skewness = max(-1.0, min(1.0, skew / 100.0))
    data = struct.pack(
        "<ffffiffffffffi",
        middle_grey_contrast,
        contrast_skewness,
        100.0,
        0.0152,
        0,
        100.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1,
    )
    return data.hex()


def _encode_hazeremoval(dehaze: float, distance: float = 0.75) -> str:
    """haze removal module version 3."""
    strength = max(-1.0, min(1.0, dehaze / 100.0))
    distance = max(0.0, min(1.0, distance))
    data = struct.pack("<ffii", strength, distance, 0, 1)
    return data.hex()


def _encode_temperature(temp_k: float, tint: float = 0.0) -> str:
    """temperature module version 4.

    struct dt_iop_temperature_params_t {
        float red;
        float green;
        float blue;
        float various;
        int preset;
    }

    The MCP stores temperature/tint as Lightroom-like intent. Darktable stores
    camera-channel multipliers. This approximation is intentionally conservative
    and keeps green near 1.0 while warming/cooling red and blue.
    """
    ref_k = 5500.0
    ratio = max(1901.0, min(25000.0, temp_k)) / ref_k
    r = ratio ** 0.8
    g = 1.0 - max(-100.0, min(100.0, tint)) / 100.0 * 0.12
    b = (1.0 / ratio) ** 0.8
    mn = min(r, g, b)
    r, g, b = r / mn, g / mn, b / mn

    # preset = DT_IOP_TEMP_USER
    return struct.pack("<ffffi", r, g, b, 1.0, 2).hex()


def _encode_basicadj(brightness: float = 0.0, highlights: float = 0.0) -> str:
    """basicadj module version 2.

    We use this deprecated-but-supported module only for controls that map
    cleanly enough to its current params: brightness and highlight compression.
    Color and contrast are handled by colorbalancergb instead.

    struct dt_iop_basicadj_params_t {
        float black_point;
        float exposure;
        float hlcompr;
        float hlcomprthresh;
        float contrast;
        int preserve_colors;
        float middle_grey;
        float brightness;
        float saturation;
        float vibrance;
        float clip;
    }
    """
    b = max(-1.0, min(1.0, brightness / 100.0))
    hlcompr = max(0.0, min(500.0, highlights * 5.0))
    data = struct.pack(
        "<fffffifffff",
        0.0,       # black_point
        0.0,       # exposure
        hlcompr,
        0.0,       # hlcomprthresh
        0.0,       # contrast
        1,         # preserve colors: luminance
        18.42,     # middle_grey
        b,
        0.0,       # saturation
        0.0,       # vibrance
        0.0,       # clip
    )
    return data.hex()


def _encode_crop(crop: CropState) -> str:
    """crop module version 3.

    struct dt_iop_crop_params_t {
        float cx, cy, cw, ch;   // left, top, right, bottom; normalised 0..1
        int ratio_n;
        int ratio_d;
    }
    """
    ratio_n, ratio_d = _crop_ratio(crop)
    data = struct.pack(
        "<ffffii",
        crop.left,
        crop.top,
        crop.right,
        crop.bottom,
        ratio_n,
        ratio_d,
    )
    return data.hex()


def _encode_ashift_rotation(rotation: float) -> str:
    """rotate and perspective (ashift) module version 5 for native straightening."""
    clamped_rotation = max(-180.0, min(180.0, rotation))
    last_drawn_lines = [0.0] * (50 * 4)
    last_quad_lines = [0.0] * 8
    return struct.pack(
        "<ffffffffii" + "ffff" + "f" * len(last_drawn_lines) + "i" + "f" * len(last_quad_lines),
        clamped_rotation,
        0.0,   # lensshift_v
        0.0,   # lensshift_h
        0.0,   # shear
        28.0,  # f_length
        1.0,   # crop_factor
        100.0, # orthocorr
        1.0,   # aspect
        0,     # ASHIFT_MODE_GENERIC
        0,     # ASHIFT_CROP_OFF
        0.0,   # cl
        1.0,   # cr
        0.0,   # ct
        1.0,   # cb
        *last_drawn_lines,
        0,
        *last_quad_lines,
    ).hex()


def _crop_ratio(crop: CropState) -> tuple[int, int]:
    width = max(0.0001, crop.right - crop.left)
    height = max(0.0001, crop.bottom - crop.top)
    ratio = width / height
    if abs(ratio - 16 / 9) < 0.01:
        return 16, 9
    if abs(ratio - 4 / 3) < 0.01:
        return 4, 3
    if abs(ratio - 3 / 2) < 0.01:
        return 3, 2
    if abs(ratio - 1.0) < 0.01:
        return 1, 1
    return -1, -1


def _encode_sharpen(amount: float, masking: float = 0.0) -> str:
    """sharpen module version 1.

    struct dt_iop_sharpen_params_t {
        float radius;
        float amount;
        float threshold;
    }
    """
    clamped_amount = max(0.0, min(100.0, amount))
    clamped_masking = max(0.0, min(100.0, masking))
    radius = 1.0 + clamped_amount / 100.0 * 3.0
    amt = clamped_amount / 100.0 * 0.75
    threshold = 0.005 + clamped_masking / 100.0 * 0.055
    data = struct.pack("<fff", radius, amt, threshold)
    return data.hex()


def _encode_denoiseprofile(strength: float) -> str:
    """denoise (profiled) module version 12.

    struct dt_iop_denoiseprofile_params_t {
        float radius, nbhood, strength, shadows, bias, scattering;
        float central_pixel_weight, overshooting;
        float a[3], b[3];
        int mode;
        float x[6][7], y[6][7];
        gboolean wb_adaptive_anscombe;
        gboolean fix_anscombe_and_nlmeans_norm;
        gboolean use_new_vst;
        int wavelet_color_mode;
        gboolean compensate_hilite_pres;
    }

    Use wavelets auto with profile auto-detection. The x/y curves are initialized
    like Darktable's own wavelet presets: evenly spaced x, neutral y=0.5.
    """
    amount = max(0.001, min(1000.0, 1.0 + strength / 100.0 * 1.5))
    x_values: list[float] = []
    y_values: list[float] = []
    for _channel in range(6):
        for band in range(7):
            x_values.append(band / 6.0)
            y_values.append(0.5)
    return struct.pack(
        "<" + "f" * 14 + "i" + "f" * 42 + "f" * 42 + "iiiii",
        1.0,       # radius
        7.0,       # nbhood
        amount,
        0.0,       # shadows
        0.0,       # bias
        0.0,       # scattering
        0.1,       # central_pixel_weight
        1.0,       # overshooting
        -1.0,      # a[0] = autodetect profile
        0.0,
        0.0,
        0.0,       # b[0]
        0.0,
        0.0,
        4,         # MODE_WAVELETS_AUTO
        *x_values,
        *y_values,
        1,         # wb_adaptive_anscombe
        1,         # fix_anscombe_and_nlmeans_norm
        1,         # use_new_vst
        1,         # MODE_Y0U0V0
        0,         # compensate_hilite_pres
    ).hex()


def _encode_vignette(amount: float) -> str:
    """vignette module version 4.

    struct dt_iop_vignette_params_t {
        float scale;
        float falloff_scale;
        float brightness;
        float saturation;
        dt_iop_vector_2d_t center;
        gboolean autoratio;
        float whratio;
        float shape;
        dt_iop_dither_t dithering;
        gboolean unbound;
    }
    """
    brightness = max(-1.0, min(1.0, amount / 100.0))
    saturation = 0.0
    data = struct.pack(
        "<ffffffiffii",
        80.0,       # scale
        50.0,       # falloff_scale
        brightness,
        saturation,
        0.0,        # center.x
        0.0,        # center.y
        1,          # autoratio
        1.0,        # whratio
        1.0,        # shape
        0,          # dithering off
        1,          # unbound
    )
    return data.hex()


def _encode_colorbalancergb(
    contrast: float = 0.0,
    saturation: float = 0.0,
    vibrance: float = 0.0,
) -> str:
    """colorbalancergb module version 5."""
    sat = max(-1.0, min(1.0, saturation / 100.0))
    vib = max(-1.0, min(1.0, vibrance / 100.0))
    con = max(-1.0, min(1.0, contrast / 100.0))
    floats = [
        # shadows/midtones/highlights/global Y/C/H
        0.0, 0.0, 0.0,
        0.0, 0.0, 0.0,
        0.0, 0.0, 0.0,
        0.0, 0.0, 0.0,
        # mask falloff defaults
        1.0, 0.0, 1.0,
        # chroma shadows/highlights/global/midtones
        0.0, 0.0, 0.0, 0.0,
        # saturation global/highlights/midtones/shadows
        sat, 0.0, 0.0, 0.0,
        # hue angle
        0.0,
        # brilliance global/highlights/midtones/shadows
        0.0, 0.0, 0.0, 0.0,
        # mask grey fulcrum, vibrance, grey fulcrum, contrast
        0.1845, vib, 0.1845, con,
    ]
    return struct.pack("<" + "f" * len(floats) + "i", *floats, 1).hex()


def _encode_toneequal(shadows: float = 0.0, whites: float = 0.0, blacks: float = 0.0) -> str:
    """toneequal module version 2 for global shadows/whites/blacks."""
    sh = max(-2.0, min(2.0, shadows / 100.0 * 1.5))
    wh = max(-2.0, min(2.0, whites / 100.0 * 1.25))
    bl = max(-2.0, min(2.0, blacks / 100.0 * 1.25))
    floats = [
        bl * 0.8,   # noise / deepest blacks
        bl,
        bl,
        bl * 0.7,
        sh,
        sh * 0.35,
        wh * 0.35,
        wh,
        wh * 0.7,
        5.0,        # blending
        1.414213562,
        1.0,        # feathering
        0.0,        # quantization
        0.0,        # contrast_boost
        0.0,        # exposure_boost
    ]
    return struct.pack(
        "<" + "f" * len(floats) + "iii",
        *floats,
        4,  # DT_TONEEQ_EIGF
        4,  # DT_TONEEQ_NORM_2
        1,  # iterations
    ).hex()


def _encode_bilat(clarity: float) -> str:
    """bilat/local contrast module version 3."""
    detail = max(-1.0, min(4.0, 0.25 + clarity / 100.0))
    return struct.pack(
        "<iffff",
        1,     # s_mode_local_laplacian
        0.0,   # sigma_r
        0.0,   # sigma_s
        detail,
        0.5,   # midtone
    ).hex()


def _local_native_supported(local: LocalAdjustmentState) -> bool:
    return local.mask_type in {"linear_gradient", "ellipse", "path", "brush", "parametric"} and local.enabled and local.has_changes()


def local_adjustments_are_native_safe(edit_state: EditState) -> bool:
    """Return True when local adjustments can be represented as Darktable masks."""
    if not edit_state.local_adjustments:
        return True
    if edit_state.crop and abs(edit_state.crop.rotation) > 0.01:
        # Darktable drawn-mask coordinates live in source-image space.  Rotated
        # MCP crop finishing changes that space, so keep that path in MCP.
        return False
    return all(
        (not item.enabled or not item.has_changes() or _local_native_supported(item))
        for item in edit_state.local_adjustments
    )


def _crop_is_native_safe(edit_state: EditState) -> bool:
    return edit_state.crop is not None and local_adjustments_are_native_safe(edit_state)


def _masked_source_points(local: LocalAdjustmentState, crop: CropState | None) -> tuple[float, float, float, float]:
    """Convert MCP visible-frame mask points to Darktable source-frame points."""
    if crop:
        width = crop.right - crop.left
        height = crop.bottom - crop.top
        start_x = crop.left + local.start_x * width
        start_y = crop.top + local.start_y * height
        end_x = crop.left + local.end_x * width
        end_y = crop.top + local.end_y * height
    else:
        start_x, start_y = local.start_x, local.start_y
        end_x, end_y = local.end_x, local.end_y
    return (
        max(0.0, min(1.0, start_x)),
        max(0.0, min(1.0, start_y)),
        max(0.0, min(1.0, end_x)),
        max(0.0, min(1.0, end_y)),
    )


def _source_point(x: float, y: float, crop: CropState | None) -> tuple[float, float]:
    if crop:
        x = crop.left + x * (crop.right - crop.left)
        y = crop.top + y * (crop.bottom - crop.top)
    return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))


def _encode_gradient_mask_points(local: LocalAdjustmentState, crop: CropState | None) -> str:
    start_x, start_y, end_x, end_y = _masked_source_points(local, crop)
    low_x, low_y, high_x, high_y = (
        (end_x, end_y, start_x, start_y) if local.invert
        else (start_x, start_y, end_x, end_y)
    )
    dx = high_x - low_x
    dy = high_y - low_y
    length = math.hypot(dx, dy)
    anchor_x = (start_x + end_x) / 2.0
    anchor_y = (start_y + end_y) / 2.0
    if length < 0.0001:
        rotation = 0.0
        compression = 0.12
    else:
        ux = dx / length
        uy = dy / length
        # Darktable rotation 0 means the high-opacity side points upward.
        rotation = -math.degrees(math.atan2(ux, -uy))
        compression = max(0.02, min(0.5, length / math.sqrt(2.0) / 2.0))
    # dt_masks_point_gradient_t: anchor[2], rotation, compression,
    # steepness, curvature, state.  Use linear state for predictable ramps.
    return struct.pack(
        "<ffffffi",
        anchor_x,
        anchor_y,
        rotation,
        compression,
        0.0,
        0.0,
        1,
    ).hex()


def _encode_ellipse_mask_points(local: LocalAdjustmentState, crop: CropState | None) -> str:
    center_x, center_y = _source_point(local.center_x, local.center_y, crop)
    scale_x = crop.right - crop.left if crop else 1.0
    scale_y = crop.bottom - crop.top if crop else 1.0
    radius_x = max(0.001, min(1.0, local.radius_x * scale_x))
    radius_y = max(0.001, min(1.0, local.radius_y * scale_y))
    border = max(0.0, min(0.5, local.feather * max(scale_x, scale_y)))
    return struct.pack(
        "<ffffffi",
        center_x,
        center_y,
        radius_x,
        radius_y,
        local.rotation,
        border,
        1,  # proportional radii
    ).hex()


def _normalised_points(points: list[list[float]], crop: CropState | None) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for point in points:
        if len(point) < 2:
            continue
        out.append(_source_point(float(point[0]), float(point[1]), crop))
    return out


def _encode_path_mask_points(local: LocalAdjustmentState, crop: CropState | None) -> str:
    points = _normalised_points(local.path_points, crop)
    if len(points) < 3:
        cx, cy = _source_point(local.center_x, local.center_y, crop)
        rx = max(0.001, local.radius_x * (crop.right - crop.left if crop else 1.0))
        ry = max(0.001, local.radius_y * (crop.bottom - crop.top if crop else 1.0))
        points = [
            (cx - rx, cy - ry),
            (cx + rx, cy - ry),
            (cx + rx, cy + ry),
            (cx - rx, cy + ry),
        ]
        points = [(max(0.0, min(1.0, x)), max(0.0, min(1.0, y))) for x, y in points]
    border = max(0.0, min(0.5, local.feather))
    buf = bytearray()
    for x, y in points:
        buf.extend(struct.pack(
            "<ffffffffi",
            x,
            y,
            x,
            y,
            x,
            y,
            border,
            border,
            1,
        ))
    return buf.hex()


def _encode_brush_mask_points(local: LocalAdjustmentState, crop: CropState | None) -> str:
    points = _normalised_points(local.brush_points or local.path_points, crop)
    if len(points) < 2:
        sx, sy, ex, ey = _masked_source_points(local, crop)
        points = [(sx, sy), (ex, ey)]
    border = max(0.001, min(0.5, local.feather))
    density = max(0.0, min(1.0, local.opacity))
    hardness = max(0.0, min(1.0, local.hardness))
    buf = bytearray()
    for x, y in points:
        buf.extend(struct.pack(
            "<ffffffffffi",
            x,
            y,
            x,
            y,
            x,
            y,
            border,
            border,
            density,
            hardness,
            1,
        ))
    return buf.hex()


def _mask_type_code(local: LocalAdjustmentState) -> int | None:
    return {
        "linear_gradient": 16,
        "ellipse": 32,
        "path": 2,
        "brush": 64,
    }.get(local.mask_type)


def _encode_drawn_mask_points(local: LocalAdjustmentState, crop: CropState | None) -> str:
    if local.mask_type == "linear_gradient":
        return _encode_gradient_mask_points(local, crop)
    if local.mask_type == "ellipse":
        return _encode_ellipse_mask_points(local, crop)
    if local.mask_type == "path":
        return _encode_path_mask_points(local, crop)
    if local.mask_type == "brush":
        return _encode_brush_mask_points(local, crop)
    raise ValueError(f"Unsupported drawn mask type: {local.mask_type}")


def _mask_points_count(local: LocalAdjustmentState) -> int:
    if local.mask_type in {"linear_gradient", "ellipse"}:
        return 1
    if local.mask_type == "path":
        return max(3, len([p for p in local.path_points if len(p) >= 2]))
    if local.mask_type == "brush":
        return max(2, len([p for p in (local.brush_points or local.path_points) if len(p) >= 2]))
    return 0


def _local_adjustment_modules(local: LocalAdjustmentState) -> list[tuple[str, int, str]]:
    modules: list[tuple[str, int, str]] = []
    if local.exposure_ev != 0.0:
        modules.append(("exposure", 6, _encode_exposure(local.exposure_ev, 0.0)))
    if local.brightness != 0.0 or local.highlights != 0.0:
        modules.append(("basicadj", 2, _encode_basicadj(local.brightness, local.highlights)))
    if local.contrast != 0.0 or local.saturation != 0.0 or local.vibrance != 0.0:
        modules.append((
            "colorbalancergb",
            5,
            _encode_colorbalancergb(local.contrast, local.saturation, local.vibrance),
        ))
    if local.sigmoid_contrast != 0.0 or local.sigmoid_skew != 0.0:
        modules.append(("sigmoid", 3, _encode_sigmoid(local.sigmoid_contrast, local.sigmoid_skew)))
    if local.dehaze != 0.0:
        modules.append(("hazeremoval", 3, _encode_hazeremoval(local.dehaze)))
    if local.shadows != 0.0 or local.whites != 0.0 or local.blacks != 0.0:
        modules.append(("toneequal", 2, _encode_toneequal(local.shadows, local.whites, local.blacks)))
    if local.clarity != 0.0:
        modules.append(("bilat", 3, _encode_bilat(local.clarity)))
    return modules


_MASK_ITEM = """\
     <rdf:li
      darktable:mask_num="{mask_num}"
      darktable:mask_id="{mask_id}"
      darktable:mask_type="{mask_type}"
      darktable:mask_name="{mask_name}"
      darktable:mask_version="6"
      darktable:mask_points="{mask_points}"
      darktable:mask_nb="{mask_nb}"
      darktable:mask_src="0000000000000000"
      />
"""


# ---------------------------------------------------------------------------
# XMP builder
# ---------------------------------------------------------------------------

_XMP_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 4.4.0-Exiv2">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:darktable="http://darktable.sf.net/"
    darktable:xmp_version="4"
    darktable:raw_params="0"
    darktable:auto_presets_applied="1"
    darktable:history_end="{history_end}"{iop_order_attributes}>
   <darktable:history>
    <rdf:Seq>
{history_items}    </rdf:Seq>
   </darktable:history>
   <darktable:masks_history>
    <rdf:Seq>
{mask_items}    </rdf:Seq>
   </darktable:masks_history>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""

_HISTORY_ITEM = """\
     <rdf:li
      darktable:operation="{operation}"
      darktable:enabled="1"
      darktable:modversion="{modversion}"
      darktable:params="{params}"
      darktable:multi_name="{multi_name}"
      darktable:multi_name_hand_edited="{multi_name_hand_edited}"
      darktable:multi_priority="{multi_priority}"
      darktable:blendop_version="7"
      darktable:blendop_params="{blendop}"
      />
"""


_DARKTABLE_V50_RAW_IOP_ORDER = (
    "rawprepare", "invert", "temperature", "rasterfile", "highlights",
    "cacorrect", "hotpixels", "rawdenoise", "demosaic", "denoiseprofile",
    "bilateral", "rotatepixels", "scalepixels", "lens", "cacorrectrgb",
    "hazeremoval", "ashift", "flip", "enlargecanvas", "overlay",
    "clipping", "liquify", "spots", "retouch", "exposure",
    "mask_manager", "tonemap", "toneequal", "crop", "graduatednd",
    "profile_gamma", "equalizer", "colorin", "channelmixerrgb",
    "contrastntexture", "diffuse", "censorize", "negadoctor", "blurs",
    "primaries", "nlmeans", "colorchecker", "defringe", "atrous",
    "lowpass", "highpass", "sharpen", "colortransfer", "colormapping",
    "channelmixer", "basicadj", "colorharmonizer", "colorbalance",
    "colorequal", "colorbalancergb", "rgbcurve", "rgblevels",
    "basecurve", "filmic", "sigmoid", "agx", "filmicrgb", "lut3d",
    "colisa", "tonecurve", "levels", "shadhi", "zonesystem",
    "globaltonemap", "relight", "bilat", "colorcorrection",
    "colorcontrast", "velvia", "vibrance", "colorzones", "bloom",
    "colorize", "lowlight", "monochrome", "grain", "soften",
    "splittoning", "vignette", "colorreconstruct", "finalscale",
    "colorout", "clahe", "overexposed", "rawoverexposed", "dither",
    "borders", "watermark", "gamma",
)


def _build_iop_order(history_instances: list[tuple[str, int]]) -> tuple[int, str]:
    """Build a Darktable 5.x RAW iop-order list including active multi-instances.

    Darktable's default order contains only base module instances. When an XMP
    history contains repeated local masked modules, Darktable maps history items
    by operation + multi_priority; without those extra instance entries it logs
    "cannot get iop-order" and may order local edits unpredictably.
    """
    extra_instances: dict[str, set[int]] = {}
    for operation, priority in history_instances:
        if priority > 0:
            extra_instances.setdefault(operation, set()).add(priority)

    if not extra_instances:
        return 4, ""

    pairs: list[str] = []
    seen: set[str] = set()
    for operation in _DARKTABLE_V50_RAW_IOP_ORDER:
        seen.add(operation)
        pairs.extend([operation, "0"])
        for priority in sorted(extra_instances.get(operation, ())):
            pairs.extend([operation, str(priority)])

    for operation in sorted(set(extra_instances) - seen):
        for priority in sorted(extra_instances[operation]):
            pairs.extend([operation, str(priority)])

    return 0, ",".join(pairs)


def write_xmp(edit_state: EditState) -> Path:
    """Write a Darktable XMP sidecar for *edit_state* and return its path."""
    adj = edit_state.adjustments
    blendop = _blendop()
    items: list[str] = []
    mask_items: list[str] = []
    multi_priorities: dict[str, int] = {}
    history_instances: list[tuple[str, int]] = []

    def _item(
        op: str,
        ver: int,
        params: str,
        *,
        item_blendop: str = blendop,
        multi_name: str = "",
        multi_priority: int | None = None,
    ) -> str:
        priority = multi_priority
        if priority is None:
            priority = multi_priorities.get(op, 0)
        multi_priorities[op] = max(multi_priorities.get(op, 0), priority + 1)
        history_instances.append((op, priority))
        return _HISTORY_ITEM.format(
            operation=op,
            modversion=ver,
            params=params,
            multi_name=escape(multi_name, quote=True),
            multi_name_hand_edited=1 if multi_name else 0,
            multi_priority=priority,
            blendop=item_blendop,
        )

    # Exposure
    if adj.exposure_ev != 0.0 or adj.black_level != 0.0:
        items.append(_item("exposure", 6, _encode_exposure(adj.exposure_ev, adj.black_level)))

    # White balance / temperature
    if adj.temperature_kelvin is not None:
        items.append(_item("temperature", 4, _encode_temperature(adj.temperature_kelvin, adj.tint)))

    # Basic tone adjustments
    if adj.brightness != 0.0 or adj.highlights != 0.0:
        items.append(_item("basicadj", 2, _encode_basicadj(adj.brightness, adj.highlights)))

    # Color balance RGB for global color/contrast controls
    if adj.contrast != 0.0 or adj.saturation != 0.0 or adj.vibrance != 0.0:
        items.append(_item(
            "colorbalancergb",
            5,
            _encode_colorbalancergb(adj.contrast, adj.saturation, adj.vibrance),
        ))

    # Sigmoid
    if adj.sigmoid_contrast != 0.0 or adj.sigmoid_skew != 0.0:
        items.append(_item("sigmoid", 3, _encode_sigmoid(adj.sigmoid_contrast, adj.sigmoid_skew)))

    # Haze removal
    if adj.dehaze != 0.0:
        items.append(_item("hazeremoval", 3, _encode_hazeremoval(adj.dehaze)))

    # Tone equalizer for shadows / whites / blacks
    if adj.shadows != 0.0 or adj.whites != 0.0 or adj.blacks != 0.0:
        items.append(_item("toneequal", 2, _encode_toneequal(adj.shadows, adj.whites, adj.blacks)))

    # Crop without rotation
    if _crop_is_native_safe(edit_state):
        if abs(edit_state.crop.rotation) > 0.01:
            items.append(_item("ashift", 5, _encode_ashift_rotation(edit_state.crop.rotation)))
        items.append(_item("crop", 3, _encode_crop(edit_state.crop)))

    # Native Darktable drawn masks for local linear-gradient adjustments.
    if local_adjustments_are_native_safe(edit_state):
        next_mask_id = 1
        for local in edit_state.local_adjustments:
            if not _local_native_supported(local):
                continue
            if local.mask_type == "parametric":
                masked_blendop = _parametric_blendop(local, local.opacity)
            else:
                mask_id = next_mask_id
                next_mask_id += 1
                mask_type = _mask_type_code(local)
                if mask_type is None:
                    continue
                mask_items.append(_MASK_ITEM.format(
                    mask_num=mask_id - 1,
                    mask_id=mask_id,
                    mask_type=mask_type,
                    mask_name=escape(local.name, quote=True),
                    mask_points=_encode_drawn_mask_points(local, edit_state.crop),
                    mask_nb=_mask_points_count(local),
                ))
                masked_blendop = _masked_blendop(
                    mask_id,
                    local.opacity,
                    local.invert and local.mask_type != "linear_gradient",
                )
            for op, ver, params in _local_adjustment_modules(local):
                items.append(_item(
                    op,
                    ver,
                    params,
                    item_blendop=masked_blendop,
                    multi_name=local.name,
                ))

    # Sharpening
    if adj.sharpness > 0:
        items.append(_item("sharpen", 1, _encode_sharpen(adj.sharpness, adj.sharpening_masking)))

    # Profiled denoise
    if adj.noise_reduction > 0:
        items.append(_item("denoiseprofile", 12, _encode_denoiseprofile(adj.noise_reduction)))

    # Local contrast / clarity
    if adj.clarity != 0.0:
        items.append(_item("bilat", 3, _encode_bilat(adj.clarity)))

    # Vignette
    if adj.vignette != 0.0:
        items.append(_item("vignette", 4, _encode_vignette(adj.vignette)))

    iop_order_version, iop_order_list = _build_iop_order(history_instances)
    iop_order_attributes = f'\n    darktable:iop_order_version="{iop_order_version}"'
    if iop_order_list:
        iop_order_attributes += f'\n    darktable:iop_order_list="{escape(iop_order_list, quote=True)}"'

    xmp_content = _XMP_TEMPLATE.format(
        history_end=len(items),
        iop_order_attributes=iop_order_attributes,
        history_items="".join(items),
        mask_items="".join(mask_items),
    )

    xmp_path = edit_state.source_path.with_suffix(".xmp")
    xmp_path.write_text(xmp_content, encoding="utf-8")
    return xmp_path
