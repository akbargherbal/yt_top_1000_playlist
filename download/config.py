from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "gcs_prefix": "gs://akbar-december-2024-backup/yt_top_500_music_playlist",
    "local_root": str(Path(__file__).resolve().parent.parent),
    "manifest_path": "data/manifest.json",
    "cookies_path": "",
    "require_cookies": True,
    "max_tracks_per_session": 50,
    "max_minutes_per_session": 120,
    "daily_attempt_cap": 200,
    "delay_min_seconds": 10,
    "delay_max_seconds": 25,
    "rate_limit": "3M",
    "download_timeout_seconds": 900,
    "retries": 3,
    "fragment_retries": 3,
    "extractor_retries": 2,
    "file_access_retries": 3,
    "max_consecutive_blocks": 2,
    "cooldown_after_block_minutes": 45,
    "cooldown_after_rate_limit_minutes": 15,
    "max_attempts_per_track": 5,
    "upload_interval_minutes": 5,
    "upload_on_stop": True,
    "upload_max_pending_files": 10,
    "snapshot_interval_minutes": 5,
    "preferred_format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
    "js_runtimes": "node",
    "remote_components": "ejs:github",
    "impersonate": "",
    "extractor_args": "",
    "min_file_bytes": 102400,
    "duration_tolerance_seconds": 15,
    "duration_tolerance_percent": 0.05,
    "staging_dir": "data/staging",
    "quarantine_dir": "data/quarantine",
    "logs_dir": "data/logs",
    "reports_dir": "reports",
}

ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    "COOKIES_PATH": ("cookies_path", str),
    "GCS_PREFIX": ("gcs_prefix", str),
    "MAX_TRACKS": ("max_tracks_per_session", int),
    "MAX_MINUTES": ("max_minutes_per_session", int),
    "UPLOAD_INTERVAL_MINUTES": ("upload_interval_minutes", int),
    "DAILY_ATTEMPT_CAP": ("daily_attempt_cap", int),
    "REQUIRE_COOKIES": ("require_cookies", lambda v: v.strip().lower() not in {"0", "false", "no", "off"}),
}


def _coerce(value: Any, target: type | Any) -> Any:
    if callable(target) and target not in {str, int, float, bool}:
        return target(value)
    if target is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off", ""}
    if target is int:
        return int(value)
    if target is float:
        return float(value)
    if target is str:
        return str(value)
    return value


@dataclass
class Config:
    values: dict[str, Any]
    path: Path
    repo_root: Path

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def with_overrides(self, overrides: dict[str, Any] | None) -> "Config":
        if not overrides:
            return self
        merged = copy.deepcopy(self.values)
        for key, value in overrides.items():
            if value is not None:
                merged[key] = value
        return Config(merged, self.path, self.repo_root)

    def _path(self, value: str | None, *, base: Path | None = None) -> Path | None:
        if not value:
            return None
        p = Path(str(value)).expanduser()
        if not p.is_absolute():
            p = (base or self.local_root) / p
        return p.resolve()

    @property
    def local_root(self) -> Path:
        return self._path(self.values["local_root"], base=self.repo_root)  # type: ignore[return-value]

    @property
    def manifest_path(self) -> Path:
        return self._path(self.values["manifest_path"])  # type: ignore[return-value]

    @property
    def staging_dir(self) -> Path:
        return self._path(self.values["staging_dir"])  # type: ignore[return-value]

    @property
    def quarantine_dir(self) -> Path:
        return self._path(self.values["quarantine_dir"])  # type: ignore[return-value]

    @property
    def logs_dir(self) -> Path:
        return self._path(self.values["logs_dir"])  # type: ignore[return-value]

    @property
    def reports_dir(self) -> Path:
        return self._path(self.values["reports_dir"])  # type: ignore[return-value]

    @property
    def cookies_path(self) -> Path | None:
        return self._path(self.values.get("cookies_path"), base=self.repo_root)

    @property
    def gcs_prefix(self) -> str:
        return str(self.values["gcs_prefix"]).rstrip("/")

    @property
    def lock_path(self) -> Path:
        return self.manifest_path.parent / "manifest.lock"

    @property
    def ytdlp_archive(self) -> Path:
        return self.manifest_path.parent / "ytdlp-archive.txt"

    @property
    def events_path(self) -> Path:
        return self.manifest_path.parent / "events.jsonl"

    @property
    def runs_path(self) -> Path:
        return self.manifest_path.parent / "runs.jsonl"

    def ensure_dirs(self) -> None:
        for path in (
            self.manifest_path.parent,
            self.staging_dir,
            self.quarantine_dir,
            self.logs_dir,
            self.reports_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _default_config_path() -> Path:
    return Path(__file__).resolve().parent / "config.yaml"


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    config_path = Path(path).expanduser().resolve() if path else _default_config_path()
    repo_root = config_path.parent.parent if config_path.parent.name == "download" else Path.cwd()

    values = copy.deepcopy(DEFAULTS)
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_path} must contain a YAML mapping")
        values.update(loaded)
    elif path:
        raise FileNotFoundError(f"Config file not found: {config_path}")

    for env_name, (key, type_) in ENV_OVERRIDES.items():
        if env_name in os.environ and os.environ[env_name] != "":
            values[key] = _coerce(os.environ[env_name], type_)

    if overrides:
        for key, value in overrides.items():
            if value is not None:
                values[key] = value

    cfg = Config(values, config_path, repo_root)
    return cfg
