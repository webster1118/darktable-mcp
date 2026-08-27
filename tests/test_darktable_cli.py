from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from darktable_mcp.darktable_cli import DarktableCli
from darktable_mcp.dng_converter import AdobeDngConverter, is_probably_apple_proraw


class FakeDngConverter:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def convert(self, source: Path, output_directory: Path) -> dict:
        self.calls.append((source, output_directory))
        output_directory.mkdir(parents=True, exist_ok=True)
        converted = output_directory / source.with_suffix(".dng").name
        converted.write_bytes(b"converted dng")
        return {
            "status": "ok",
            "output_path": str(converted),
            "converted_via": "fake-adobe-dng-converter",
        }


class DarktableCliCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path(self._testMethodName)

    def test_builds_jpeg_command_with_xmp_and_resize(self) -> None:
        cli = DarktableCli(self.tmp_path / "darktable-cli.exe")
        command = cli.build_export_command(self.tmp_path / "photo.DNG", self.tmp_path / "result.jpg", self.tmp_path / "edit.xmp", quality=87, max_dimension=1600)

        self.assertEqual(command[1:4], [(self.tmp_path / "photo.DNG").resolve().as_posix(), (self.tmp_path / "edit.xmp").resolve().as_posix(), (self.tmp_path / "result.jpg").resolve().as_posix()])
        self.assertEqual(command[-2:], ["--conf", "plugins/imageio/format/jpeg/quality=87"])
        self.assertEqual(command[4:10], ["--width", "1600", "--height", "1600", "--hq", "true"])

    def test_png_command_does_not_apply_a_jpeg_setting(self) -> None:
        cli = DarktableCli(self.tmp_path / "darktable-cli.exe")
        command = cli.build_export_command(self.tmp_path / "photo.DNG", self.tmp_path / "result.png")

        self.assertNotIn("plugins/imageio/format/jpeg/quality=92", command)

    def test_uses_a_supplied_isolated_darktable_config(self) -> None:
        cli = DarktableCli(self.tmp_path / "darktable-cli.exe")
        config = self.tmp_path / "darktable-config"
        command = cli.build_export_command(
            self.tmp_path / "photo.DNG", self.tmp_path / "result.jpg", config_directory=config
        )

        self.assertIn("--core", command)
        self.assertEqual(command[command.index("--configdir") + 1], config.resolve().as_posix())

    def test_rejects_invalid_export_values(self) -> None:
        cli = DarktableCli(self.tmp_path / "darktable-cli.exe")
        for quality, maximum in [(0, None), (101, None), (92, 0)]:
            with self.subTest(quality=quality, maximum=maximum):
                with self.assertRaises(ValueError):
                    cli.build_export_command(self.tmp_path / "photo.DNG", self.tmp_path / "result.jpg", quality=quality, max_dimension=maximum)

    def test_detects_probable_apple_proraw_dng(self) -> None:
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-proraw-test-") as tmp:
            path = Path(tmp) / "iphone.dng"
            path.write_bytes(b"II*\x00Apple iPhone ProRAW profile")

            self.assertTrue(is_probably_apple_proraw(path))

    def test_builds_adobe_dng_converter_command(self) -> None:
        converter = AdobeDngConverter(self.tmp_path / "Adobe DNG Converter.exe")
        command = converter.build_convert_command(self.tmp_path / "photo.dng", self.tmp_path / "out")

        self.assertEqual(command[0], str(self.tmp_path / "Adobe DNG Converter.exe"))
        self.assertIn("-c", command)
        self.assertIn("-p1", command)
        self.assertEqual(command[command.index("-d") + 1], str((self.tmp_path / "out").resolve()))
        self.assertEqual(command[-1], str((self.tmp_path / "photo.dng").resolve()))

    def test_forced_dng_conversion_feeds_converted_file_to_darktable(self) -> None:
        converter = FakeDngConverter()
        cli = DarktableCli(self.tmp_path / "darktable-cli.exe", dng_converter=converter)
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-convert-flow-") as tmp:
            source = Path(tmp) / "photo.dng"
            destination = Path(tmp) / "out.jpg"
            source.write_bytes(b"regular dng")

            def fake_export_once(_cli, render_source, destination, *args, **kwargs):
                destination.write_bytes(b"jpg")
                return {"status": "ok", "output_path": str(destination), "rendered_via": "darktable-cli", "size_bytes": 3}

            with patch.dict(os.environ, {"DARKTABLE_MCP_DNG_CONVERSION": "always"}):
                with patch.object(DarktableCli, "_export_once", autospec=True, side_effect=fake_export_once) as export_once:
                    result = cli.export(source, destination)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["dng_conversion"]["converted_via"], "fake-adobe-dng-converter")
        self.assertEqual(len(converter.calls), 1)
        self.assertNotEqual(Path(export_once.call_args.args[1]), source)

    def test_retries_failed_dng_export_with_adobe_conversion(self) -> None:
        converter = FakeDngConverter()
        cli = DarktableCli(self.tmp_path / "darktable-cli.exe", dng_converter=converter)
        with tempfile.TemporaryDirectory(prefix="darktable-mcp-convert-retry-") as tmp:
            source = Path(tmp) / "photo.dng"
            destination = Path(tmp) / "out.jpg"
            source.write_bytes(b"regular dng")
            calls: list[Path] = []

            def fake_export_once(_cli, render_source, destination, *args, **kwargs):
                calls.append(Path(render_source))
                if len(calls) == 1:
                    return {"status": "error", "error": "darktable-cli failed"}
                destination.write_bytes(b"jpg")
                return {"status": "ok", "output_path": str(destination), "rendered_via": "darktable-cli", "size_bytes": 3}

            with patch.dict(os.environ, {"DARKTABLE_MCP_DNG_CONVERSION": "auto"}):
                with patch.object(DarktableCli, "_export_once", autospec=True, side_effect=fake_export_once):
                    result = cli.export(source, destination)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["darktable_first_attempt"], "failed")
        self.assertEqual(len(converter.calls), 1)
        self.assertEqual(calls[0], source)
        self.assertNotEqual(calls[1], source)


class DarktableCliFixtureTests(unittest.TestCase):
    def test_exports_fixture_dng_when_darktable_is_available(self) -> None:
        if os.environ.get("DARKTABLE_MCP_RUN_SMOKE") != "1":
            self.skipTest("set DARKTABLE_MCP_RUN_SMOKE=1 to run the slow Darktable DNG export test")

        fixture = Path("tests/content/IMG_9491260826.dng")
        if not fixture.exists():
            self.skipTest("fixture DNG is not present")

        cli = DarktableCli.discover()
        if not cli:
            self.skipTest("darktable-cli is not installed")

        with tempfile.TemporaryDirectory(prefix="darktable-mcp-test-") as tmp:
            output = Path(tmp) / "fixture.jpg"
            result = cli.export(fixture, output, quality=80, max_dimension=512)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["rendered_via"], "darktable-cli")
        self.assertGreater(result["size_bytes"], 0)
