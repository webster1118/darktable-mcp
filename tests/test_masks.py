from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from darktable_mcp.edits import EditState, LocalAdjustmentState
from darktable_mcp.processor import ImageProcessor


class MaskTests(unittest.TestCase):
    def test_local_gradient_adjustment_round_trips_through_sidecar(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-mask-test-") as tmp:
            image_path = Path(tmp) / "photo.dng"
            state = EditState(source_path=image_path)
            state.local_adjustments.append(
                LocalAdjustmentState(
                    name="sky",
                    start_y=0.0,
                    end_y=0.5,
                    invert=True,
                    exposure_ev=-0.3,
                    clarity=10.0,
                    dehaze=12.0,
                )
            )
            state.save()

            loaded = EditState.load(image_path)

        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.local_adjustments), 1)
        self.assertEqual(loaded.local_adjustments[0].name, "sky")
        self.assertEqual(loaded.local_adjustments[0].exposure_ev, -0.3)
        self.assertEqual(loaded.local_adjustments[0].clarity, 10.0)
        self.assertEqual(loaded.local_adjustments[0].dehaze, 12.0)

    def test_linear_gradient_mask_applies_strongest_at_end_point(self) -> None:
        img = Image.new("RGB", (8, 8), color=(100, 100, 100))
        local = LocalAdjustmentState(
            name="foreground",
            start_y=0.0,
            end_y=1.0,
            exposure_ev=1.0,
        )

        out = ImageProcessor(Path("preview.jpg"))._apply_local_adjustments(img, [local])
        arr = np.array(out)

        self.assertLess(arr[0, 0, 0], arr[-1, 0, 0])
        self.assertGreater(arr[-1, 0, 0], 180)
