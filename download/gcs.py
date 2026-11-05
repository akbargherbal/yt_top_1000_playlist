from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


class GcsError(RuntimeError):
    pass


def _md5_base64(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode("ascii")


class Gcs:
    def __init__(self, prefix: str, gcloud: str | None = None):
        self.prefix = prefix.rstrip("/")
        self.gcloud = gcloud or shutil.which("gcloud") or "gcloud"
        if not (shutil.which(self.gcloud) or Path(self.gcloud).exists()):
            raise GcsError("gcloud not found on PATH")

    def _run(self, args: list[str], *, retries: int = 3, timeout: int = 300) -> subprocess.CompletedProcess:
        last: subprocess.CompletedProcess | None = None
        for attempt in range(retries):
            proc = subprocess.run(
                [self.gcloud, "--quiet", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
            )
            if proc.returncode == 0:
                return proc
            last = proc
            text = (proc.stderr or proc.stdout or "").lower()
            if any(
                marker in text
                for marker in (
                    "permission denied",
                    "access denied",
                    "does not have storage",
                    "not authenticated",
                    "no credentials",
                    "invalid credentials",
                )
            ):
                break
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
        assert last is not None
        raise GcsError((last.stderr or last.stdout or "gcloud failed").strip()[:1000])

    def cp(self, local: str | Path, remote: str) -> None:
        self._run(["storage", "cp", str(local), remote])

    def cp_recursive(self, local: str | Path, remote: str) -> None:
        self._run(["storage", "cp", "-r", str(local), remote])

    def download(self, remote: str, local: str | Path) -> None:
        dest = Path(local)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run(["storage", "cp", remote, str(dest)])

    def describe(self, uri: str) -> dict[str, Any] | None:
        proc = subprocess.run(
            [self.gcloud, "--quiet", "storage", "objects", "describe", uri, "--format=json"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
        )
        if proc.returncode != 0:
            text = (proc.stderr or proc.stdout or "").lower()
            if any(
                marker in text
                for marker in ("not found", "404", "no urls matched", "does not exist")
            ):
                return None
            raise GcsError((proc.stderr or proc.stdout or "gcloud describe failed").strip()[:1000])
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise GcsError(f"Invalid describe output for {uri}: {exc}") from exc

    def rm(self, uri: str) -> None:
        self._run(["storage", "rm", uri])

    def verify_object(self, local: str | Path, remote: str) -> tuple[bool, str]:
        local_path = Path(local)
        info = self.describe(remote)
        if info is None:
            return False, "object not found"
        try:
            remote_size = int(info.get("size", -1))
        except (TypeError, ValueError):
            remote_size = -1
        if remote_size != local_path.stat().st_size:
            return False, f"size mismatch: local={local_path.stat().st_size} remote={remote_size}"

        remote_md5 = info.get("md5_hash")
        if remote_md5:
            local_md5 = _md5_base64(local_path)
            if remote_md5 != local_md5:
                return False, "md5 mismatch"
        return True, "ok"
