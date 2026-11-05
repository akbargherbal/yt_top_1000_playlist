from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import pandas as pd

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from download.config import load_config
from download.state import Manifest, utc_now

REQUIRED_COLUMNS = {"artist", "source_rank", "country", "tier", "songs", "RANK"}


def _track_id(video_id: str) -> str:
    return hashlib.sha1(video_id.encode("utf-8")).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(pickle_path: Path, config, source: str | None = None) -> dict:
    df = pd.read_pickle(pickle_path)
    if not isinstance(df, pd.DataFrame):
        raise ValueError(f"{pickle_path} does not contain a pandas DataFrame")

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Pickle is missing columns: {sorted(missing)}")

    video_ids = df["songs"].map(lambda song: song.get("video_id"))
    if video_ids.isna().any():
        raise ValueError("One or more rows have no songs.video_id")

    duplicates = video_ids[video_ids.duplicated()].tolist()
    if duplicates:
        raise ValueError(
            "The pickle still contains duplicate video_id rows: "
            f"{sorted(set(duplicates))}. It must be pre-deduplicated."
        )

    tracks = []
    for _, row in df.iterrows():
        song = row["songs"]
        video_id = str(song["video_id"])
        tracks.append(
            {
                "track_id": _track_id(video_id),
                "video_id": video_id,
                "rank": int(row["RANK"]),
                "source_rank": int(row["source_rank"]) if pd.notna(row["source_rank"]) else None,
                "artist": str(row["artist"]),
                "title": str(song.get("title") or ""),
                "youtube_title": str(song.get("youtube_title") or ""),
                "country": str(row["country"]) if pd.notna(row["country"]) else None,
                "tier": str(row["tier"]) if pd.notna(row["tier"]) else None,
                "duration_seconds": int(song["duration_seconds"])
                if song.get("duration_seconds") is not None
                else None,
                "url": str(song.get("url") or f"https://www.youtube.com/watch?v={video_id}"),
                "status": "pending",
                "attempt_count": 0,
                "upload_attempt_count": 0,
                "last_attempt_utc": None,
                "next_retry_utc": None,
                "error_class": None,
                "error_message": None,
                "upload_error": None,
                "local_path": None,
                "file_ext": None,
                "file_bytes": None,
                "sha256": None,
                "codec": None,
                "bitrate": None,
                "actual_duration": None,
                "gcs_uri": None,
                "info_json_uri": None,
                "uploaded_utc": None,
                "verified_utc": None,
                "local_deleted_utc": None,
                "notes": None,
                "updated_utc": utc_now(),
            }
        )

    tracks.sort(key=lambda track: track["rank"])
    return {
        "generated_utc": utc_now(),
        "dataset": {
            "source": source or pickle_path.name,
            "path": str(pickle_path.resolve()),
            "rows": len(tracks),
            "unique_video_ids": len({track["video_id"] for track in tracks}),
            "source_pickle_sha256": _sha256_file(pickle_path),
        },
        "config": {
            "gcs_prefix": config.gcs_prefix,
            "upload_interval_minutes": config.get("upload_interval_minutes"),
            "preferred_format": config.get("preferred_format"),
        },
        "tracks": tracks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the JSON download manifest from top_500_songs.pkl")
    parser.add_argument("--pickle", default="top_500_songs.pkl", help="Source pickle")
    parser.add_argument("--manifest", default=None, help="Output manifest path")
    parser.add_argument("--config", default=None, help="Config file (used for paths/GCS prefix)")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing manifest (keeps .bak)")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    pickle_path = Path(args.pickle).expanduser().resolve()
    if not pickle_path.exists():
        print(f"ERROR: pickle not found: {pickle_path}", file=sys.stderr)
        return 1

    manifest_path = Path(args.manifest).expanduser() if args.manifest else config.manifest_path
    if manifest_path.exists() and not args.force and not args.dry_run:
        print(
            f"ERROR: manifest already exists: {manifest_path}\n"
            "Use --force to rebuild it (the old file is copied to .bak).",
            file=sys.stderr,
        )
        return 1

    try:
        data = build_manifest(pickle_path, config)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    counts = {}
    for track in data["tracks"]:
        counts[track["tier"]] = counts.get(track["tier"], 0) + 1

    print(f"Pickle:   {pickle_path}")
    print(f"Rows:     {len(data['tracks'])}")
    print(f"Unique:   {data['dataset']['unique_video_ids']}")
    print(f"Tiers:    {dict(sorted(counts.items()))}")
    print(f"Manifest: {manifest_path}")

    if args.dry_run:
        print("Dry run: nothing written.")
        return 0

    manifest = Manifest.create(manifest_path, data)
    manifest.save()
    manifest.append_event(
        "import",
        rows=len(data["tracks"]),
        pickle=str(pickle_path),
        manifest=str(manifest_path),
    )
    print("Manifest written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
