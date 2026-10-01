from pathlib import Path

import pytest
import yaml

from water_meter.config import ConfigError, Tuning, load_config

APP_DIR = Path(__file__).resolve().parent.parent


def calibration(boxes=8):
    return {
        "rotation_deg": 12.0,
        "meter_crop": [780, 550, 500, 500],
        "digit_boxes": [[10 + 32 * i, 20, 22, 32] for i in range(boxes)],
        "dial_center": [290, 330],
        "hub_radius": 12,
        "rim_radius": 90,
        "zero_angle_deg": 0,
    }


def write(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_load_valid_config(tmp_path):
    cfg = load_config(write(tmp_path, {"calibration": calibration(), "tuning": {"max_gpm": 25}}))
    assert len(cfg.calibration.digit_boxes) == 8
    assert cfg.calibration.digit_boxes[1] == (42, 20, 22, 32)
    assert cfg.calibration.dial_center == (290.0, 330.0)
    assert cfg.tuning.wheel_units == 0.1
    assert cfg.tuning.max_gpm == 25


def test_missing_tuning_uses_defaults(tmp_path):
    cfg = load_config(write(tmp_path, {"calibration": calibration(), "tuning": {}}))
    assert cfg.tuning == Tuning()


def test_wrong_box_count_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, {"calibration": calibration(boxes=7)}))


def test_unit_mismatch_rejected(tmp_path):
    data = {"calibration": calibration(), "tuning": {"wheel_units": 0.1, "needle_units_per_rev": 1.0}}
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, {"calibration": calibration(), "tuning": {"max_gmp": 30}}))


def test_repo_config_loads():
    cfg = load_config(APP_DIR / "config.yaml")
    assert len(cfg.calibration.digit_boxes) == 8
