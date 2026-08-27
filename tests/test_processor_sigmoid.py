import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from darktable_mcp.edits import AdjustmentState, EditState
from darktable_mcp.processor import ImageProcessor


class ProcessorSigmoidTests(unittest.TestCase):
    def test_sigmoid_contrast_keeps_black_and_white_anchors(self) -> None:
        gradient = np.tile(np.arange(256, dtype=np.uint8), (16, 1))
        rgb = np.dstack([gradient, gradient, gradient])
        img = Image.fromarray(rgb)
        state = EditState(source_path=Path("gradient.jpg"))
        state.adjustments = AdjustmentState(sigmoid_contrast=35.0)

        out = ImageProcessor(state.source_path)._apply_post_raw(img, state.adjustments)
        arr = np.array(out)

        self.assertLessEqual(arr[0, 0, 0], 2)
        self.assertGreaterEqual(arr[0, -1, 0], 253)
        self.assertLess(arr[0, 96, 0], 96)
        self.assertGreater(arr[0, 160, 0], 160)

    def test_dehaze_expands_flat_tonal_range(self) -> None:
        gradient = np.tile(np.linspace(95, 160, 256, dtype=np.uint8), (64, 1))
        rgb = np.dstack([gradient, gradient, gradient])
        img = Image.fromarray(rgb)
        state = EditState(source_path=Path("hazy.jpg"))
        state.adjustments = AdjustmentState(dehaze=45.0)

        out = ImageProcessor(state.source_path)._apply_post_raw(img, state.adjustments)
        before = np.array(img, dtype=np.float32)
        after = np.array(out, dtype=np.float32)

        self.assertGreater(after.max() - after.min(), before.max() - before.min())
