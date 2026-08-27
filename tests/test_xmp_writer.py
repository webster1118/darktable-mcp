from pathlib import Path
import tempfile
import unittest

from darktable_mcp.edits import CropState, EditState
from darktable_mcp.xmp_writer import write_xmp


class XmpWriterTests(unittest.TestCase):
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

    def test_skips_native_crop_when_rotation_is_needed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-xmp-test-") as tmp:
            state = EditState(source_path=Path(tmp) / "photo.dng")
            state.crop = CropState(top=0.125, bottom=0.875, rotation=1.5)

            xmp_path = write_xmp(state)
            xmp = xmp_path.read_text(encoding="utf-8")

        self.assertNotIn('darktable:operation="crop"', xmp)

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
