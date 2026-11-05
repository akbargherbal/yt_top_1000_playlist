from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from download.config import load_config
from download.gcs import Gcs
from download.report import generate as generate_report
from download.state import Manifest, utc_now

log = logging.getLogger("uploader")


def _remote_audio(config, track: dict) -> str:
    local = Path(track["local_path"])
    return f"{config.gcs_prefix}/audio/{int(track['rank']):04d}_{track['video_id']}{local.suffix}"


def _remote_info(config, track: dict) -> str:
    return f"{config.gcs_prefix}/metadata/{int(track['rank']):04d}_{track['video_id']}.info.json"


def _info_local(track: dict) -> Path:
    return Path(track["local_path"]).with_suffix(".info.json")


def _snapshot_remote(config, stamp: str, name: str) -> str:
    return f"{config.gcs_prefix}/state/snapshots/{stamp}/{name}"


def snapshot_state(
    manifest: Manifest,
    config,
    gcs: Gcs,
    *,
    snapshot: bool = False,
    include_meta: bool = False,
) -> None:
    """Upload the manifest + reports. Never let a snapshot failure stop downloads."""
    try:
        manifest.save()
    except Exception:
        log.exception("Could not save local manifest before snapshot")
        raise

    report_paths = generate_report(manifest, config.reports_dir)
    latest = f"{config.gcs_prefix}/state/latest"

    gcs.cp(manifest.path, f"{latest}/manifest.json")
    if manifest.backup_path.exists():
        gcs.cp(manifest.backup_path, f"{latest}/manifest.json.bak")
    for path in report_paths.values():
        gcs.cp(path, f"{latest}/{path.name}")

    if include_meta:
        gcs.cp(config.path, f"{latest}/meta/config.yaml")
        plan = config.repo_root / "PLAN.md"
        if plan.exists():
            gcs.cp(plan, f"{latest}/meta/PLAN.md")
        for py_file in sorted((config.repo_root / "download").glob("*.py")):
            gcs.cp(py_file, f"{latest}/meta/download/{py_file.name}")

    if snapshot:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        gcs.cp(manifest.path, _snapshot_remote(config, stamp, "manifest.json"))
        if manifest.backup_path.exists():
            gcs.cp(manifest.backup_path, _snapshot_remote(config, stamp, "manifest.json.bak"))
        for path in report_paths.values():
            gcs.cp(path, _snapshot_remote(config, stamp, path.name))
        if manifest.events_path.exists():
            gcs.cp(manifest.events_path, _snapshot_remote(config, stamp, "events.jsonl"))


def upload_pending(
    manifest: Manifest,
    config,
    gcs: Gcs,
    *,
    force: bool = False,
    max_files: int | None = None,
    verify_only: bool = False,
    snapshot: bool = True,
) -> dict[str, int]:
    summary = {"uploaded": 0, "verified": 0, "failed": 0, "skipped": 0}

    if verify_only:
        for track in manifest.tracks_by_status({"uploaded", "done"}):
            uri = track.get("gcs_uri")
            if not uri:
                continue
            try:
                ok, detail = gcs.verify_object(Path(track["local_path"]), uri)
            except Exception as exc:  # noqa: BLE001 - GCS boundary
                summary["failed"] += 1
                log.warning("verify failed for %s: %s", track["video_id"], exc)
                continue
            if ok:
                manifest.update(track["track_id"], status="done", upload_error=None, verified_utc=utc_now())
                summary["verified"] += 1
            else:
                manifest.update(
                    track["track_id"],
                    status="downloaded",
                    upload_error=f"verification failed: {detail}",
                    next_retry_utc=utc_now(),
                )
                summary["failed"] += 1
                log.warning("verification mismatch for %s: %s", track["video_id"], detail)
        manifest.save()
        if snapshot:
            snapshot_state(manifest, config, gcs, snapshot=True)
        return summary

    candidates = [
        track
        for track in manifest.tracks_by_status({"downloaded"})
        if track.get("local_path")
    ]
    candidates.sort(key=lambda t: int(t.get("rank", 0)))
    if max_files is not None:
        candidates = candidates[:max_files]

    for track in candidates:
        track_id = track["track_id"]
        local = Path(track["local_path"])
        if not local.exists():
            summary["failed"] += 1
            manifest.update(
                track_id,
                status="failed_retryable",
                error_class="local_missing",
                error_message=f"Local file disappeared: {local}",
                next_retry_utc=utc_now(),
            )
            manifest.append_event("failed", track_id=track_id, reason="local_missing", path=str(local))
            manifest.save()
            continue

        remote = _remote_audio(config, track)
        info_local = _info_local(track)
        info_remote = _remote_info(config, track)
        upload_attempt = int(track.get("upload_attempt_count") or 0) + 1

        try:
            gcs.cp(local, remote)
        except Exception as exc:  # noqa: BLE001 - GCS boundary
            summary["failed"] += 1
            manifest.update(
                track_id,
                upload_attempt_count=upload_attempt,
                upload_error=str(exc)[:1000],
                next_retry_utc=utc_now(),
            )
            manifest.append_event("upload_failed", track_id=track_id, error=str(exc)[:1000])
            manifest.save()
            log.warning("upload failed for %s: %s", track["video_id"], exc)
            continue

        info_uri = None
        if info_local.exists():
            try:
                gcs.cp(info_local, info_remote)
                info_uri = info_remote
            except Exception as exc:  # noqa: BLE001 - GCS boundary
                log.warning("metadata upload failed for %s: %s", track["video_id"], exc)

        manifest.update(
            track_id,
            status="uploaded",
            upload_attempt_count=upload_attempt,
            upload_error=None,
            gcs_uri=remote,
            info_json_uri=info_uri,
            uploaded_utc=utc_now(),
            file_bytes=local.stat().st_size,
        )
        manifest.append_event("upload_ok", track_id=track_id, gcs_uri=remote)
        manifest.save()

        try:
            ok, detail = gcs.verify_object(local, remote)
        except Exception as exc:  # noqa: BLE001 - GCS boundary
            ok, detail = False, str(exc)

        if ok:
            manifest.update(
                track_id,
                status="done",
                upload_error=None,
                verified_utc=utc_now(),
            )
            manifest.append_event("upload_verified", track_id=track_id, gcs_uri=remote)
            summary["uploaded"] += 1
        else:
            manifest.update(
                track_id,
                status="downloaded",
                upload_error=f"verification failed: {detail}",
                next_retry_utc=utc_now(),
            )
            manifest.append_event("upload_failed", track_id=track_id, reason="verify", error=str(detail))
            summary["failed"] += 1
            log.warning("verification failed for %s: %s", track["video_id"], detail)
        manifest.save()

    if snapshot:
        snapshot_state(manifest, config, gcs, snapshot=True)

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload pending audio and manifest snapshots to GCS")
    parser.add_argument("--config", default=None, help="Config file")
    parser.add_argument("--once", action="store_true", help="Run one upload cycle and exit (default)")
    parser.add_argument("--watch", action="store_true", help="Loop every upload_interval_minutes")
    parser.add_argument("--force", action="store_true", help="Upload all downloaded tracks now")
    parser.add_argument("--max-files", type=int, default=None, help="Limit files this cycle")
    parser.add_argument("--verify-only", action="store_true", help="Verify GCS objects without uploading")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    config.ensure_dirs()
    if not config.manifest_path.exists():
        print(f"ERROR: manifest not found: {config.manifest_path}", file=sys.stderr)
        return 1

    gcs = Gcs(config.gcs_prefix)

    def run_once() -> dict[str, int]:
        manifest = Manifest.load(config.manifest_path)
        return upload_pending(
            manifest,
            config,
            gcs,
            force=args.force,
            max_files=args.max_files,
            verify_only=args.verify_only,
            snapshot=True,
        )

    if args.watch:
        interval = max(1, int(config.get("upload_interval_minutes", 5))) * 60
        try:
            while True:
                summary = run_once()
                print(f"upload tick: {summary}")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("Stopped.")
            return 0

    summary = run_once()
    print(f"upload tick: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
