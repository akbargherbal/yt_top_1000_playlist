# YouTube Top 500 Audio Downloader

Resumable downloader for the 495 unique tracks in `top_500_songs.pkl`.
Downloads audio with `yt-dlp`, validates it with `ffprobe`/`ffmpeg`, uploads
completed tracks and state snapshots to GCS, and keeps a JSON manifest so a
later VM session continues where the previous one stopped.

**Destination:** `gs://akbar-december-2024-backup/yt_top_500_music_playlist/`

## Setup

```bash
cd /content/yt_top_1000_playlist

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U -r requirements.txt
```

`ffmpeg`, `ffprobe`, `node`, and `gcloud` must be on `PATH`.

Recent `yt-dlp` releases need the EJS challenge solver. The config already sets:

```yaml
js_runtimes: node
remote_components: "ejs:github"
```

## Cookies

Keep the cookie jar **outside this repository**, for example:

```text
/content/www.youtube.com_cookies.txt
```

Set its absolute path in `download/config.yaml`:

```yaml
cookies_path: /content/www.youtube.com_cookies.txt
```

or override at runtime with `--cookies /path/to/cookies.txt` or
`COOKIES_PATH=/path/to/cookies.txt`.

The file is equivalent to your logged-in YouTube session. Keep it mode `600`.

## Initialize the manifest

The pickle is already deduplicated; the importer does not drop rows again.

```bash
python download/import_songs.py --config download/config.yaml
```

Expected: **495 tracks**.

## Run

```bash
# Inspect the plan without downloading
python download/run_session.py --config download/config.yaml --dry-run

# 5-track pilot, no upload
python download/run_session.py --config download/config.yaml \
  --max-tracks 5 --no-upload

# Normal session: 50 tracks or 120 minutes
python download/run_session.py --config download/config.yaml \
  --max-tracks 50 --max-minutes 120

# Retry failed/blocked tracks, ignoring backoff
python download/run_session.py --config download/config.yaml --retry-failed

# One specific video
python download/run_session.py --config download/config.yaml \
  --video-id fbK2ySlxAW4 --max-tracks 1
```

Resume is automatic: run the same command again. A lock prevents two sessions
from running at once.

## Main flags

| Flag | Meaning |
|---|---|
| `--config PATH` | Config file |
| `--cookies PATH` | Override `cookies_path` |
| `--max-tracks N` | Max tracks this session |
| `--max-minutes N` | Time budget |
| `--delay-min SEC` / `--delay-max SEC` | Inter-track delay |
| `--upload-interval MIN` | Periodic upload interval (default 5) |
| `--ranks RANGE` | e.g. `1-50` or `1,5,10-20` |
| `--video-id ID` | One specific YouTube video |
| `--retry-failed` | Retry failed/blocked now, ignore backoff |
| `--no-upload` | Download/verify only |
| `--no-download` | Reconcile/upload only |
| `--dry-run` | Show selection, do nothing |
| `--reconcile-only` | Check state and exit |
| `--log-level LEVEL` | `debug`, `info`, `warning`, `error` |

Precedence: **CLI flag > environment variable > config file**.

## Upload, reconcile, report

```bash
# Force an upload tick
python download/uploader.py --config download/config.yaml --once --force

# Verify existing GCS objects
python download/uploader.py --config download/config.yaml --verify-only

# Reconcile stale rows/local files/GCS
python download/reconcile.py --config download/config.yaml --check-gcs --fix

# Generate reports
python download/report.py --config download/config.yaml
```

Restore state on a fresh VM:

```bash
python download/reconcile.py --config download/config.yaml --from-gcs --fix
```

## State and GCS layout

```text
data/
├── manifest.json          # authoritative state
├── manifest.json.bak
├── events.jsonl           # append-only audit log
├── runs.jsonl
├── staging/               # downloaded audio
├── quarantine/            # rejected files
└── logs/

reports/
├── report.csv
└── report.json
```

```text
gs://akbar-december-2024-backup/yt_top_500_music_playlist/
├── audio/
├── metadata/
├── state/latest/
│   ├── manifest.json
│   ├── manifest.json.bak
│   ├── report.csv
│   ├── report.json
│   └── meta/
├── state/snapshots/<UTC timestamp>/
└── logs/
```

## Behaviour

- One track at a time, 10–25 second jittered delay by default.
- `downloaded` → uploaded on the next 5-minute tick, or immediately when the
  pending count reaches 10, or at session stop.
- `done` is set only after the GCS object size/hash is verified.
- A blocked request stops the session and sets a cooldown; it does not retry in
  a tight loop.
- Local files are kept after upload by default.
