from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from download.state import Manifest, utc_now

CSV_FIELDS = [
    "track_id",
    "rank",
    "artist",
    "title",
    "video_id",
    "tier",
    "country",
    "duration_seconds",
    "status",
    "attempt_count",
    "last_attempt_utc",
    "next_retry_utc",
    "error_class",
    "error_message",
    "file_bytes",
    "actual_duration",
    "codec",
    "bitrate",
    "local_path",
    "gcs_uri",
    "uploaded_utc",
    "verified_utc",
    "notes",
]


def build_report(manifest: Manifest) -> dict[str, Any]:
    tracks = sorted(manifest.tracks, key=lambda t: int(t.get("rank", 0)))
    total_bytes = sum(int(t.get("file_bytes") or 0) for t in tracks)
    total_duration = sum(float(t.get("duration_seconds") or 0) for t in tracks)
    done_bytes = sum(
        int(t.get("file_bytes") or 0) for t in tracks if t.get("status") == "done"
    )

    return {
        "generated_utc": utc_now(),
        "total_tracks": len(tracks),
        "status_counts": manifest.status_counts(),
        "tier_counts": manifest.tier_counts(),
        "error_counts": manifest.error_counts(),
        "total_expected_duration_seconds": round(total_duration, 1),
        "total_expected_hours": round(total_duration / 3600, 2),
        "bytes_local": total_bytes,
        "bytes_done": done_bytes,
        "tracks": [
            {
                "track_id": t.get("track_id"),
                "rank": t.get("rank"),
                "artist": t.get("artist"),
                "title": t.get("title"),
                "video_id": t.get("video_id"),
                "tier": t.get("tier"),
                "status": t.get("status"),
                "attempt_count": t.get("attempt_count"),
                "error_class": t.get("error_class"),
                "file_bytes": t.get("file_bytes"),
                "gcs_uri": t.get("gcs_uri"),
            }
            for t in tracks
        ],
    }


def write_csv(manifest: Manifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for track in sorted(manifest.tracks, key=lambda t: int(t.get("rank", 0))):
            writer.writerow({field: track.get(field) for field in CSV_FIELDS})


def write_json(manifest: Manifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_report(manifest)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate(
    manifest: Manifest,
    out_dir: str | Path,
    formats: tuple[str, ...] = ("csv", "json"),
) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    if "csv" in formats:
        paths["csv"] = out / "report.csv"
        write_csv(manifest, paths["csv"])
    if "json" in formats:
        paths["json"] = out / "report.json"
        write_json(manifest, paths["json"])
    return paths


def main(argv: list[str] | None = None) -> int:
    import argparse

    from download.config import load_config

    parser = argparse.ArgumentParser(description="Generate CSV/JSON reports from the manifest")
    parser.add_argument("--manifest", default=None, help="Manifest path")
    parser.add_argument("--config", default=None, help="Config file")
    parser.add_argument("--out", default=None, help="Output directory")
    parser.add_argument("--format", default="csv,json", help="csv, json, or csv,json")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    manifest_path = Path(args.manifest).expanduser() if args.manifest else config.manifest_path
    manifest = Manifest.load(manifest_path)
    formats = tuple(part.strip() for part in args.format.split(",") if part.strip())
    out_dir = Path(args.out).expanduser() if args.out else config.reports_dir
    paths = generate(manifest, out_dir, formats=formats)

    summary = build_report(manifest)
    print(f"Tracks: {summary['total_tracks']}")
    print(f"Status: {summary['status_counts']}")
    print(f"Errors: {summary['error_counts']}")
    for kind, path in paths.items():
        print(f"{kind.upper()}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
