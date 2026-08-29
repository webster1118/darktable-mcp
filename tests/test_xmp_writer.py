from pathlib import Path
import re
import struct
import tempfile
import unittest

from darktable_mcp.edits import CropState, EditState, LocalAdjustmentState
from darktable_mcp.xmp_writer import (
    _encode_basicadj,
    local_adjustments_are_native_safe,
    write_xmp,
)


class XmpWriterTests(unittest.TestCase):
    def test_negative_highlights_map_to_positive_darktable_compression(self) -> None:
        params = bytes.fromhex(_encode_basicadj(brightness=0, highlights=-73))
        _black, _exposure, compression, *_rest = struct.unpack("<fffffifffff", params)
        self.assertAlmostEqual(compression, 365.0)

    def test_positive_highlights_do_not_apply_opposite_direction_compression(self) -> None:
        params = bytes.fromhex(_encode_basicadj(brightness=0, highlights=20))
        _black, _exposure, compression, *_rest = struct.unpack("<fffffifffff", params)
        self.assertEqual(compression, 0.0)

    def test_writes_verified_exposure_and_native_crop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-xmp-test-") as tmp:
            state = EditState(source_path=Path(tmp) / "photo.dng")
            state.adjustments.exposure_ev = 0.5
            state.crop = CropState(top=0.125, bottom=0.875)

            xmp_path = write_xmp(state)
            xmp = xmp_path.read_text(encoding="utf-8")

        self.assertIn('darktable:operation="exposure"', xmp)
        self.assertIn('darktable:operation="crop"', xmp)
        self.assertIn('darktable:modversion="3"', xmp)

    def test_writes_native_ashift_and_crop_when_rotation_is_needed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-xmp-test-") as tmp:
            state = EditState(source_path=Path(tmp) / "photo.dng")
            state.crop = CropState(top=0.125, bottom=0.875, rotation=1.5)

            xmp_path = write_xmp(state)
            xmp = xmp_path.read_text(encoding="utf-8")

        self.assertIn('darktable:operation="ashift"', xmp)
        self.assertIn('darktable:operation="crop"', xmp)

    def test_writes_native_sigmoid_and_haze_removal_modules(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-xmp-test-") as tmp:
            state = EditState(source_path=Path(tmp) / "photo.dng")
            state.adjustments.sigmoid_contrast = 18.0
            state.adjustments.sigmoid_skew = -8.0
            state.adjustments.dehaze = 22.0

            xmp_path = write_xmp(state)
            xmp = xmp_path.read_text(encoding="utf-8")

        self.assertIn('darktable:operation="sigmoid"', xmp)
        self.assertIn('darktable:modversion="3"', xmp)
        self.assertIn('darktable:operation="hazeremoval"', xmp)

    def test_writes_native_color_tone_detail_modules(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-xmp-test-") as tmp:
            state = EditState(source_path=Path(tmp) / "photo.dng")
            state.adjustments.temperature_kelvin = 6200
            state.adjustments.tint = 8
            state.adjustments.brightness = 6
            state.adjustments.highlights = 20
            state.adjustments.contrast = 12
            state.adjustments.saturation = 5
            state.adjustments.vibrance = 8
            state.adjustments.shadows = 18
            state.adjustments.whites = 8
            state.adjustments.blacks = -4
            state.adjustments.sharpness = 18
            state.adjustments.sharpening_masking = 70
            state.adjustments.noise_reduction = 12
            state.adjustments.clarity = 10
            state.adjustments.vignette = -10

            xmp_path = write_xmp(state)
            xmp = xmp_path.read_text(encoding="utf-8")

        self.assertIn('darktable:operation="temperature"', xmp)
        self.assertIn('darktable:operation="basicadj"', xmp)
        self.assertIn('darktable:operation="colorbalancergb"', xmp)
        self.assertIn('darktable:operation="toneequal"', xmp)
        self.assertIn('darktable:operation="sharpen"', xmp)
        self.assertIn('darktable:operation="denoiseprofile"', xmp)
        self.assertIn('darktable:operation="bilat"', xmp)
        self.assertIn('darktable:operation="vignette"', xmp)

        sharpen_match = re.search(
            r'darktable:operation="sharpen".*?darktable:params="([0-9a-f]+)"',
            xmp,
            re.DOTALL,
        )
        self.assertIsNotNone(sharpen_match)
        _radius, _amount, threshold = struct.unpack("<fff", bytes.fromhex(sharpen_match.group(1)))
        self.assertGreater(threshold, 0.03)

    def test_writes_native_linear_gradient_masked_module(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-xmp-test-") as tmp:
            state = EditState(source_path=Path(tmp) / "photo.dng")
            state.adjustments.vibrance = 2
            state.crop = CropState(top=0.125, bottom=0.875)
            state.local_adjustments.append(
                LocalAdjustmentState(
                    name="sky blue",
                    start_y=0.0,
                    end_y=0.35,
                    invert=True,
                    saturation=40,
                    vibrance=30,
                )
            )

            xmp_path = write_xmp(state)
            xmp = xmp_path.read_text(encoding="utf-8")

        self.assertIn("<darktable:masks_history>", xmp)
        self.assertIn('darktable:mask_type="16"', xmp)
        self.assertIn('darktable:mask_name="sky blue"', xmp)
        self.assertIn('darktable:operation="colorbalancergb"', xmp)
        self.assertIn('darktable:multi_name="sky blue"', xmp)
        self.assertIn('darktable:iop_order_version="0"', xmp)
        self.assertIn('darktable:iop_order_list="rawprepare,0', xmp)
        self.assertIn('colorbalancergb,0,colorbalancergb,1', xmp)
        self.assertTrue(xmp.index("rawprepare,0") < xmp.index("gamma,0"))

        mask_points_match = re.search(r'darktable:mask_points="([0-9a-f]+)"', xmp)
        self.assertIsNotNone(mask_points_match)
        anchor_x, anchor_y, rotation, compression, _steepness, _curvature, state_value = struct.unpack(
            "<ffffffi", bytes.fromhex(mask_points_match.group(1))
        )
        self.assertAlmostEqual(anchor_x, 0.5, places=3)
        self.assertAlmostEqual(anchor_y, 0.25625, places=3)
        self.assertAlmostEqual(rotation, 0.0, places=3)
        self.assertGreater(compression, 0.02)
        self.assertEqual(state_value, 1)

        blend_match = re.search(
            r'darktable:operation="colorbalancergb".*?darktable:multi_name="sky blue".*?darktable:blendop_params="([0-9a-f]+)"',
            xmp,
            re.DOTALL,
        )
        self.assertIsNotNone(blend_match)
        blend = bytes.fromhex(blend_match.group(1))
        self.assertEqual(len(blend), 300)
        self.assertEqual(struct.unpack_from("<I", blend, 0)[0], 3)
        self.assertEqual(struct.unpack_from("<I", blend, 16)[0], 1)

    def test_rotated_crop_keeps_local_masks_in_mcp_finishing(self) -> None:
        state = EditState(source_path=Path("photo.dng"))
        state.crop = CropState(top=0.1, bottom=0.9, rotation=1.0)
        state.local_adjustments.append(LocalAdjustmentState(name="sky", saturation=30))

        self.assertFalse(local_adjustments_are_native_safe(state))

    def test_writes_native_ellipse_path_brush_and_parametric_masks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-xmp-test-") as tmp:
            state = EditState(source_path=Path(tmp) / "photo.dng")
            state.adjustments.exposure_ev = 0.2
            state.adjustments.brightness = 2
            state.local_adjustments.extend([
                LocalAdjustmentState(
                    name="ellipse subject",
                    mask_type="ellipse",
                    center_x=0.5,
                    center_y=0.45,
                    radius_x=0.2,
                    radius_y=0.15,
                    exposure_ev=0.4,
                ),
                LocalAdjustmentState(
                    name="path mountains",
                    mask_type="path",
                    path_points=[[0.2, 0.2], [0.8, 0.2], [0.7, 0.55], [0.25, 0.5]],
                    clarity=15,
                ),
                LocalAdjustmentState(
                    name="brush trees",
                    mask_type="brush",
                    brush_points=[[0.1, 0.7], [0.25, 0.8], [0.4, 0.75]],
                    dehaze=10,
                ),
                LocalAdjustmentState(
                    name="bright tones",
                    mask_type="parametric",
                    parametric_channel="luminance",
                    parametric_low=0.55,
                    parametric_low_soft=0.65,
                    parametric_high_soft=1.0,
                    parametric_high=1.0,
                    highlights=-20,
                ),
            ])

            xmp_path = write_xmp(state)
            xmp = xmp_path.read_text(encoding="utf-8")

        self.assertIn('darktable:mask_type="32"', xmp)
        self.assertIn('darktable:mask_type="2"', xmp)
        self.assertIn('darktable:mask_type="64"', xmp)
        self.assertIn('darktable:operation="exposure"', xmp)
        self.assertIn('darktable:operation="bilat"', xmp)
        self.assertIn('darktable:operation="hazeremoval"', xmp)
        self.assertIn('darktable:operation="basicadj"', xmp)
        self.assertIn('darktable:multi_name="bright tones"', xmp)
        self.assertIn('darktable:iop_order_version="0"', xmp)
        self.assertIn('exposure,0,exposure,1', xmp)
        self.assertIn('basicadj,0,basicadj,1', xmp)

        bright_match = re.search(
            r'darktable:multi_name="bright tones".*?darktable:blendop_params="([0-9a-f]+)"',
            xmp,
            re.DOTALL,
        )
        self.assertIsNotNone(bright_match)
        blend = bytes.fromhex(bright_match.group(1))
        self.assertEqual(struct.unpack_from("<I", blend, 0)[0], 5)
        self.assertEqual(struct.unpack_from("<I", blend, 20)[0], 1)

    def test_writes_native_ashift_for_crop_rotation_without_local_masks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-xmp-test-") as tmp:
            state = EditState(source_path=Path(tmp) / "photo.dng")
            state.crop = CropState(top=0.125, bottom=0.875, rotation=1.2)

            xmp_path = write_xmp(state)
            xmp = xmp_path.read_text(encoding="utf-8")

        self.assertIn('darktable:operation="ashift"', xmp)
        self.assertIn('darktable:modversion="5"', xmp)
        self.assertIn('darktable:operation="crop"', xmp)
