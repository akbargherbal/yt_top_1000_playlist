from __future__ import annotations

import argparse
import contextlib
import fcntl
import logging
import os
import random
import shutil
import signal
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from download.config import Config, load_config
from download.downloader import download_track, ytdlp_command
from download.gcs import Gcs
from download.reconcile import reconcile
from download.report import generate as generate_report
from download.state import Manifest, parse_utc, utc_now
from download.uploader import snapshot_state, upload_pending
from download.verify import quarantine_file, verify_file

log = logging.getLogger("run_session")

_STOP_REQUESTED = False


def _signal_handler(signum: int, _frame: Any) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    log.warning("Signal %s received; stopping after the current track", signum)


@contextlib.contextmanager
def manifest_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another session is already running (lock: {path})") from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _setup_logging(config: Config, level: str) -> Path:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = config.logs_dir / f"session-{stamp}.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    for handler in (logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    return log_path


def _preflight(config: Config, *, no_upload: bool) -> None:
    ytdlp_command()  # raises with a useful message if missing
    for tool in ("ffprobe", "ffmpeg"):
        if not shutil.which(tool):
            raise RuntimeError(f"{tool} not found on PATH")
    if not no_upload:
        if not shutil.which("gcloud"):
            raise RuntimeError("gcloud not found on PATH")

    cookies = config.cookies_path
    if cookies:
        if not cookies.exists():
            raise RuntimeError(f"Cookie file not found: {cookies}")
        mode = cookies.stat().st_mode & 0o777
        if mode & 0o077:
            log.warning("Cookie file %s is mode %o; chmod 600 is recommended", cookies, mode)
    elif bool(config.get("require_cookies", True)):
        raise RuntimeError(
            "cookies_path is empty. Set it in config.yaml or pass --cookies; "
            "or set require_cookies: false for a cookie-less test."
        )


def _parse_ranks(value: str | None) -> set[int] | None:
    if not value:
        return None
    ranks: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            ranks.update(range(int(start), int(end) + 1))
        else:
            ranks.add(int(part))
    return ranks


def _eligible(track: dict, now: datetime, retry_failed: bool, max_attempts: int) -> bool:
    status = track.get("status")
    attempts = int(track.get("attempt_count") or 0)

    if status not in {"pending", "failed_retryable", "blocked"}:
        return False
    if status == "pending":
        return attempts < max_attempts or retry_failed
    if attempts >= max_attempts and not retry_failed:
        return False
    if retry_failed:
        return True
    next_retry = parse_utc(track.get("next_retry_utc"))
    return next_retry is None or next_retry <= now


def _candidates(
    manifest: Manifest,
    *,
    ranks: set[int] | None,
    video_id: str | None,
    retry_failed: bool,
    max_attempts: int,
    attempted_ids: set[str],
) -> list[dict]:
    now = datetime.now(timezone.utc)
    tracks = []
    for track in manifest.tracks:
        if track["track_id"] in attempted_ids:
            continue
        if ranks is not None and int(track.get("rank", 0)) not in ranks:
            continue
        if video_id and track.get("video_id") != video_id:
            continue
        if _eligible(track, now, retry_failed, max_attempts):
            tracks.append(track)
    tracks.sort(key=lambda t: int(t.get("rank", 0)))
    return tracks


def _next_retry(attempt_count: int, error_class: str, http_status: int | None, config: Config) -> str:
    now = datetime.now(timezone.utc)
    if error_class == "blocked":
        if http_status == 429:
            minutes = float(config.get("cooldown_after_rate_limit_minutes", 15))
        else:
            minutes = float(config.get("cooldown_after_block_minutes", 45))
    else:
        schedule = [15, 60, 360, 1440]
        minutes = schedule[min(max(attempt_count - 1, 0), len(schedule) - 1)]

    minutes *= random.uniform(0.8, 1.2)
    return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pending_upload_count(manifest: Manifest) -> int:
    return sum(
        1
        for track in manifest.tracks
        if track.get("status") == "downloaded" and track.get("local_path")
    )


def _print_summary(manifest: Manifest, stop_reason: str, session_counts: dict[str, int]) -> None:
    print("")
    print(f"Stop reason: {stop_reason}")
    print(f"This session: {session_counts}")
    print(f"Status counts: {manifest.status_counts()}")
    print(f"Errors: {manifest.error_counts()}")


def main(argv: list[str] | None = None) -> int:
    global _STOP_REQUESTED
    _STOP_REQUESTED = False

    parser = argparse.ArgumentParser(
        description="Run a resumable YouTube audio download session.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", default=None, help="Config file")
    parser.add_argument("--manifest", default=None, help="Override manifest path")
    parser.add_argument("--cookies", default=None, help="Override cookies_path")
    parser.add_argument("--gcs-prefix", default=None, help="Override GCS destination")
    parser.add_argument("--max-tracks", type=int, default=None, help="Max tracks this session")
    parser.add_argument("--max-minutes", type=int, default=None, help="Time budget in minutes")
    parser.add_argument("--delay-min", type=int, default=None, help="Minimum inter-track delay")
    parser.add_argument("--delay-max", type=int, default=None, help="Maximum inter-track delay")
    parser.add_argument("--upload-interval", type=int, default=None, help="Upload interval in minutes")
    parser.add_argument("--upload-max-pending", type=int, default=None, help="Force upload after N pending files")
    parser.add_argument("--ranks", default=None, help="Restrict ranks, e.g. 1-50 or 1,5,10-20")
    parser.add_argument("--video-id", default=None, help="Download one specific YouTube video")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed tracks now, ignoring backoff")
    parser.add_argument("--no-upload", action="store_true", help="Download/verify only; skip GCS")
    parser.add_argument("--no-download", action="store_true", help="Reconcile/upload only")
    parser.add_argument("--dry-run", action="store_true", help="Show plan; download nothing")
    parser.add_argument("--reconcile-only", action="store_true", help="Check state and exit")
    parser.add_argument("--log-level", default="INFO", help="debug, info, warning, error")
    args = parser.parse_args(argv)

    overrides = {
        "manifest_path": args.manifest,
        "cookies_path": args.cookies,
        "gcs_prefix": args.gcs_prefix,
        "max_tracks_per_session": args.max_tracks,
        "max_minutes_per_session": args.max_minutes,
        "delay_min_seconds": args.delay_min,
        "delay_max_seconds": args.delay_max,
        "upload_interval_minutes": args.upload_interval,
        "upload_max_pending_files": args.upload_max_pending,
    }
    config = load_config(args.config, overrides=overrides)
    config.ensure_dirs()
    log_path = _setup_logging(config, args.log_level)

    try:
        with manifest_lock(config.lock_path):
            if not config.manifest_path.exists():
                print(
                    f"ERROR: manifest not found: {config.manifest_path}\n"
                    "Run: python download/import_songs.py --config download/config.yaml",
                    file=sys.stderr,
                )
                return 1

            manifest = Manifest.load(config.manifest_path)
            gcs = None if args.no_upload else Gcs(config.gcs_prefix)

            if args.dry_run:
                ranks = _parse_ranks(args.ranks)
                candidates = _candidates(
                    manifest,
                    ranks=ranks,
                    video_id=args.video_id,
                    retry_failed=args.retry_failed,
                    max_attempts=int(config.get("max_attempts_per_track", 5)),
                    attempted_ids=set(),
                )
                print(f"Manifest: {config.manifest_path}")
                print(f"Status: {manifest.status_counts()}")
                print(f"GCS: {config.gcs_prefix if gcs else '(disabled)'}")
                print(f"Cookies: {config.cookies_path}")
                print(f"Budget: {config.get('max_tracks_per_session')} tracks / {config.get('max_minutes_per_session')} minutes")
                print(f"Delay: {config.get('delay_min_seconds')}-{config.get('delay_max_seconds')} seconds")
                print(f"Upload interval: {config.get('upload_interval_minutes')} minutes")
                print(f"Eligible tracks: {len(candidates)}")
                for track in candidates[:10]:
                    print(f"  {int(track['rank']):4d}  {track['artist']} - {track['title']}  [{track['status']}]")
                return 0

            if args.reconcile_only:
                summary = reconcile(
                    manifest,
                    config,
                    gcs=gcs,
                    check_gcs=not args.no_upload,
                    fix=True,
                    reset_stale_minutes=20,
                    prune_partials=True,
                )
                print(f"Reconcile: {summary}")
                print(f"Status: {manifest.status_counts()}")
                generate_report(manifest, config.reports_dir)
                return 0

            _preflight(config, no_upload=args.no_upload)

            reconcile(
                manifest,
                config,
                gcs=gcs,
                check_gcs=False,
                fix=True,
                reset_stale_minutes=20,
                prune_partials=True,
            )

            if args.no_download:
                if gcs is not None:
                    summary = upload_pending(manifest, config, gcs, snapshot=True)
                    snapshot_state(manifest, config, gcs, snapshot=True, include_meta=True)
                    for handler in logging.getLogger().handlers:
                        handler.flush()
                    gcs.cp(log_path, f"{config.gcs_prefix}/logs/{log_path.name}")
                    print(f"Upload: {summary}")
                generate_report(manifest, config.reports_dir)
                print(f"Status: {manifest.status_counts()}")
                return 0

            if gcs is not None:
                try:
                    snapshot_state(manifest, config, gcs, snapshot=False, include_meta=True)
                except Exception:  # noqa: BLE001 - backup failure should not block downloads
                    log.exception("Initial state snapshot failed; continuing")

            signal.signal(signal.SIGTERM, _signal_handler)
            signal.signal(signal.SIGINT, _signal_handler)

            max_tracks = int(config.get("max_tracks_per_session", 50))
            max_minutes = int(config.get("max_minutes_per_session", 120))
            daily_cap = int(config.get("daily_attempt_cap", 200))
            max_attempts = int(config.get("max_attempts_per_track", 5))
            upload_interval = float(config.get("upload_interval_minutes", 5)) * 60
            upload_max_pending = int(config.get("upload_max_pending_files", 10))
            delay_min = float(config.get("delay_min_seconds", 10))
            delay_max = float(config.get("delay_max_seconds", 25))
            ranks = _parse_ranks(args.ranks)

            session_started = utc_now()
            run_id = int(time.time())
            started = time.monotonic()
            last_upload = time.monotonic()
            daily_attempts = manifest.count_events_today("attempt_started")
            attempted_ids: set[str] = set()
            counts = {"attempted": 0, "succeeded": 0, "failed": 0, "blocked": 0}
            stop_reason = "completed"
            current_track_id: str | None = None
            blocked_streak = 0

            manifest.append_event(
                "session_start",
                run_id=run_id,
                max_tracks=max_tracks,
                max_minutes=max_minutes,
                status_counts=manifest.status_counts(),
            )

            try:
                while True:
                    if _STOP_REQUESTED:
                        stop_reason = "signal"
                        break
                    if counts["attempted"] >= max_tracks:
                        stop_reason = "track_budget"
                        break
                    if time.monotonic() - started >= max_minutes * 60:
                        stop_reason = "time_budget"
                        break
                    if daily_attempts + counts["attempted"] >= daily_cap:
                        stop_reason = "daily_cap"
                        break

                    candidates = _candidates(
                        manifest,
                        ranks=ranks,
                        video_id=args.video_id,
                        retry_failed=args.retry_failed,
                        max_attempts=max_attempts,
                        attempted_ids=attempted_ids,
                    )
                    if not candidates:
                        stop_reason = "no_candidates"
                        break

                    track = candidates[0]
                    track_id = track["track_id"]
                    current_track_id = track_id
                    attempted_ids.add(track_id)
                    counts["attempted"] += 1
                    attempt_number = int(track.get("attempt_count") or 0) + 1

                    manifest.update(
                        track_id,
                        status="downloading",
                        attempt_count=attempt_number,
                        last_attempt_utc=utc_now(),
                        next_retry_utc=None,
                        error_class=None,
                        error_message=None,
                    )
                    manifest.append_event(
                        "attempt_started",
                        track_id=track_id,
                        video_id=track["video_id"],
                        attempt=attempt_number,
                        run_id=run_id,
                    )
                    manifest.save()

                    log.info(
                        "Downloading %s/%s rank=%s %s - %s",
                        counts["attempted"],
                        max_tracks,
                        track.get("rank"),
                        track.get("artist"),
                        track.get("title"),
                    )
                    result = download_track(track, config)

                    if result.ok and result.path:
                        verified = verify_file(
                            result.path,
                            track.get("duration_seconds"),
                            min_file_bytes=int(config.get("min_file_bytes", 102400)),
                            tolerance_seconds=float(config.get("duration_tolerance_seconds", 15)),
                            tolerance_percent=float(config.get("duration_tolerance_percent", 0.05)),
                        )
                        if verified.ok:
                            manifest.update(
                                track_id,
                                status="downloaded",
                                local_path=str(result.path),
                                file_ext=result.path.suffix,
                                file_bytes=verified.file_bytes,
                                sha256=verified.sha256,
                                codec=verified.codec,
                                bitrate=verified.bitrate,
                                actual_duration=verified.actual_duration,
                                error_class=None,
                                error_message=None,
                                next_retry_utc=None,
                            )
                            manifest.append_event(
                                "download_ok",
                                track_id=track_id,
                                video_id=track["video_id"],
                                path=str(result.path),
                                bytes=verified.file_bytes,
                                sha256=verified.sha256,
                                reused=result.reused,
                            )
                            manifest.append_event(
                                "verify_ok",
                                track_id=track_id,
                                duration_actual=verified.actual_duration,
                            )
                            counts["succeeded"] += 1
                            blocked_streak = 0
                            log.info("OK %s (%.1fs, %d bytes)", track["video_id"], verified.actual_duration or 0, verified.file_bytes or 0)
                        else:
                            dest = quarantine_file(
                                result.path, config.quarantine_dir, verified.reason or "invalid"
                            )
                            retry_at = _next_retry(attempt_number, "invalid", None, config)
                            manifest.update(
                                track_id,
                                status="failed_retryable",
                                local_path=None,
                                file_ext=None,
                                error_class="invalid",
                                error_message=verified.reason or "validation failed",
                                next_retry_utc=retry_at,
                                notes=f"Quarantined: {dest}",
                            )
                            manifest.append_event(
                                "verify_failed",
                                track_id=track_id,
                                reason=verified.reason,
                                quarantine=str(dest),
                            )
                            counts["failed"] += 1
                            log.warning("Invalid file for %s: %s", track["video_id"], verified.reason)
                    else:
                        error_class = result.error_class or "failed_retryable"
                        message = (result.message or "download failed")[:2000]
                        if error_class == "blocked":
                            retry_at = _next_retry(attempt_number, error_class, result.http_status, config)
                            manifest.update(
                                track_id,
                                status="blocked",
                                error_class="blocked",
                                error_message=message,
                                next_retry_utc=retry_at,
                            )
                            manifest.append_event(
                                "blocked",
                                track_id=track_id,
                                video_id=track["video_id"],
                                http_status=result.http_status,
                                next_retry_utc=retry_at,
                            )
                            counts["blocked"] += 1
                            blocked_streak += 1
                            log.warning("Blocked on %s; next retry %s", track["video_id"], retry_at)
                            # Stop the session on the first block instead of hammering
                            # YouTube with more requests.
                            stop_reason = "blocked"
                            manifest.save()
                            break
                        elif error_class == "unavailable":
                            manifest.update(
                                track_id,
                                status="unavailable",
                                error_class="unavailable",
                                error_message=message,
                                next_retry_utc=None,
                            )
                            manifest.append_event(
                                "unavailable", track_id=track_id, video_id=track["video_id"]
                            )
                            counts["failed"] += 1
                            log.warning("Unavailable: %s", track["video_id"])
                        else:
                            retry_at = _next_retry(attempt_number, error_class, result.http_status, config)
                            manifest.update(
                                track_id,
                                status="failed_retryable",
                                error_class=error_class,
                                error_message=message,
                                next_retry_utc=retry_at,
                            )
                            manifest.append_event(
                                "failed",
                                track_id=track_id,
                                video_id=track["video_id"],
                                error_class=error_class,
                                next_retry_utc=retry_at,
                            )
                            counts["failed"] += 1
                            log.warning("Failed %s: %s", track["video_id"], message.splitlines()[0][:200])

                    current_track_id = None
                    manifest.save()

                    if gcs is not None:
                        pending = _pending_upload_count(manifest)
                        due = time.monotonic() - last_upload >= upload_interval
                        if due or pending >= upload_max_pending:
                            try:
                                upload_summary = upload_pending(
                                    manifest, config, gcs, max_files=pending or None, snapshot=True
                                )
                                log.info("Upload tick: %s", upload_summary)
                            except Exception:  # noqa: BLE001 - keep downloading
                                log.exception("Upload tick failed; will retry later")
                            last_upload = time.monotonic()

                    if stop_reason == "blocked":
                        break

                    remaining = _candidates(
                        manifest,
                        ranks=ranks,
                        video_id=args.video_id,
                        retry_failed=args.retry_failed,
                        max_attempts=max_attempts,
                        attempted_ids=attempted_ids,
                    )
                    if remaining:
                        time.sleep(random.uniform(delay_min, delay_max))

            except KeyboardInterrupt:
                stop_reason = "interrupted"
                log.warning("Interrupted by user")
            except Exception:
                stop_reason = "error"
                log.exception("Session aborted")
            finally:
                if current_track_id:
                    try:
                        current = manifest.get(current_track_id)
                        if current.get("status") == "downloading":
                            manifest.update(
                                current_track_id,
                                status="failed_retryable",
                                error_class="interrupted",
                                error_message="Session interrupted while downloading",
                                next_retry_utc=utc_now(),
                            )
                            manifest.append_event(
                                "failed", track_id=current_track_id, reason="interrupted"
                            )
                    except KeyError:
                        pass
                manifest.save()

            report_paths = generate_report(manifest, config.reports_dir)
            manifest.append_run(
                {
                    "run_id": run_id,
                    "started_utc": session_started,
                    "finished_utc": utc_now(),
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "stop_reason": stop_reason,
                    "counts": counts,
                    "status_counts": manifest.status_counts(),
                    "reports": {kind: str(path) for kind, path in report_paths.items()},
                }
            )
            manifest.append_event(
                "session_end", run_id=run_id, stop_reason=stop_reason, counts=counts
            )
            manifest.save()

            if gcs is not None and (bool(config.get("upload_on_stop", True)) or counts["succeeded"]):
                try:
                    summary = upload_pending(manifest, config, gcs, snapshot=True)
                    snapshot_state(manifest, config, gcs, snapshot=True, include_meta=True)
                    for handler in logging.getLogger().handlers:
                        handler.flush()
                    gcs.cp(log_path, f"{config.gcs_prefix}/logs/{log_path.name}")
                    print(f"Final upload: {summary}")
                except Exception:  # noqa: BLE001 - report the failure, keep local state
                    log.exception("Final upload/snapshot failed")

            _print_summary(manifest, stop_reason, counts)
            print(f"Report: {report_paths.get('csv')}")
            return 2 if stop_reason == "blocked" else 0

    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
