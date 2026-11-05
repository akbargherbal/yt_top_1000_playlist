from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from download.config import load_config
from download.gcs import Gcs
from download.state import Manifest, parse_utc, utc_now
from download.verify import quarantine_file, verify_file


def restore_from_gcs(config, gcs: Gcs) -> Path:
    remote = f"{config.gcs_prefix}/state/latest/manifest.json"
    tmp = config.manifest_path.with_suffix(".restore.tmp")
    gcs.download(remote, tmp)
    # Validate before replacing the local manifest.
    restored = Manifest.load(tmp)
    config.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if config.manifest_path.exists():
        backup = config.manifest_path.with_suffix(config.manifest_path.suffix + ".pre-restore.bak")
        backup.write_bytes(config.manifest_path.read_bytes())
    tmp.replace(config.manifest_path)
    return config.manifest_path


def _local_media_map(config) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not config.staging_dir.exists():
        return found
    for path in config.staging_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if any(
            name.endswith(suffix)
            for suffix in (
                ".part",
                ".ytdl",
                ".info.json",
                ".description",
                ".webp",
                ".jpg",
                ".jpeg",
                ".png",
                ".temp",
            )
        ):
            continue
        video_id = name.split(".", 1)[0]
        found.setdefault(video_id, path)
    return found


def reconcile(
    manifest: Manifest,
    config,
    *,
    gcs: Gcs | None = None,
    check_gcs: bool = False,
    fix: bool = True,
    reset_stale_minutes: int = 20,
    prune_partials: bool = False,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "stale_reset": 0,
        "local_found": 0,
        "local_invalid": 0,
        "remote_missing": 0,
        "remote_size_mismatch": 0,
        "partials_removed": 0,
    }
    changed = False
    now = datetime.now(timezone.utc)
    media = _local_media_map(config)

    for track in manifest.tracks:
        track_id = track["track_id"]
        status = track.get("status")

        if status == "downloading":
            last = parse_utc(track.get("last_attempt_utc"))
            if last is None or now - last > timedelta(minutes=reset_stale_minutes):
                summary["stale_reset"] += 1
                if fix:
                    manifest.update(
                        track_id,
                        status="pending",
                        error_class="stale",
                        error_message="Reset stale downloading row after crash/interruption",
                        next_retry_utc=None,
                    )
                    manifest.append_event("reconcile", track_id=track_id, action="stale_reset")
                    changed = True
                status = "pending"

        if status in {"done", "uploaded"}:
            continue

        local = Path(track["local_path"]) if track.get("local_path") else None
        if local and local.exists():
            media.setdefault(track["video_id"], local)
        elif track.get("video_id") in media:
            local = media[track["video_id"]]
            summary["local_found"] += 1
            if fix:
                manifest.update(
                    track_id,
                    local_path=str(local),
                    file_ext=local.suffix,
                    file_bytes=local.stat().st_size,
                )
                changed = True

        if local and local.exists() and status in {"pending", "failed_retryable", "blocked", "quarantined"}:
            result = verify_file(
                local,
                track.get("duration_seconds"),
                min_file_bytes=int(config.get("min_file_bytes", 102400)),
                tolerance_seconds=float(config.get("duration_tolerance_seconds", 15)),
                tolerance_percent=float(config.get("duration_tolerance_percent", 0.05)),
            )
            if result.ok:
                summary["local_found"] += 1
                if fix:
                    manifest.update(
                        track_id,
                        status="downloaded",
                        local_path=str(local),
                        file_ext=local.suffix,
                        file_bytes=result.file_bytes,
                        sha256=result.sha256,
                        codec=result.codec,
                        bitrate=result.bitrate,
                        actual_duration=result.actual_duration,
                        error_class=None,
                        error_message=None,
                        next_retry_utc=None,
                    )
                    manifest.append_event("reconcile", track_id=track_id, action="local_recovered")
                    changed = True
            else:
                summary["local_invalid"] += 1
                if fix:
                    dest = quarantine_file(local, config.quarantine_dir, result.reason or "invalid")
                    manifest.update(
                        track_id,
                        status="failed_retryable",
                        local_path=None,
                        error_class="invalid",
                        error_message=f"Reconcile rejected local file: {result.reason}",
                        next_retry_utc=utc_now(),
                        notes=f"Quarantined: {dest}",
                    )
                    manifest.append_event(
                        "failed", track_id=track_id, reason="invalid_local", file=str(dest)
                    )
                    changed = True

        if status == "downloaded" and (not local or not local.exists()):
            summary["remote_missing"] += 1
            if fix:
                manifest.update(
                    track_id,
                    status="failed_retryable",
                    error_class="local_missing",
                    error_message="Local file missing while waiting for upload",
                    next_retry_utc=utc_now(),
                )
                changed = True

    if check_gcs:
        if gcs is None:
            gcs = Gcs(config.gcs_prefix)
        for track in manifest.tracks_by_status({"uploaded", "done"}):
            uri = track.get("gcs_uri")
            if not uri:
                continue
            info = gcs.describe(uri)
            if info is None:
                summary["remote_missing"] += 1
                if fix:
                    local = Path(track["local_path"]) if track.get("local_path") else None
                    if local and local.exists():
                        manifest.update(
                            track["track_id"],
                            status="downloaded",
                            error_class="remote_missing",
                            error_message="GCS object missing; local file kept for re-upload",
                            next_retry_utc=utc_now(),
                        )
                    else:
                        manifest.update(
                            track["track_id"],
                            status="failed_retryable",
                            error_class="remote_missing",
                            error_message="GCS object missing and local file unavailable",
                            next_retry_utc=utc_now(),
                        )
                    changed = True
                continue

            try:
                remote_size = int(info.get("size", -1))
            except (TypeError, ValueError):
                remote_size = -1
            local = Path(track["local_path"]) if track.get("local_path") else None
            if local and local.exists() and remote_size != local.stat().st_size:
                summary["remote_size_mismatch"] += 1
                if fix:
                    manifest.update(
                        track["track_id"],
                        status="downloaded",
                        upload_error=f"size mismatch: local={local.stat().st_size} remote={remote_size}",
                        next_retry_utc=utc_now(),
                    )
                    changed = True
            elif fix and track.get("status") != "done":
                manifest.update(track["track_id"], status="done", verified_utc=utc_now())
                changed = True

    if prune_partials:
        for pattern in ("*.part", "*.ytdl", "*.temp"):
            for path in config.staging_dir.glob(pattern):
                try:
                    path.unlink()
                    summary["partials_removed"] += 1
                except OSError:
                    pass

    if changed:
        manifest.save()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile manifest, local files, and GCS state")
    parser.add_argument("--config", default=None, help="Config file")
    parser.add_argument("--from-gcs", action="store_true", help="Restore state/latest/manifest.json first")
    parser.add_argument("--check-gcs", action="store_true", help="Verify uploaded/done objects")
    parser.add_argument("--fix", action="store_true", help="Apply repairs (otherwise report only)")
    parser.add_argument("--reset-stale", type=int, default=20, help="Reset downloading rows older than N minutes")
    parser.add_argument("--prune-partials", action="store_true", help="Delete .part/.ytdl files")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    config.ensure_dirs()
    gcs = Gcs(config.gcs_prefix)

    if args.from_gcs:
        restore_from_gcs(config, gcs)

    if not config.manifest_path.exists():
        print(f"ERROR: manifest not found: {config.manifest_path}", file=sys.stderr)
        return 1

    manifest = Manifest.load(config.manifest_path)
    summary = reconcile(
        manifest,
        config,
        gcs=gcs,
        check_gcs=args.check_gcs,
        fix=args.fix,
        reset_stale_minutes=args.reset_stale,
        prune_partials=args.prune_partials,
    )
    summary["status_counts"] = manifest.status_counts()

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Status: {manifest.status_counts()}")
        for key, value in summary.items():
            if key != "status_counts":
                print(f"{key}: {value}")
        if not args.fix:
            print("Report-only; re-run with --fix to apply repairs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
