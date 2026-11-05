from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class VerifyResult:
    ok: bool
    reason: str | None = None
    codec: str | None = None
    bitrate: int | None = None
    actual_duration: float | None = None
    file_bytes: int | None = None
    sha256: str | None = None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def verify_file(
    path: str | Path,
    expected_duration: float | int | None,
    *,
    min_file_bytes: int = 102400,
    tolerance_seconds: float = 15.0,
    tolerance_percent: float = 0.05,
) -> VerifyResult:
    file_path = Path(path)
    if not file_path.exists():
        return VerifyResult(False, f"file not found: {file_path}")

    size = file_path.stat().st_size
    if size < min_file_bytes:
        return VerifyResult(False, f"file too small: {size} bytes", file_bytes=size)

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return VerifyResult(False, "ffprobe not found", file_bytes=size)

    try:
        proc = _run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration,size,bit_rate",
                "-show_entries",
                "stream=codec_type,codec_name",
                "-of",
                "json",
                str(file_path),
            ],
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(False, "ffprobe timed out", file_bytes=size)

    if proc.returncode != 0:
        return VerifyResult(False, f"ffprobe failed: {proc.stderr.strip()[:300]}", file_bytes=size)

    try:
        info = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return VerifyResult(False, "ffprobe returned invalid JSON", file_bytes=size)

    streams = info.get("streams") or []
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not audio:
        return VerifyResult(False, "no audio stream", file_bytes=size)

    fmt = info.get("format") or {}
    actual_duration = None
    try:
        actual_duration = float(fmt.get("duration")) if fmt.get("duration") is not None else None
    except (TypeError, ValueError):
        actual_duration = None

    bitrate = None
    try:
        bitrate = int(float(fmt.get("bit_rate"))) if fmt.get("bit_rate") else None
    except (TypeError, ValueError):
        bitrate = None

    if expected_duration:
        expected = float(expected_duration)
        tolerance = max(float(tolerance_seconds), expected * float(tolerance_percent))
        if actual_duration is not None and abs(actual_duration - expected) > tolerance:
            return VerifyResult(
                False,
                f"duration mismatch: expected {expected:.1f}s, got {actual_duration:.1f}s",
                codec=audio.get("codec_name"),
                bitrate=bitrate,
                actual_duration=actual_duration,
                file_bytes=size,
            )

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return VerifyResult(False, "ffmpeg not found", file_bytes=size)

    try:
        probe = _run(
            [ffmpeg, "-v", "error", "-i", str(file_path), "-t", "10", "-f", "null", "-"],
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(
            False,
            "ffmpeg decode check timed out",
            codec=audio.get("codec_name"),
            bitrate=bitrate,
            actual_duration=actual_duration,
            file_bytes=size,
        )

    if probe.returncode != 0 or probe.stderr.strip():
        return VerifyResult(
            False,
            f"decode check failed: {probe.stderr.strip()[:300]}",
            codec=audio.get("codec_name"),
            bitrate=bitrate,
            actual_duration=actual_duration,
            file_bytes=size,
        )

    return VerifyResult(
        True,
        None,
        codec=audio.get("codec_name"),
        bitrate=bitrate,
        actual_duration=actual_duration,
        file_bytes=size,
        sha256=_sha256(file_path),
    )


def quarantine_file(path: str | Path, quarantine_dir: str | Path, reason: str = "") -> Path:
    src = Path(path)
    qdir = Path(quarantine_dir)
    qdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = qdir / f"{stamp}_{src.name}"
    shutil.move(str(src), str(dest))
    if reason:
        dest.with_suffix(dest.suffix + ".reason.txt").write_text(reason + "\n", encoding="utf-8")
    return dest
