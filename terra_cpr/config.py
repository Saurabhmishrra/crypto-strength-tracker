"""Strict, small TOML configuration loader for the scanner layer."""
from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.9–3.10: this project needs only a tiny TOML subset.
    tomllib = None

from .live import LiveConfig
from .market_structure import MarketStructureConfig
from .relative_strength import RSConfig
from .scanner import ScannerConfig

DEFAULT_INTERVAL_SECONDS = 3600


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"unsupported TOML value: {value!r}") from None


def _read_toml(text: str) -> dict[str, dict[str, Any]]:
    if tomllib is not None:
        return tomllib.loads(text)
    output: dict[str, dict[str, Any]] = {}
    section: dict[str, Any] | None = None
    for line_number, original in enumerate(text.splitlines(), start=1):
        line = original.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if not name or "." in name:
                raise ValueError(f"unsupported TOML section on line {line_number}")
            section = output.setdefault(name, {})
            continue
        if section is None or "=" not in line:
            raise ValueError(f"expected section and key=value on line {line_number}")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key:
            raise ValueError(f"empty TOML key on line {line_number}")
        section[key] = _parse_scalar(value)
    return output


def _known_values(raw: Mapping[str, Any], cls) -> dict[str, Any]:
    known = {field.name for field in fields(cls)}
    unknown = sorted(set(raw).difference(known))
    if unknown:
        raise ValueError(f"unknown {cls.__name__} setting(s): {', '.join(unknown)}")
    return {key: value for key, value in raw.items() if key in known}


def load_scanner_config(path: Path, fallback_interval_seconds: int) -> ScannerConfig:
    raw = _read_toml(path.read_text())
    scanner_values = dict(raw.get("scanner", {}))
    scanner_values.setdefault("interval_seconds", fallback_interval_seconds)
    market_values = _known_values(raw.get("market_structure", {}), MarketStructureConfig)
    rs_values = _known_values(raw.get("relative_strength", {}), RSConfig)
    decision_values = _known_values(raw.get("decision", {}), ScannerConfig)
    decision_values.pop("market", None)
    decision_values.pop("rs", None)
    scanner_values.update(decision_values)
    allowed_scanner = {field.name for field in fields(ScannerConfig)} - {"market", "rs"}
    unknown_scanner = sorted(set(scanner_values).difference(allowed_scanner))
    if unknown_scanner:
        raise ValueError(f"unknown ScannerConfig setting(s): {', '.join(unknown_scanner)}")
    rs_config = replace(RSConfig.for_interval(int(scanner_values["interval_seconds"])), **rs_values)
    config = ScannerConfig(
        **scanner_values,
        market=MarketStructureConfig(**market_values),
        rs=rs_config,
    )
    if config.interval_seconds != fallback_interval_seconds:
        raise ValueError(
            f"config interval_seconds={config.interval_seconds} does not match fixture interval_seconds={fallback_interval_seconds}"
        )
    return config


def load_live_configs(path: Path) -> tuple[ScannerConfig, LiveConfig]:
    """Load both configs for live mode, where the file itself sets the interval.

    A live run has no fixture to agree with, so the bar interval is taken from
    the configuration rather than cross-checked against one.
    """
    raw = _read_toml(path.read_text())
    interval = int(raw.get("scanner", {}).get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
    live_values = _known_values(raw.get("live", {}), LiveConfig)
    return load_scanner_config(path, interval), LiveConfig(**live_values)
