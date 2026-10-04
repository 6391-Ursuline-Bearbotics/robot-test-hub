"""Validated, versioned local configuration; no robot endpoints or secrets."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    schema_version: int = 1
    data_dir: str = "data/demo"
    port: int = 6391
    idle_delay: float = 3.0
    freshness: float = 1.0
    chunk_size: int = 256 * 1024
    tick_interval: float = 0.01
    status_interval: float = 0.05
    shutdown_timeout: float = 5.0

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigError("schema_version must be 1; upgrade the hub for newer configuration")
        if not isinstance(self.data_dir, str) or not self.data_dir.strip() or "\x00" in self.data_dir:
            raise ConfigError("data_dir must be a nonempty local directory path")
        for key, low, high in (("port", 1, 65535), ("chunk_size", 1, 16 * 1024 * 1024)):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ConfigError(f"{key} must be an integer between {low} and {high}")
        for key, maximum in (("idle_delay", 86400), ("freshness", 3600), ("tick_interval", 60),
                             ("status_interval", 60), ("shutdown_timeout", 300)):
            value = getattr(self, key)
            # Compare bounds before conversion: arbitrarily large JSON integers
            # must produce actionable validation, not OverflowError in isfinite.
            if (type(value) not in (int, float) or not 0 <= value <= maximum
                    or not math.isfinite(value) or (key != "idle_delay" and value == 0)):
                raise ConfigError(f"{key} must be a finite {'nonnegative' if key == 'idle_delay' else 'positive'} number no greater than {maximum} seconds")

    @classmethod
    def load(cls, path: Path | None = None, **overrides) -> Config:
        values = {}
        if path is not None:
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConfigError(f"Cannot read configuration JSON ({type(exc).__name__}); check --config path and JSON syntax") from None
            if not isinstance(values, dict):
                raise ConfigError("Configuration must be a JSON object")
            if "schema_version" not in values:
                raise ConfigError("Configuration must include schema_version: 1")
            if values.keys() - cls.__dataclass_fields__.keys():
                raise ConfigError("Unknown configuration fields; supported fields: " + ", ".join(cls.__dataclass_fields__))
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)

    def as_dict(self):
        return asdict(self)
