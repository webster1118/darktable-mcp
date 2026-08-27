"""Small, testable adapter around the Darktable command-line renderer.

The MCP server deliberately does not invoke ``subprocess`` directly.  Keeping
the process boundary here makes Windows discovery, command construction and
error reporting consistent for previews and final exports.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .dng_converter import AdobeDngConverter, is_probably_apple_proraw


def find_darktable_cli() -> Optional[Path]:
    """Return the configured Darktable CLI executable, if it is installed."""
    configured = os.environ.get("DARKTABLE_CLI")
    candidates = [
        Path(configured) if configured else None,
        Path(shutil.which("darktable-cli")) if shutil.which("darktable-cli") else None,
        Path(shutil.which("darktable-cli.exe")) if shutil.which("darktable-cli.exe") else None,
        Path.home() / "AppData" / "Local" / "Programs" / "darktable" / "bin" / "darktable-cli.exe",
        Path(r"C:\Program Files\darktable\bin\darktable-cli.exe"),
        Path(r"C:\Program Files (x86)\darktable\bin\darktable-cli.exe"),
    ]
    return next((candidate.resolve() for candidate in candidates if candidate and candidate.is_file()), None)


@dataclass(frozen=True)
class DarktableCli:
    executable: Path
    timeout_seconds: int = 180
    dng_converter: Optional[AdobeDngConverter] = None

    @classmethod
    def discover(cls) -> Optional["DarktableCli"]:
        executable = find_darktable_cli()
        return cls(executable, dng_converter=AdobeDngConverter.discover()) if executable else None

    def build_export_command(
        self,
        source: Path,
        destination: Path,
        xmp: Optional[Path] = None,
        *,
        quality: int = 92,
        max_dimension: Optional[int] = None,
        config_directory: Optional[Path] = None,
    ) -> list[str]:
        """Build the documented darktable-cli invocation for one export."""
        if not 1 <= quality <= 100:
            raise ValueError("quality must be between 1 and 100")
        if max_dimension is not None and max_dimension < 1:
            raise ValueError("max_dimension must be a positive integer")

        source, destination = source.resolve(), destination.resolve()
        # darktable-cli on Windows parses its input/output arguments itself;
        # use slash-separated paths even though subprocess does not require it.
        command = [str(self.executable), source.as_posix()]
        if xmp:
            command.append(xmp.resolve().as_posix())
        command.extend([destination.as_posix(), "--width", str(max_dimension or 0),
                        "--height", str(max_dimension or 0), "--hq", "true"])
        if config_directory:
            command.extend(["--core", "--configdir", config_directory.resolve().as_posix()])
        if destination.suffix.lower() in {".jpg", ".jpeg"}:
            command.extend(["--conf", f"plugins/imageio/format/jpeg/quality={quality}"])
        return command

    def export(
        self,
        source: Path,
        destination: Path,
        xmp: Optional[Path] = None,
        *,
        quality: int = 92,
        max_dimension: Optional[int] = None,
        allow_dng_conversion: bool = True,
        config_directory: Optional[Path] = None,
    ) -> dict:
        """Render one image and return a JSON-serialisable result."""
        config_context = (
            nullcontext(config_directory)
            if config_directory is not None
            else tempfile.TemporaryDirectory(prefix="darktable-mcp-")
        )
        with config_context as config_context_path:
            config_path = Path(config_context_path)
            config_path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="darktable-mcp-converted-dng-") as converted_directory:
                conversion_result = None
                render_source = source
                should_preconvert = allow_dng_conversion and self._should_preconvert_dng(source)
                if should_preconvert:
                    conversion_result = self._convert_dng_for_darktable(source, Path(converted_directory))
                    if conversion_result.get("status") != "ok":
                        return conversion_result
                    render_source = Path(conversion_result["output_path"])

                result = self._export_once(
                    render_source,
                    destination,
                    xmp,
                    quality=quality,
                    max_dimension=max_dimension,
                    config_directory=config_path,
                )
                if (
                    allow_dng_conversion
                    and result.get("status") != "ok"
                    and self._should_retry_with_converted_dng(source, should_preconvert)
                ):
                    conversion_result = self._convert_dng_for_darktable(source, Path(converted_directory))
                    if conversion_result.get("status") == "ok":
                        result = self._export_once(
                            Path(conversion_result["output_path"]),
                            destination,
                            xmp,
                            quality=quality,
                            max_dimension=max_dimension,
                            config_directory=config_path,
                        )
                        result["darktable_first_attempt"] = "failed"

                if conversion_result and conversion_result.get("status") == "ok":
                    result["dng_conversion"] = {
                        "status": "ok",
                        "converted_via": conversion_result.get("converted_via"),
                        "source_path": str(source),
                        "intermediate_storage": "temporary",
                    }
                return result

    def _export_once(
        self,
        source: Path,
        destination: Path,
        xmp: Optional[Path],
        *,
        quality: int,
        max_dimension: Optional[int],
        config_directory: Path,
    ) -> dict:
        command = self.build_export_command(
            source, destination, xmp, quality=quality, max_dimension=max_dimension,
            config_directory=config_directory,
        )
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=self.timeout_seconds, check=False
            )
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": f"darktable-cli timed out after {self.timeout_seconds} seconds.", "command": command}
        except OSError as exc:
            return {"status": "error", "error": f"Could not start darktable-cli: {exc}", "command": command}

        if completed.returncode != 0:
            return {"status": "error", "error": "darktable-cli failed.", "returncode": completed.returncode,
                    "stdout": completed.stdout[-3000:], "stderr": completed.stderr[-3000:], "command": command}
        if not destination.exists():
            return {"status": "error", "error": f"darktable-cli returned success but did not create the expected output file: {destination}",
                    "stdout": completed.stdout[-3000:], "stderr": completed.stderr[-3000:], "command": command}
        return {"status": "ok", "output_path": str(destination), "rendered_via": "darktable-cli",
                "size_bytes": destination.stat().st_size, "stdout": completed.stdout[-1000:], "stderr": completed.stderr[-1000:]}

    def _conversion_mode(self) -> str:
        mode = os.environ.get("DARKTABLE_MCP_DNG_CONVERSION", "auto").strip().lower()
        return mode if mode in {"auto", "always", "never"} else "auto"

    def _should_preconvert_dng(self, source: Path) -> bool:
        if source.suffix.lower() != ".dng" or not self.dng_converter:
            return False
        mode = self._conversion_mode()
        if mode == "never":
            return False
        if mode == "always":
            return True
        return is_probably_apple_proraw(source)

    def _should_retry_with_converted_dng(self, source: Path, already_converted: bool) -> bool:
        return (
            source.suffix.lower() == ".dng"
            and not already_converted
            and self.dng_converter is not None
            and self._conversion_mode() != "never"
        )

    def _convert_dng_for_darktable(self, source: Path, output_directory: Path) -> dict:
        if not self.dng_converter:
            return {
                "status": "error",
                "error": "Adobe DNG Converter is not available for DNG preprocessing.",
            }
        return self.dng_converter.convert(source, output_directory)
