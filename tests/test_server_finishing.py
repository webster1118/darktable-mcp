from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from darktable_mcp.edits import CropState, EditState, LocalAdjustmentState
from darktable_mcp.server import (
    _cleanup_generated_files,
    _diagnostic_warnings,
    _export_state_to_path,
    _metric_delta,
    _reference_match_suggestions,
    _regional_metric_delta,
    _render_preview_to_file,
    _finish_darktable_render,
    _needs_mcp_finishing,
    apply_starting_point,
    cleanup_temporary_files,
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

    def test_preview_render_overwrites_fixed_raw_preview_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-preview-test-") as tmp:
            tmp_path = Path(tmp)
            image = tmp_path / "photo.dng"
            output = tmp_path / "preview.jpg"
            image.write_bytes(b"placeholder dng")
            output.write_bytes(b"stale preview")
            state = EditState(source_path=image)

            def fake_export(src, dst, xmp, quality, max_dimension=None, **kwargs):
                self.assertEqual(src, image)
                self.assertFalse(dst.exists(), "preview target should be removed before Darktable renders")
                self.assertIsNotNone(xmp)
                dst.write_bytes(b"fresh preview")
                return {
                    "status": "ok",
                    "output_path": str(dst),
                    "rendered_via": "darktable-cli",
                    "size_bytes": dst.stat().st_size,
                }

            with patch("darktable_mcp.server.DARKTABLE", object()):
                with patch("darktable_mcp.server._export_via_darktable_cli", side_effect=fake_export):
                    result = _render_preview_to_file(image, state, output, max_dimension=400)
            rendered_bytes = output.read_bytes()

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(rendered_bytes, b"fresh preview")

    def test_metric_delta_and_warnings_flag_darker_uncropped_edits(self) -> None:
        current = {
            "mean": 88.0,
            "p05": 18.0,
            "p50": 94.0,
            "p95": 142.0,
            "saturation_mean": 0.25,
            "contrast_span": 124.0,
            "detail_gradient_mean": 8.0,
            "detail_gradient_p95": 32.0,
            "detail_laplacian_var": 210.0,
        }
        baseline = {
            "mean": 100.0,
            "p05": 17.0,
            "p50": 105.0,
            "p95": 165.0,
            "saturation_mean": 0.24,
            "contrast_span": 148.0,
            "detail_gradient_mean": 12.0,
            "detail_gradient_p95": 40.0,
            "detail_laplacian_var": 360.0,
        }
        state = EditState(source_path=Path("photo.dng"))

        delta = _metric_delta(current, baseline)
        warnings = _diagnostic_warnings(current, baseline, state)

        self.assertEqual(delta["mean"], -12.0)
        self.assertEqual(delta["detail_gradient_mean"], -4.0)
        self.assertTrue(any("darker" in warning for warning in warnings))
        self.assertTrue(any("edge/detail" in warning for warning in warnings))
        self.assertTrue(any("No crop" in warning for warning in warnings))
        self.assertTrue(any("No local adjustments" in warning for warning in warnings))

    def test_fast_warning_flags_disabled_sharpening(self) -> None:
        warnings = _diagnostic_warnings({}, fast=True)

        self.assertTrue(any("Fast render mode disables sharpening" in warning for warning in warnings))

    def test_regional_deltas_flag_local_mismatches(self) -> None:
        current = {
            "regions": {
                "sky_top": {
                    "mean": 150.0,
                    "p50": 150.0,
                    "p95": 180.0,
                    "saturation_mean": 0.12,
                    "colorfulness": 12.0,
                    "contrast_span": 30.0,
                    "detail_gradient_mean": 2.0,
                    "detail_gradient_p95": 5.0,
                    "detail_laplacian_var": 20.0,
                }
            }
        }
        reference = {
            "regions": {
                "sky_top": {
                    "mean": 160.0,
                    "p50": 160.0,
                    "p95": 190.0,
                    "saturation_mean": 0.18,
                    "colorfulness": 20.0,
                    "contrast_span": 32.0,
                    "detail_gradient_mean": 4.0,
                    "detail_gradient_p95": 7.0,
                    "detail_laplacian_var": 30.0,
                }
            }
        }

        delta = _regional_metric_delta(current, reference)
        warnings = _diagnostic_warnings(current, reference)

        self.assertEqual(delta["sky_top"]["mean"], -10.0)
        self.assertEqual(delta["sky_top"]["saturation_mean"], -0.06)
        self.assertTrue(any("sky top region is darker" in warning for warning in warnings))
        self.assertTrue(any("sky top region is less saturated" in warning for warning in warnings))
        self.assertTrue(any("sky top region is less colorful" in warning for warning in warnings))
        self.assertTrue(any("sky top region has less measured detail" in warning for warning in warnings))

    def test_reference_match_suggestions_translate_deltas_to_actions(self) -> None:
        delta = {
            "mean": 0.5,
            "p50": -6.0,
            "p05": 5.0,
            "saturation_mean": -0.03,
            "colorfulness": -6.0,
            "detail_gradient_mean": -1.0,
        }
        regional_delta = {
            "lake_lower_mid": {
                "mean": -9.0,
                "p50": -12.0,
                "saturation_mean": -0.04,
                "colorfulness": -7.0,
                "detail_gradient_mean": 0.0,
            },
            "mountains_mid": {
                "mean": 0.0,
                "p50": 0.0,
                "saturation_mean": 0.0,
                "colorfulness": 0.0,
                "detail_gradient_mean": -2.0,
            },
        }

        suggestions = _reference_match_suggestions(delta, regional_delta)

        self.assertTrue(any("Overall color" in suggestion for suggestion in suggestions))
        self.assertTrue(any("midtones are low" in suggestion for suggestion in suggestions))
        self.assertTrue(any("Lake is darker" in suggestion for suggestion in suggestions))
        self.assertTrue(any("Lake color" in suggestion for suggestion in suggestions))
        self.assertTrue(any("Mountain detail" in suggestion for suggestion in suggestions))

    def test_raw_render_refuses_to_run_without_explicit_xmp(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-xmp-fail-test-") as tmp:
            tmp_path = Path(tmp)
            image = tmp_path / "photo.dng"
            output = tmp_path / "preview.jpg"
            image.write_bytes(b"placeholder dng")
            state = EditState(source_path=image)

            with patch("darktable_mcp.server.write_xmp", side_effect=PermissionError("read only")):
                with patch("darktable_mcp.server.DARKTABLE", object()):
                    result = _export_state_to_path(path=image, state=state, out_path=output)

        self.assertEqual(result["status"], "error")
        self.assertIn("Refusing to call darktable-cli without an explicit XMP", result["error"])

    def test_apply_starting_point_sets_proraw_normalization_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-start-test-") as tmp:
            image = Path(tmp) / "photo.dng"
            image.write_bytes(b"placeholder dng")

            result = apply_starting_point(str(image))
            current = get_current_edits(str(image))

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["profile"], "apple_proraw_natural")
        self.assertAlmostEqual(current["edits"]["adjustments"]["exposure_ev"], 0.6)
        self.assertEqual(current["edits"]["adjustments"]["sharpness"], 70)
        self.assertEqual(current["edits"]["adjustments"]["sharpening_masking"], 70)

    def test_apply_starting_point_skips_existing_edits_by_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-start-test-") as tmp:
            image = Path(tmp) / "photo.dng"
            image.write_bytes(b"placeholder dng")
            state = EditState(source_path=image)
            state.adjustments.exposure_ev = 1.2
            state.save()

            result = apply_starting_point(str(image))

        self.assertEqual(result["status"], "skipped")
        self.assertAlmostEqual(result["edits"]["adjustments"]["exposure_ev"], 1.2)

    def test_cleanup_generated_files_removes_only_mcp_artifacts_for_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-clean-test-") as tmp:
            tmp_path = Path(tmp)
            image = tmp_path / "photo.dng"
            other_image = tmp_path / "other.dng"
            final_export = tmp_path / "photo.jpg"
            preview_copy = tmp_path / "photo__preview.jpg"
            preview_dir = tmp_path / ".darktable-mcp-preview"
            preview_dir.mkdir()
            preview = preview_dir / "photo__analysis.jpg"
            other_preview = preview_dir / "other__analysis.jpg"
            for file in [image, other_image, final_export, preview_copy, preview, other_preview]:
                file.write_bytes(b"x")

            result = _cleanup_generated_files(image)

            self.assertEqual(result["errors"], [])
            self.assertFalse(preview_copy.exists())
            self.assertFalse(preview.exists())
            self.assertTrue(image.exists())
            self.assertTrue(final_export.exists())
            self.assertTrue(other_preview.exists())

    def test_cleanup_temporary_files_reports_missing_image(self) -> None:
        result = cleanup_temporary_files("missing.dng")

        self.assertEqual(result["status"], "error")
