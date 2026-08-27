"""Adobe DNG Converter integration for DNGs Darktable cannot read directly."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def find_adobe_dng_converter() -> Optional[Path]:
    """Return the configured Adobe DNG Converter executable, if installed."""
    configured = os.environ.get("ADOBE_DNG_CONVERTER")
    candidates = [
        Path(configured) if configured else None,
        Path(shutil.which("Adobe DNG Converter.exe")) if shutil.which("Adobe DNG Converter.exe") else None,
        Path(r"C:\Program Files\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe"),
        Path(r"C:\Program Files (x86)\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe"),
    ]
    return next((candidate.resolve() for candidate in candidates if candidate and candidate.is_file()), None)


def is_probably_apple_proraw(path: Path) -> bool:
    """Best-effort Apple ProRAW detection without requiring exiftool.

    Apple ProRAW files are DNG/TIFF containers that usually contain plain-text
    maker/model/profile strings near the front of the file. This heuristic is
    intentionally conservative; a missed detection is handled by the export
    retry path if Darktable rejects the DNG.
    """
    if path.suffix.lower() != ".dng":
        return False
    try:
        sample = path.read_bytes()[:2_000_000].lower()
    except OSError:
        return False
    return b"apple" in sample and (b"iphone" in sample or b"proraw" in sample)


@dataclass(frozen=True)
class AdobeDngConverter:
    executable: Path
    timeout_seconds: int = 240

    @classmethod
    def discover(cls) -> Optional["AdobeDngConverter"]:
        executable = find_adobe_dng_converter()
        return cls(executable) if executable else None

    def build_convert_command(
        self,
        source: Path,
        output_directory: Path,
        *,
        compressed: bool = True,
        preview_size: int = 1,
    ) -> list[str]:
        """Build an Adobe DNG Converter command for one source file."""
        if preview_size not in {0, 1, 2}:
            raise ValueError("preview_size must be 0, 1, or 2")
        source = source.resolve()
        output_directory = output_directory.resolve()
        compression_flag = "-c" if compressed else "-u"
        return [
            str(self.executable),
            compression_flag,
            f"-p{preview_size}",
            "-d",
            str(output_directory),
            str(source),
        ]

    def convert(self, source: Path, output_directory: Path) -> dict:
        """Convert *source* into *output_directory* and return the output DNG."""
        source = source.resolve()
        output_directory = output_directory.resolve()
        output_directory.mkdir(parents=True, exist_ok=True)
        expected = output_directory / source.with_suffix(".dng").name
        if expected.exists():
            expected.unlink()

        command = self.build_convert_command(source, output_directory)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "error": f"Adobe DNG Converter timed out after {self.timeout_seconds} seconds.",
                "command": command,
            }
        except OSError as exc:
            return {
                "status": "error",
                "error": f"Could not start Adobe DNG Converter: {exc}",
                "command": command,
            }

        # Some Adobe DNG Converter versions return before the file is fully
        # visible on disk. Wait until the expected output exists and its size
        # remains stable for two consecutive checks.
        deadline = time.monotonic() + min(60, self.timeout_seconds)
        last_size = -1
        stable_checks = 0
        while time.monotonic() < deadline:
            if expected.exists():
                size = expected.stat().st_size
                if size > 0 and size == last_size:
                    stable_checks += 1
                    if stable_checks >= 2:
                        break
                else:
                    stable_checks = 0
                last_size = size
            time.sleep(0.5)

        if completed.returncode != 0:
            return {
                "status": "error",
                "error": "Adobe DNG Converter failed.",
                "returncode": completed.returncode,
                "stdout": completed.stdout[-3000:],
                "stderr": completed.stderr[-3000:],
                "command": command,
            }
        if not expected.exists():
            return {
                "status": "error",
                "error": f"Adobe DNG Converter did not create the expected output file: {expected}",
                "stdout": completed.stdout[-3000:],
                "stderr": completed.stderr[-3000:],
                "command": command,
            }
        return {
            "status": "ok",
            "output_path": str(expected),
            "converted_via": "adobe-dng-converter",
            "size_bytes": expected.stat().st_size,
            "stdout": completed.stdout[-1000:],
            "stderr": completed.stderr[-1000:],
            "command": command,
        }
