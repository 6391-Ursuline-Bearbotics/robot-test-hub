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
    io_timeout: float = 5.0
    discovery_page_size: int = 100
    retry_initial: float = 1.0
    retry_max: float = 30.0
    retry_limit: int = 8
    eta_window: float = 15.0
    eta_minimum: float = 2.0
    historical_max_age: float = 300.0
    log_export_destination: str | None = None
    log_export_interval: float = 60.0
    analytics_enabled: bool = False
    analytics_share_summaries: bool = False
    analytics_pause_when_enabled: bool = True
    analytics_threads: int = 1
    analytics_memory_mb: int = 512
    analytics_interval: float = 2.0
    backup_destination: str | None = None
    backup_interval: float = 3600.0
    backup_retry: float = 60.0

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigError("schema_version must be 1; upgrade the hub for newer configuration")
        if not isinstance(self.data_dir, str) or not self.data_dir.strip() or "\x00" in self.data_dir:
            raise ConfigError("data_dir must be a nonempty local directory path")
        if self.backup_destination is not None:
            if not isinstance(self.backup_destination, str) or not self.backup_destination.strip() or "\x00" in self.backup_destination:
                raise ConfigError("backup_destination must be null or an explicit directory path")
            source_root, backup_root = Path(self.data_dir).resolve(), Path(self.backup_destination).resolve()
            if source_root == backup_root or source_root.is_relative_to(backup_root) or backup_root.is_relative_to(source_root):
                raise ConfigError("backup_destination must be disjoint from data_dir")
        if self.log_export_destination is not None:
            if not isinstance(self.log_export_destination, str) or not self.log_export_destination.strip() or "\x00" in self.log_export_destination:
                raise ConfigError("log_export_destination must be null or an explicit directory path")
            source_root, export_root = Path(self.data_dir).resolve(), Path(self.log_export_destination).resolve()
            if source_root == export_root or source_root.is_relative_to(export_root) or export_root.is_relative_to(source_root):
                raise ConfigError("log_export_destination must be disjoint from data_dir")
        for key in ("analytics_enabled","analytics_share_summaries","analytics_pause_when_enabled"):
            if type(getattr(self,key)) is not bool:
                raise ConfigError(key+" must be boolean")
        for key, low, high in (("analytics_threads",1,4),("analytics_memory_mb",128,4096),("port", 1, 65535), ("chunk_size", 1, 16 * 1024 * 1024), ("discovery_page_size", 1, 1000), ("retry_limit", 1, 100)):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ConfigError(f"{key} must be an integer between {low} and {high}")
        for key, maximum in (("analytics_interval", 3600), ("idle_delay", 86400), ("freshness", 3600), ("tick_interval", 60),
                             ("status_interval", 60), ("shutdown_timeout", 300), ("io_timeout", 300), ("retry_initial", 300), ("retry_max", 3600), ("eta_window", 3600), ("eta_minimum", 3600), ("historical_max_age", 86400), ("log_export_interval", 86400), ("backup_interval", 86400), ("backup_retry", 86400)):
            value = getattr(self, key)
            # Compare bounds before conversion: arbitrarily large JSON integers
            # must produce actionable validation, not OverflowError in isfinite.
            if (type(value) not in (int, float) or not 0 <= value <= maximum
                    or not math.isfinite(value) or (key != "idle_delay" and value == 0)):
                raise ConfigError(f"{key} must be a finite {'nonnegative' if key == 'idle_delay' else 'positive'} number no greater than {maximum} seconds")

        if self.retry_max < self.retry_initial:
            raise ConfigError("retry_max must be at least retry_initial")
        if self.eta_minimum > self.eta_window:
            raise ConfigError("eta_minimum must be no greater than eta_window")

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
