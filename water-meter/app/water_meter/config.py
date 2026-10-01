from dataclasses import dataclass, fields
from pathlib import Path

import yaml

DIGIT_COUNT = 8


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Calibration:
    rotation_deg: float
    meter_crop: tuple[int, int, int, int]
    digit_boxes: list[tuple[int, int, int, int]]
    dial_center: tuple[float, float]
    hub_radius: float
    rim_radius: float
    zero_angle_deg: float


@dataclass(frozen=True)
class Tuning:
    wheel_units: float = 0.1
    needle_units_per_rev: float = 1.0
    consistency_tolerance: float = 0.3
    jitter_gal: float = 0.02
    max_gpm: float = 25.0
    recovery_minutes: float = 10.0
    recovery_agree_gal: float = 1.0
    still_threshold_gal: float = 0.01
    still_window_minutes: float = 15.0
    flow_window_minutes: float = 5.0
    stale_minutes: float = 5.0
    min_digit_confidence: float = 0.6
    sample_interval_s: float = 15.0


@dataclass(frozen=True)
class Config:
    calibration: Calibration
    tuning: Tuning


def _box(value, name: str) -> tuple[int, int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ConfigError(f"{name} must be [x, y, w, h]")
    return tuple(int(v) for v in value)


def _point(value, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f"{name} must be [x, y]")
    return (float(value[0]), float(value[1]))


def _check_keys(section: dict, cls, name: str) -> None:
    known = {f.name for f in fields(cls)}
    unknown = set(section) - known
    if unknown:
        raise ConfigError(f"unknown {name} keys: {sorted(unknown)}")


def _calibration(raw) -> Calibration:
    if not isinstance(raw, dict):
        raise ConfigError("calibration section is required")
    _check_keys(raw, Calibration, "calibration")
    missing = {f.name for f in fields(Calibration)} - set(raw)
    if missing:
        raise ConfigError(f"missing calibration keys: {sorted(missing)}")
    boxes = raw["digit_boxes"]
    if not isinstance(boxes, list) or len(boxes) != DIGIT_COUNT:
        raise ConfigError(f"digit_boxes must list exactly {DIGIT_COUNT} boxes")
    return Calibration(
        rotation_deg=float(raw["rotation_deg"]),
        meter_crop=_box(raw["meter_crop"], "meter_crop"),
        digit_boxes=[_box(b, f"digit_boxes[{i}]") for i, b in enumerate(boxes)],
        dial_center=_point(raw["dial_center"], "dial_center"),
        hub_radius=float(raw["hub_radius"]),
        rim_radius=float(raw["rim_radius"]),
        zero_angle_deg=float(raw["zero_angle_deg"]),
    )


def _tuning(raw) -> Tuning:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("tuning must be a mapping")
    _check_keys(raw, Tuning, "tuning")
    tuning = Tuning(**{k: float(v) for k, v in raw.items()})
    if abs(tuning.needle_units_per_rev - 10 * tuning.wheel_units) > 1e-9:
        raise ConfigError("needle_units_per_rev must equal 10 * wheel_units")
    return tuning


def load_config(path: Path) -> Config:
    try:
        data = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")
    _check_keys(data, Config, "top-level")
    return Config(calibration=_calibration(data.get("calibration")), tuning=_tuning(data.get("tuning")))
