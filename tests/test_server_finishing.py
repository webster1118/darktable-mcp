from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from darktable_mcp.edits import CropState, EditState, LocalAdjustmentState
from darktable_mcp.server import _apply_vacation_feedback, _finish_darktable_render, _needs_mcp_finishing


class DarktableFinishingTests(unittest.TestCase):
    def test_skips_mcp_crop_when_darktable_native_crop_is_safe(self) -> None:
        state = EditState(source_path=Path("photo.dng"))
        state.crop = CropState(left=0.0, top=0.125, right=1.0, bottom=0.875)

        self.assertFalse(_needs_mcp_finishing(state))

    def test_applies_rotated_crop_after_darktable_render(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-finish-test-") as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "render.jpg"
            output = tmp_path / "finished.jpg"
            Image.new("RGB", (1600, 1200), color=(128, 128, 128)).save(source)
            state = EditState(source_path=tmp_path / "photo.dng")
            state.crop = CropState(left=0.0, top=0.125, right=1.0, bottom=0.875, rotation=0.5)

            _finish_darktable_render(source, state, output, format_name="jpeg", quality=90)

            with Image.open(output) as img:
                self.assertLess(abs(img.size[0] / img.size[1] - 16 / 9), 0.01)

    def test_finishing_applies_local_adjustments_before_crop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-finish-test-") as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "render.jpg"
            output = tmp_path / "finished.jpg"
            Image.new("RGB", (200, 100), color=(80, 80, 80)).save(source)
            state = EditState(source_path=tmp_path / "photo.dng")
            state.local_adjustments.append(
                LocalAdjustmentState(name="bottom", start_y=0.0, end_y=1.0, exposure_ev=1.0)
            )
            state.crop = CropState(left=0.0, top=0.5, right=1.0, bottom=1.0)

            _finish_darktable_render(source, state, output, format_name="jpeg", quality=90)

            with Image.open(output) as img:
                arr = np.array(img)
            self.assertGreater(arr[..., 0].mean(), 130)

    def test_vacation_feedback_brightens_dark_preview_metrics(self) -> None:
        state = EditState(source_path=Path("photo.dng"))
        state.adjustments.exposure_ev = 0.65
        state.adjustments.shadows = 28.0
        state.adjustments.brightness = 6.0

        changes = _apply_vacation_feedback(
            state,
            {
                "mean": 82.0,
                "p05": 6.0,
                "p50": 70.0,
                "p95": 190.0,
                "shadow_clip_pct": 0.2,
                "highlight_clip_pct": 0.0,
                "contrast_span": 184.0,
            },
        )

        self.assertIn("raised exposure", changes)
        self.assertIn("lifted shadows", changes)
        self.assertGreater(state.adjustments.exposure_ev, 0.65)
        self.assertGreater(state.adjustments.shadows, 28.0)

    def test_vacation_feedback_adds_dehaze_to_flat_preview_metrics(self) -> None:
        state = EditState(source_path=Path("photo.dng"))
        state.adjustments.dehaze = 18.0
        state.adjustments.clarity = 8.0

        changes = _apply_vacation_feedback(
            state,
            {
                "mean": 122.0,
                "p05": 62.0,
                "p50": 124.0,
                "p95": 170.0,
                "shadow_clip_pct": 0.0,
                "highlight_clip_pct": 0.0,
                "contrast_span": 108.0,
            },
        )

        self.assertIn("added dehaze", changes)
        self.assertIn("added local contrast", changes)
        self.assertGreater(state.adjustments.dehaze, 18.0)
        self.assertGreater(state.adjustments.clarity, 8.0)
