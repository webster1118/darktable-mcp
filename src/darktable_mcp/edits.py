from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
import json
from typing import Optional


@dataclass
class CropState:
    left: float = 0.0    # normalised 0.0–1.0
    top: float = 0.0
    right: float = 1.0
    bottom: float = 1.0
    rotation: float = 0.0  # degrees, clockwise


@dataclass
class AdjustmentState:
    # --- Exposure ---
    exposure_ev: float = 0.0          # stops, -5 to +5
    black_level: float = 0.0          # 0.0–0.5
    highlight_recovery: float = 0.0   # 0.0–1.0
    shadow_lift: float = 0.0          # 0.0–1.0

    # --- White balance ---
    temperature_kelvin: Optional[float] = None  # None = use camera WB
    tint: float = 0.0                           # -100 to +100 (green↔magenta)

    # --- Tone ---
    contrast: float = 0.0     # -100 to +100
    brightness: float = 0.0   # -100 to +100
    highlights: float = 0.0   # -100 to +100
    shadows: float = 0.0      # -100 to +100
    whites: float = 0.0       # -100 to +100
    blacks: float = 0.0       # -100 to +100
    sigmoid_contrast: float = 0.0  # -100 to +100
    sigmoid_skew: float = 0.0      # -100 to +100

    # --- Colour ---
    saturation: float = 0.0   # -100 to +100
    vibrance: float = 0.0     # -100 to +100

    # --- Detail ---
    sharpness: float = 0.0      # 0 to 100
    noise_reduction: float = 0.0  # 0 to 100

    # --- Effects ---
    vignette: float = 0.0   # -100 to +100 (negative = dark edges)
    clarity: float = 0.0    # -100 to +100 (local contrast)
    dehaze: float = 0.0     # 0 to 100


@dataclass
class LocalAdjustmentState:
    name: str
    mask_type: str = "linear_gradient"
    start_x: float = 0.5
    start_y: float = 0.0
    end_x: float = 0.5
    end_y: float = 1.0
    invert: bool = False
    opacity: float = 1.0
    enabled: bool = True
    exposure_ev: float = 0.0
    brightness: float = 0.0
    contrast: float = 0.0
    highlights: float = 0.0
    shadows: float = 0.0
    whites: float = 0.0
    blacks: float = 0.0
    sigmoid_contrast: float = 0.0
    sigmoid_skew: float = 0.0
    saturation: float = 0.0
    vibrance: float = 0.0
    clarity: float = 0.0
    dehaze: float = 0.0

    def has_changes(self) -> bool:
        defaults = LocalAdjustmentState(name=self.name)
        return any([
            self.exposure_ev != defaults.exposure_ev,
            self.brightness != defaults.brightness,
            self.contrast != defaults.contrast,
            self.highlights != defaults.highlights,
            self.shadows != defaults.shadows,
            self.whites != defaults.whites,
            self.blacks != defaults.blacks,
            self.sigmoid_contrast != defaults.sigmoid_contrast,
            self.sigmoid_skew != defaults.sigmoid_skew,
            self.saturation != defaults.saturation,
            self.vibrance != defaults.vibrance,
            self.clarity != defaults.clarity,
            self.dehaze != defaults.dehaze,
        ])


@dataclass
class EditState:
    source_path: Path
    adjustments: AdjustmentState = field(default_factory=AdjustmentState)
    crop: Optional[CropState] = None
    local_adjustments: list[LocalAdjustmentState] = field(default_factory=list)
    output_name: Optional[str] = None  # stem only, no extension

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _sidecar(image_path: Path) -> Path:
        return image_path.with_name(image_path.stem + ".mcp.json")

    @classmethod
    def load(cls, image_path: Path) -> Optional[EditState]:
        sidecar = cls._sidecar(image_path)
        if not sidecar.exists():
            return None
        with open(sidecar) as f:
            data = json.load(f)
        state = cls(source_path=image_path)
        adj_data = data.get("adjustments", {})
        # Only set fields that exist in the dataclass
        valid = {k: v for k, v in adj_data.items() if hasattr(state.adjustments, k)}
        state.adjustments = AdjustmentState(**valid)
        crop_data = data.get("crop")
        state.crop = CropState(**crop_data) if crop_data else None
        local_items = data.get("local_adjustments", [])
        state.local_adjustments = [
            LocalAdjustmentState(**{
                k: v for k, v in item.items()
                if hasattr(LocalAdjustmentState(name=""), k)
            })
            for item in local_items
        ]
        state.output_name = data.get("output_name")
        return state

    def save(self):
        sidecar = self._sidecar(self.source_path)
        data = {
            "adjustments": asdict(self.adjustments),
            "crop": asdict(self.crop) if self.crop else None,
            "local_adjustments": [asdict(item) for item in self.local_adjustments],
            "output_name": self.output_name,
        }
        with open(sidecar, "w") as f:
            json.dump(data, f, indent=2)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def update(self, params: dict):
        for key, value in params.items():
            if hasattr(self.adjustments, key):
                setattr(self.adjustments, key, value)

    def has_changes(self) -> bool:
        defaults = AdjustmentState()
        a = self.adjustments
        return any([
            a.exposure_ev != defaults.exposure_ev,
            a.contrast != defaults.contrast,
            a.sigmoid_contrast != defaults.sigmoid_contrast,
            a.sigmoid_skew != defaults.sigmoid_skew,
            a.saturation != defaults.saturation,
            a.temperature_kelvin is not None,
            a.sharpness != defaults.sharpness,
            a.noise_reduction != defaults.noise_reduction,
            a.vignette != defaults.vignette,
            a.clarity != defaults.clarity,
            a.dehaze != defaults.dehaze,
            self.crop is not None,
            any(item.enabled and item.has_changes() for item in self.local_adjustments),
            self.output_name is not None,
        ])

    def to_dict(self) -> dict:
        return {
            "adjustments": asdict(self.adjustments),
            "crop": asdict(self.crop) if self.crop else None,
            "local_adjustments": [asdict(item) for item in self.local_adjustments],
            "output_name": self.output_name,
        }
