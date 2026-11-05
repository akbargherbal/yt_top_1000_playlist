from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

EXCLUDED_SUFFIXES = {
    ".part",
    ".ytdl",
    ".info.json",
    ".description",
    ".webp",
    ".jpg",
    ".jpeg",
    ".png",
    ".temp",
}


@dataclass
class DownloadResult:
    ok: bool
    path: Path | None = None
    error_class: str | None = None
    message: str | None = None
    http_status: int | None = None
    exit_code: int | None = None
    reused: bool = False
    command: list[str] | None = None


def ytdlp_command() -> list[str]:
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    raise RuntimeError(
        "yt-dlp is not installed. Run: python -m pip install -U yt-dlp "
        "(or activate the project venv first)."
    )


def find_media(staging_dir: Path, video_id: str) -> Path | None:
    candidates: list[Path] = []
    for path in staging_dir.glob(f"{video_id}.*"):
        if not path.is_file():
            continue
        name = path.name
        if any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
            continue
        candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _archive_remove(archive: Path, video_id: str) -> None:
    if not archive.exists():
        return
    try:
        lines = archive.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    kept = [line for line in lines if video_id not in line.split()]
    if len(kept) != len(lines):
        tmp = archive.with_suffix(archive.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, archive)


def _http_status(text: str) -> int | None:
    match = re.search(r"(?:HTTP Error|status code|HTTP)\s*(\d{3})", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if "429" in text:
        return 429
    if "403" in text:
        return 403
    return None


def classify_error(text: str, exit_code: int | None = None) -> tuple[str, str]:
    lower = text.lower()
    status = _http_status(text)

    blocked_markers = (
        "sign in to confirm",
        "not a bot",
        "confirm you're not a bot",
        "confirm you’re not a bot",
        "use --cookies",
        "cookies are no longer valid",
        "http error 403",
        "http error 429",
        "too many requests",
        "unable to download video data",
        "this request was blocked",
    )
    if status in {403, 429} or any(marker in lower for marker in blocked_markers):
        return "blocked", "YouTube blocked the request (bot check, rate limit, or stale cookies)."

    unavailable_markers = (
        "video unavailable",
        "private video",
        "removed by the uploader",
        "members-only",
        "this video is not available",
        "not available in your country",
        "has been removed",
        "account associated with this video has been terminated",
        "copyright",
    )
    if any(marker in lower for marker in unavailable_markers):
        return "unavailable", "Video is unavailable (private, removed, region-locked, or restricted)."

    retryable_markers = (
        "timed out",
        "timeout",
        "connection reset",
        "connection refused",
        "temporary failure",
        "network is unreachable",
        "http error 5",
        "unable to connect",
        "getaddrinfo",
    )
    if any(marker in lower for marker in retryable_markers):
        return "failed_retryable", "Transient network/server error."

    if exit_code not in {None, 0}:
        return "failed_retryable", f"yt-dlp exited with code {exit_code}."
    return "failed_retryable", "Unknown download failure."


def _build_command(track: dict, config, staging_dir: Path) -> list[str]:
    video_id = str(track["video_id"])
    opts: list[str] = []

    cookies = config.cookies_path
    if cookies:
        opts += ["--cookies", str(cookies)]

    rate_limit = config.get("rate_limit")
    if rate_limit:
        opts += ["--limit-rate", str(rate_limit)]

    js_runtimes = config.get("js_runtimes")
    if js_runtimes and shutil.which("node"):
        opts += ["--js-runtimes", str(js_runtimes)]

    remote_components = config.get("remote_components")
    if remote_components:
        opts += ["--remote-components", str(remote_components)]

    extractor_args = config.get("extractor_args")
    if extractor_args:
        opts += ["--extractor-args", str(extractor_args)]

    impersonate = config.get("impersonate")
    if impersonate:
        opts += ["--impersonate", str(impersonate)]

    cmd = ytdlp_command() + opts + [
        "--no-playlist",
        "--no-progress",
        "--newline",
        "--no-mtime",
        "--format",
        str(config.get("preferred_format")),
        "--output",
        str(staging_dir / f"{video_id}.%(ext)s"),
        "--write-info-json",
        "--write-thumbnail",
        "--retries",
        str(config.get("retries", 3)),
        "--fragment-retries",
        str(config.get("fragment_retries", 3)),
        "--extractor-retries",
        str(config.get("extractor_retries", 2)),
        "--file-access-retries",
        str(config.get("file_access_retries", 3)),
        "--concurrent-fragments",
        "1",
        "--socket-timeout",
        "30",
        "--sleep-requests",
        "1.5",
        "--download-archive",
        str(config.ytdlp_archive),
        "--print",
        "after_move:filepath",
        str(track.get("url") or f"https://www.youtube.com/watch?v={video_id}"),
    ]

    return cmd


def download_track(track: dict, config) -> DownloadResult:
    staging_dir = config.staging_dir
    staging_dir.mkdir(parents=True, exist_ok=True)
    video_id = str(track["video_id"])

    existing = find_media(staging_dir, video_id)
    if existing and existing.stat().st_size >= int(config.get("min_file_bytes", 102400)):
        return DownloadResult(ok=True, path=existing, reused=True)

    cookies = config.cookies_path
    if cookies and not cookies.exists():
        return DownloadResult(
            ok=False,
            error_class="blocked",
            message=f"Cookie file not found: {cookies}",
        )
    if not cookies and bool(config.get("require_cookies", True)):
        return DownloadResult(
            ok=False,
            error_class="blocked",
            message="cookies_path is empty, but require_cookies is true.",
        )

    _archive_remove(config.ytdlp_archive, video_id)
    cmd = _build_command(track, config, staging_dir)
    timeout = int(config.get("download_timeout_seconds", 900))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        return DownloadResult(
            ok=False,
            error_class="failed_retryable",
            message=f"yt-dlp timed out after {timeout}s",
            command=cmd,
        )
    except OSError as exc:
        return DownloadResult(
            ok=False,
            error_class="failed_retryable",
            message=f"Could not start yt-dlp: {exc}",
            command=cmd,
        )

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    path: Path | None = None
    for line in reversed((proc.stdout or "").splitlines()):
        candidate = Path(line.strip())
        if line.strip() and candidate.exists() and candidate.is_file():
            path = candidate
            break
    if path is None:
        path = find_media(staging_dir, video_id)

    if proc.returncode == 0 and path and path.exists():
        return DownloadResult(
            ok=True,
            path=path,
            exit_code=proc.returncode,
            command=cmd,
        )

    if proc.returncode == 0 and not path:
        return DownloadResult(
            ok=False,
            error_class="failed_retryable",
            message="yt-dlp succeeded but no output media file was found.",
            exit_code=proc.returncode,
            command=cmd,
        )

    error_class, message = classify_error(output, proc.returncode)
    detail = (proc.stderr or proc.stdout or "").strip()
    if detail:
        message = f"{message}\n{detail[-2000:]}"
    return DownloadResult(
        ok=False,
        error_class=error_class,
        message=message,
        http_status=_http_status(output),
        exit_code=proc.returncode,
        command=cmd,
    )
