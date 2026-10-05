"""Loading of the regulatory and hedging-policy YAML configuration."""
from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml

_REQUIRED_TOP = ("m_cva", "hedging_disallowance_R", "reporting_currency", "IR", "FX", "CCS", "RCS", "EQ", "CMDTY")


def _read_yaml(path: str | Path | None, default_name: str) -> dict[str, Any]:
    if path is None:
        text = resources.files("sacva.config").joinpath(default_name).read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path or default_name}: expected a mapping at top level")
    return data


def load_params(path: str | Path | None = None) -> dict[str, Any]:
    """Regulatory parameters (defaults to the bundled OSFI CAR Ch. 8 file)."""
    params = _read_yaml(path, "osfi_car_ch8.yaml")
    missing = [k for k in _REQUIRED_TOP if k not in params]
    if missing:
        raise ValueError(f"regulatory params missing keys: {missing}")
    return params


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Hedging-policy and calibration configuration (defaults to the bundled file)."""
    policy = _read_yaml(path, "hedging_policy.yaml")
    for key in ("policies", "assignment", "calibration"):
        if key not in policy:
            raise ValueError(f"hedging policy config missing '{key}'")
    return policy
