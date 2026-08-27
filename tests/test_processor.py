from pathlib import Path
import unittest

from darktable_mcp.processor import ImageProcessor


class ImageProcessorFixtureTests(unittest.TestCase):
    def test_reads_fixture_dng_dimensions_without_metadata_error(self) -> None:
        fixture = Path("tests/content/IMG_9491260826.dng")
        if not fixture.exists():
            self.skipTest("fixture DNG is not present")

        info = ImageProcessor(fixture).get_info()

        self.assertEqual(info["type"], "raw")
        self.assertEqual(info["width"], 8064)
        self.assertEqual(info["height"], 6048)
        self.assertNotIn("read_error", info)
