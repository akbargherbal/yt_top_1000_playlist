from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Manifest:
    """Authoritative JSON state plus append-only event logs."""

    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = Path(path)
        self.data = data

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Manifest not found: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "tracks" not in data:
            raise ValueError(f"Invalid manifest: {p}")
        if int(data.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported manifest schema_version={data.get('schema_version')} "
                f"(expected {SCHEMA_VERSION})"
            )
        return cls(p, data)

    @classmethod
    def create(cls, path: str | Path, data: dict[str, Any]) -> "Manifest":
        data = dict(data)
        data["schema_version"] = SCHEMA_VERSION
        data.setdefault("generated_utc", utc_now())
        return cls(Path(path), data)

    @property
    def tracks(self) -> list[dict[str, Any]]:
        return self.data["tracks"]

    @property
    def events_path(self) -> Path:
        return self.path.parent / "events.jsonl"

    @property
    def runs_path(self) -> Path:
        return self.path.parent / "runs.jsonl"

    @property
    def backup_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".bak")

    def get(self, track_id: str) -> dict[str, Any]:
        for track in self.tracks:
            if track["track_id"] == track_id:
                return track
        raise KeyError(track_id)

    def by_video_id(self, video_id: str) -> dict[str, Any] | None:
        for track in self.tracks:
            if track.get("video_id") == video_id:
                return track
        return None

    def update(self, track_id: str, **fields: Any) -> dict[str, Any]:
        track = self.get(track_id)
        track.update(fields)
        track["updated_utc"] = utc_now()
        return track

    def save(self) -> None:
        self.data["generated_utc"] = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")

        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        if self.path.exists():
            shutil.copy2(self.path, self.backup_path)

        os.replace(tmp, self.path)
        _fsync_dir(self.path.parent)

        # Fail fast if the file is not valid JSON after the replace.
        json.loads(self.path.read_text(encoding="utf-8"))

    def append_event(self, event: str, **fields: Any) -> None:
        record = {"ts": utc_now(), "event": event}
        record.update(fields)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def append_run(self, record: dict[str, Any]) -> None:
        payload = {"ts": utc_now()}
        payload.update(record)
        self.runs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.runs_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def count_events_today(self, event: str = "attempt_started") -> int:
        if not self.events_path.exists():
            return 0
        today = utc_now()[:10]
        total = 0
        with open(self.events_path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event") == event and str(record.get("ts", "")).startswith(today):
                    total += 1
        return total

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for track in self.tracks:
            status = str(track.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    def tier_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for track in self.tracks:
            tier = str(track.get("tier", "unknown"))
            counts[tier] = counts.get(tier, 0) + 1
        return dict(sorted(counts.items()))

    def error_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for track in self.tracks:
            error = track.get("error_class")
            if error:
                counts[str(error)] = counts.get(str(error), 0) + 1
        return dict(sorted(counts.items()))

    def tracks_by_status(self, statuses: Iterable[str]) -> list[dict[str, Any]]:
        wanted = set(statuses)
        return [track for track in self.tracks if track.get("status") in wanted]
