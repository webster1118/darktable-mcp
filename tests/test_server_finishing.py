from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from darktable_mcp.edits import CropState, EditState, LocalAdjustmentState
from darktable_mcp.server import (
    _finish_darktable_render,
    _needs_mcp_finishing,
    apply_edit_recipe,
    get_current_edits,
)


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

    def test_apply_edit_recipe_sets_generic_adjustments_and_crop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-recipe-test-") as tmp:
            image = Path(tmp) / "photo.dng"
            image.write_bytes(b"placeholder")

            result = apply_edit_recipe(
                str(image),
                adjustments={"exposure_ev": 0.7, "saturation": 12.0, "clarity": 8.0},
                crop={"top": 0.125, "bottom": 0.875},
                output_name="sunny-natural",
            )

            current = get_current_edits(str(image))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(current["edits"]["adjustments"]["exposure_ev"], 0.7)
        self.assertEqual(current["edits"]["adjustments"]["saturation"], 12.0)
        self.assertEqual(current["edits"]["crop"]["top"], 0.125)
        self.assertEqual(current["edits"]["output_name"], "sunny-natural")

    def test_apply_edit_recipe_rejects_unknown_adjustments(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-recipe-test-") as tmp:
            image = Path(tmp) / "photo.dng"
            image.write_bytes(b"placeholder")

            result = apply_edit_recipe(str(image), adjustments={"magic": 1.0})

        self.assertEqual(result["status"], "error")
        self.assertIn("Unknown adjustment", result["error"])
