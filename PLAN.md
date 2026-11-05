# Plan: Download the Top 500 Songs as Audio

**Source:** `top_500_songs.pkl`
**Destination:** `gs://akbar-december-2024-backup/yt_top_500_music_playlist/`
**Local working copy:** `/content/yt_top_1000_playlist`
**Status:** Plan only — no downloads have been started.

---

## 1. Executive summary

Build a small, resumable download pipeline around `yt-dlp` that:

1. Imports the **pre-deduplicated** pickle: **495 unique videos**, no runtime dedupe.
2. Downloads sequentially with conservative rate limits.
3. Validates every file locally with `ffprobe`/`ffmpeg` before accepting it.
4. Uploads pending tracks to GCS **periodically every 5 minutes** (configurable), not after every track.
5. Maintains a **JSON manifest** plus an append-only event log; both are snapshotted to GCS on the same periodic cycle.
6. On the next VM session, restores the manifest, reconciles local/GCS state, and continues where it stopped.

Expected volume: about **0.5–1.5 GB** total (38.8 hours of audio at typical YouTube bitrates), so local disk is not a constraint. Expected runtime: roughly **3–6 sessions of 60–120 minutes**, depending on bot-check frequency and download speed.

---

## 2. Dataset audit

| Item | Value |
|---|---|
| Rows in current pickle | **495** |
| Original source rows | 500 |
| Duplicate rows removed when the pickle was produced | 5 |
| Tracks to download | **495** |
| Total expected duration | 38.8 hours |
| Tiers | Tier 1: 351, Tier 2: 136, Tier 3: 13 |
| Countries | Lebanon, Iraq, Saudi Arabia, Egypt, Syria, others |
| Required song fields | `title`, `youtube_title`, `video_id`, `url`, `duration_seconds` |

The dataframe columns are `artist`, `source_rank`, `country`, `tier`, `song_count`, `songs`, `RANK`. The `songs` column is a dict containing the video metadata above.

**Deduplication is already done:** the pickle was deduplicated once by `video_id`, keeping the lowest `RANK` (and the first row on ties). That is why it now contains **495 rows**. The downloader must **not** deduplicate again; every row in the pickle is a unique track.

---

## 3. Assumptions and constraints

- The account is already authenticated to GCS, and the destination prefix currently exists but is empty.
- `gcloud storage` works without a default project. If `gsutil` is used as a fallback, set `CLOUDSDK_CORE_PROJECT` to the bucket-owning project (`orange-migration` per the bucket IAM policy).
- The VM is a datacenter IP, so YouTube may return “Sign in to confirm you’re not a bot”, `403`, or `429`. These must be treated as **stop-and-cool-down** conditions, not retried aggressively.
- Downloads are for a personal archive only. Do not redistribute the files; check that you have the rights to download this content in your jurisdiction, and note that bulk downloading may conflict with YouTube’s Terms of Service.
- Default output preserves the original audio container/codec (`m4a` or `webm`/Opus). No lossy re-encode.
- One download worker only. No parallel downloads.
- Optional but strongly recommended: a Netscape-format `cookies.txt` exported from a logged-in browser. Without it, a datacenter IP will likely be blocked quickly.

### 3.1 Can cookies be generated locally and used on the VM?

**Yes.** YouTube cookies are not tied to the machine that created them. A Netscape-format `cookies.txt` exported from your local browser can be copied to the VM and passed to `yt-dlp` with `--cookies`.

Caveats:

- The cookies must still be valid and the account must remain logged in.
- Google may treat a login from a new datacenter IP as suspicious even with valid cookies. Cookies greatly improve the odds, but they do not guarantee that YouTube will not show a bot check.
- Cookies are equivalent to your logged-in session. Treat the file like a password.
- Export all `youtube.com` cookies, including `HttpOnly` cookies. `document.cookie` in DevTools will not show those.
- If YouTube still asks for a login confirmation, re-export fresh cookies, verify the VM clock is correct, and try `--impersonate chrome`.

#### Export on your local computer

1. Log in to YouTube in a normal (non-incognito) browser window and open `https://www.youtube.com`.
2. Export cookies for `youtube.com` in **Netscape** format:
   - Chrome/Edge: the open-source extension **“Get cookies.txt LOCALLY”**.
   - Firefox: an extension such as **“Export Cookies”** that writes Netscape format.
3. Save the file as `cookies.txt`.
4. Confirm the first line is:
   ```text
   # Netscape HTTP Cookie File
   ```
5. Do not edit the file by hand. Keep the `#HttpOnly_` prefixes.

If login confirmation persists, export cookies for both `youtube.com` and `google.com` into the same file. Do not include unrelated sites.

#### Transfer to the VM

Store the cookie jar **outside the repository**. For example, use
`/content/www.youtube.com_cookies.txt`; the exact absolute path is what goes
into `cookies_path` in `download/config.yaml`.

Preferred, if SSH is available:

```bash
scp ~/Downloads/cookies.txt user@YOUR_VM:/content/www.youtube.com_cookies.txt
```

If SSH is not available, use a short-lived GCS object and delete it immediately:

```bash
# On your local computer
gcloud storage cp ~/Downloads/cookies.txt \
  gs://akbar-december-2024-backup/yt_top_500_music_playlist/tmp/cookies.txt

# On the VM
mkdir -p /content
gcloud storage cp \
  gs://akbar-december-2024-backup/yt_top_500_music_playlist/tmp/cookies.txt \
  /content/www.youtube.com_cookies.txt
chmod 600 /content/www.youtube.com_cookies.txt

# Remove the GCS copy as soon as the VM has it
gcloud storage rm \
  gs://akbar-december-2024-backup/yt_top_500_music_playlist/tmp/cookies.txt
```

#### Configure the path

Set the absolute path in `download/config.yaml`:

```yaml
cookies_path: /content/www.youtube.com_cookies.txt
```

Or override it at runtime:

```bash
COOKIES_PATH=/content/www.youtube.com_cookies.txt python download/run_session.py
```

The downloader should pass `--cookies "$COOKIES_PATH"` to `yt-dlp`. Do not copy
the cookie file into the repo and do not hard-code a repo-relative path.

#### Verify on the VM

Check that the file parses and contains YouTube cookies without printing their values:

```bash
COOKIES_PATH=/content/www.youtube.com_cookies.txt
head -1 "$COOKIES_PATH"
awk -F'\t' '!/^#/ {print $1, $6}' "$COOKIES_PATH" | sort -u | head -30
stat -c '%a %n' "$COOKIES_PATH"      # should be 600
date -u                              # VM clock must be correct
```

Then test metadata extraction only:

```bash
yt-dlp --cookies "$COOKIES_PATH" --skip-download --print "%(id)s %(title)s" \
  "https://www.youtube.com/watch?v=fbK2ySlxAW4"
```

If that succeeds, run the 5-track pilot. If it fails with “Sign in to confirm you’re not a bot”, do **not** retry in a loop; refresh the cookies and wait before trying again.

#### Security rules

- Keep the cookie jar outside the repository; `.gitignore` contains `*cookies*`
  as a safety net, but that is not a substitute for storing it elsewhere.
- Keep it at mode `600`.
- Delete or rotate it after the project, and re-export rather than sharing it.
- Do not paste cookie contents into chat, logs, or issue reports.
- If you sign out all devices or change the Google password, the cookies become invalid.

---

## 4. Architecture

```text
top_500_songs.pkl
        │
        ▼
┌──────────────────┐
│ import_songs.py  │  load rows (already deduplicated in pickle)
└────────┬─────────┘
         ▼
┌──────────────────────────────┐
│ data/manifest.json           │  authoritative state
│ data/events.jsonl            │  append-only audit trail
└────────┬─────────────────────┘
         ▼
┌──────────────────┐    ┌──────────────────┐
│ run_session.py   │───▶│ yt-dlp (1 track) │
│ claim next track │    └────────┬─────────┘
└────────┬─────────┘             ▼
         │              ┌──────────────────┐
         │              │ verify.py        │ ffprobe, duration, decode, SHA-256
         │              └────────┬─────────┘
         │                       ▼
         │              ┌──────────────────┐
         │              │ local staging/   │
         │              └────────┬─────────┘
         │                       ▼
         │         every N minutes (default 5)
         │              ┌──────────────────┐
         └─────────────▶│ uploader.py      │ upload pending + snapshot state to GCS
                        └──────────────────┘
```

The JSON manifest is the source of truth. GCS is both the backup and the durable copy of completed tracks. Local files are a staging area, not the only copy.

---

## 5. Proposed repository layout

```text
/content/yt_top_1000_playlist/
├── PLAN.md
├── top_500_songs.pkl
├── download/
│   ├── config.yaml
│   ├── import_songs.py
│   ├── run_session.py
│   ├── reconcile.py
│   ├── verify.py
│   ├── uploader.py
│   ├── state.py
│   ├── report.py
│   ├── backup_gcs.sh
│   └── yt-dlp.conf
├── data/
│   ├── manifest.json
│   ├── manifest.json.bak
│   ├── events.jsonl
│   ├── runs.jsonl
│   ├── staging/
│   ├── quarantine/
│   └── logs/
└── reports/
    ├── report.csv
    └── report.json
```

The cookie jar is **not** stored in this tree. Keep it at an absolute path
outside the repo, for example:

```text
/content/www.youtube.com_cookies.txt
```

Add to `.gitignore`:

```gitignore
data/
reports/
*.part
*cookies*
```

---

## 6. Why JSON instead of SQLite?

For this workload, **JSON is the better fit**. The earlier SQLite proposal was over-engineered.

| Requirement | JSON | SQLite |
|---|---|---|
| ~495 records | Easy | Easy |
| Single writer, sequential downloads | Fine | Fine |
| Human inspection / manual fixes | Excellent | Needs `sqlite3` or a GUI |
| Diffing and versioning | Easy | Binary |
| Dependencies | None | `sqlite3` module |
| Atomic updates | `tmp` + `fsync` + `os.replace` | Built-in transactions |
| Indexed queries | Unnecessary | Useful at large scale |
| Concurrent writers | Not supported | Built-in |
| Partial updates | Full rewrite (~0.5 MB) | Row-level |

JSON is sufficient because:

- The dataset is tiny; a full rewrite takes milliseconds.
- There is only one writer process (enforced by a lock file).
- The important queries are simple: “next pending track”, “tracks due for retry”, “downloaded but not uploaded”.
- A human-readable manifest is valuable for debugging failed tracks and manually correcting metadata.
- A corrupt JSON file is easier to inspect and repair than a corrupt database.

SQLite would become worthwhile if the project grows to tens of thousands of tracks, needs multiple concurrent workers, or needs complex indexed queries. That is not the case here.

### 6.1 Crash-safe JSON writes

Every manifest update must use an atomic write:

1. Write the new JSON to `data/manifest.json.tmp`.
2. Flush and `os.fsync()` the file.
3. Copy the previous `manifest.json` to `manifest.json.bak`.
4. `os.replace(tmp, manifest.json)`.
5. `os.fsync()` the directory.
6. Validate that the new file parses as JSON.

A crash can therefore leave either the old valid manifest, the new valid manifest, or a temporary file — never a half-written manifest.

The append-only `events.jsonl` log provides an audit trail and a second recovery source. Each line is one event, flushed after writing:

```json
{"ts":"2026-11-05T15:00:00Z","event":"attempt_started","track_id":"...","run_id":1}
{"ts":"2026-11-05T15:00:25Z","event":"download_ok","track_id":"...","bytes":4213370,"sha256":"..."}
{"ts":"2026-11-05T15:00:25Z","event":"verify_ok","track_id":"...","duration_actual":317.2}
{"ts":"2026-11-05T15:05:00Z","event":"upload_ok","track_id":"...","gcs_uri":"gs://..."}
```

`runs.jsonl` records one summary object per session.

### 6.2 Manifest structure

```json
{
  "schema_version": 1,
  "generated_utc": "2026-11-05T15:00:00Z",
  "dataset": {
    "source": "top_500_songs.pkl",
    "rows": 495,
    "unique_video_ids": 495,
    "duplicates_removed_before_import": 5
  },
  "config": {
    "gcs_prefix": "gs://akbar-december-2024-backup/yt_top_500_music_playlist",
    "upload_interval_minutes": 5
  },
  "tracks": [
    {
      "track_id": "a1b2c3d4e5f60708",
      "video_id": "fbK2ySlxAW4",
      "rank": 1,
      "source_rank": 2,
      "artist": "Elissa",
      "title": "Maktooba Leek",
      "youtube_title": "Elissa - Maktooba Leek | Lyrics Video",
      "country": "Lebanon",
      "tier": "Tier 1",
      "duration_seconds": 317,
      "url": "https://www.youtube.com/watch?v=fbK2ySlxAW4",

      "status": "pending",
      "attempt_count": 0,
      "upload_attempt_count": 0,
      "last_attempt_utc": null,
      "next_retry_utc": null,
      "error_class": null,
      "error_message": null,
      "upload_error": null,

      "local_path": null,
      "file_ext": null,
      "file_bytes": null,
      "sha256": null,
      "codec": null,
      "bitrate": null,
      "actual_duration": null,

      "gcs_uri": null,
      "info_json_uri": null,
      "uploaded_utc": null,
      "verified_utc": null,
      "local_deleted_utc": null,

      "notes": null,
      "updated_utc": "2026-11-05T15:00:00Z"
    }
  ]
}
```

### 6.3 Status model

```text
pending ──▶ downloading ──▶ downloaded ──▶ uploaded ──▶ done
                  │
                  ├──▶ failed_retryable ──▶ (backoff) ──▶ pending
                  ├──▶ blocked ──▶ (cooldown / cookie refresh) ──▶ pending
                  ├──▶ unavailable        (terminal; no automatic retry)
                  ├──▶ quarantined        (file failed validation)
                  └──▶ skipped            (duplicate / excluded)
```

- `downloaded` means the local file passed validation but has not yet been uploaded.
- `uploaded` means the GCS upload command succeeded but verification has not yet passed.
- `done` is only set after the GCS object has been verified.
- If an upload fails, the track stays `downloaded` with `upload_error` and is retried on the next upload cycle.

---

## 7. Download, throttling, and upload scheduling

### 7.1 Base configuration

```yaml
# download/config.yaml
gcs_prefix: gs://akbar-december-2024-backup/yt_top_500_music_playlist
local_root: /content/yt_top_1000_playlist

# Absolute path to a cookie jar kept OUTSIDE this repository.
# Can be overridden with the COOKIES_PATH environment variable.
cookies_path: /content/www.youtube.com_cookies.txt

max_tracks_per_session: 50
max_minutes_per_session: 120
daily_attempt_cap: 200

delay_min_seconds: 10
delay_max_seconds: 25
rate_limit: 3M

retries: 3
fragment_retries: 3
extractor_retries: 2
file_access_retries: 3

max_consecutive_blocks: 2
cooldown_after_block_minutes: 45
cooldown_after_rate_limit_minutes: 15
max_attempts_per_track: 5

# Periodic upload / state snapshot
upload_interval_minutes: 5
upload_on_stop: true
upload_max_pending_files: 10
snapshot_interval_minutes: 5

preferred_format: "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"
```

### 7.2 One-track yt-dlp invocation

The orchestrator runs `yt-dlp` for exactly one video at a time. That makes progress tracking, error classification, and backoff straightforward.

```bash
COOKIES_PATH="${COOKIES_PATH:-/content/www.youtube.com_cookies.txt}"

yt-dlp \
  --no-playlist \
  --cookies "$COOKIES_PATH" \
  --js-runtimes node \
  --format "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio" \
  --output "/content/yt_top_1000_playlist/data/staging/%(id)s.%(ext)s" \
  --write-info-json \
  --write-thumbnail \
  --no-mtime \
  --retries 3 \
  --fragment-retries 3 \
  --extractor-retries 2 \
  --file-access-retries 3 \
  --concurrent-fragments 1 \
  --limit-rate 3M \
  --sleep-requests 1.5 \
  --download-archive /content/yt_top_1000_playlist/data/ytdlp-archive.txt \
  --print after_move:filepath \
  "https://www.youtube.com/watch?v=VIDEO_ID"
```

Notes:

- `--sleep-interval` is not used as the primary throttle because each process downloads only one track. The orchestrator sleeps **between** tracks with random jitter.
- `--download-archive` is a second line of defence against duplicate downloads. The JSON manifest remains authoritative.
- `--js-runtimes node` is included because modern `yt-dlp` YouTube extraction often needs a JavaScript runtime; Node 22 is already present on this VM.
- If bot checks persist, add `--impersonate chrome` (requires `curl_cffi`) and/or a fresher cookies file.

### 7.3 Inter-track pacing and adaptive backoff

- Wait **10–25 seconds** (random uniform, ±jitter) between tracks.
- Never run more than one `yt-dlp` process at a time.
- On the first `403`/`429` or bot-check response:
  - mark the current track `blocked` with `next_retry_utc = now + 15 min`;
  - stop the session for at least 15 minutes;
  - retry one track as a probe before resuming.
- On the second block within the same session, or two consecutive blocked probes:
  - stop the session;
  - set a **45–60 minute** cooldown and halve the next session’s track budget;
  - require a fresh `cookies.txt` before the next attempt.
- If success rate over the last 20 attempts is below 70%, stop and investigate.
- Daily cap of 200 attempts, persisted in `runs.jsonl` / manifest; do not reset it by restarting the process.
- Log every attempt with timestamp, video ID, outcome, HTTP status, and yt-dlp version.

### 7.4 Error classification

| Condition | Class | Action |
|---|---|---|
| `429`, `403`, bot check, “Sign in to confirm” | `blocked` | Cool down, refresh cookies, stop session on repeat |
| Timeout, connection reset, `5xx` | `failed_retryable` | Retry with exponential backoff |
| `Video unavailable`, private, removed, members-only | `unavailable` | Terminal, no automatic retry |
| Copyright/geo block | `unavailable` or `blocked` | Record exact message; do not bypass aggressively |
| File exists but fails `ffprobe`/duration/decode | `quarantined` | Move to `data/quarantine/`, retry once from scratch |
| GCS upload/verification failure | stays `downloaded` | Keep local file, retry on next upload cycle |

Backoff schedule for retryable failures: **15 min → 1 h → 6 h → 24 h**, with ±20% jitter, capped at 5 attempts. After 5 attempts, mark `failed_retryable` with a note for manual review.

---

## 8. Periodic upload behaviour

Uploads are **batched on a timer**, not per track.

At each upload tick (default every 5 minutes):

1. Select tracks with `status == "downloaded"` and an existing local file.
2. For each:
   - upload audio to `audio/`;
   - upload the `.info.json` sidecar to `metadata/`;
   - verify the GCS object’s size (and hash where available);
   - mark `uploaded`, then `done`.
3. Write the updated `manifest.json` atomically.
4. Upload the manifest snapshot and report to `state/latest/`.
5. Create a timestamped snapshot under `state/snapshots/<UTC timestamp>/`.
6. If an upload fails, leave the track `downloaded`, record `upload_error`, and retry next tick.

Additional forced ticks:

- at session start (to reconcile);
- at session end;
- on graceful shutdown / `SIGTERM`;
- when pending local files exceed `upload_max_pending_files` (default 10), even if 5 minutes have not elapsed;
- when a block/stop condition is detected.

The uploader runs in the same process and is triggered between tracks. If a single track takes longer than the interval, the upload runs immediately after that track finishes. This avoids a second writer racing the manifest.

---

## 9. Local validation

Before a track is marked `downloaded`, run:

```bash
ffprobe -v error -show_entries format=duration,size,bit_rate \
        -show_entries stream=codec_type,codec_name -of json FILE
```

Accept only if:

1. File exists and is larger than a small sanity threshold (for example 100 KB).
2. An audio stream is present.
3. Actual duration is within **5% or 15 seconds** (whichever is larger) of `duration_seconds` from the pickle.
4. A short decode check succeeds:

```bash
ffmpeg -v error -i FILE -t 10 -f null -
```

5. `sha256sum` is recorded in the manifest.
6. If any check fails, move the file to `data/quarantine/`, mark `quarantined`, and retry once with a fresh download.

---

## 10. GCS layout and backup strategy

```text
gs://akbar-december-2024-backup/yt_top_500_music_playlist/
├── audio/
│   ├── 0001_fbK2ySlxAW4.m4a
│   ├── 0002_<video_id>.m4a
│   └── ...
├── metadata/
│   ├── 0001_fbK2ySlxAW4.info.json
│   └── ...
├── state/
│   ├── latest/
│   │   ├── manifest.json
│   │   ├── manifest.json.bak
│   │   ├── report.csv
│   │   └── meta/
│   │       ├── PLAN.md
│   │       ├── config.yaml
│   │       └── *.py
│   └── snapshots/
│       └── 20261105T153000Z/
│           ├── manifest.json
│           ├── report.csv
│           └── events.jsonl
└── logs/
    └── session-20261105T150000Z.log
```

### Upload verification

After each upload:

```bash
gcloud storage objects describe gs://akbar-december-2024-backup/yt_top_500_music_playlist/audio/0001_fbK2ySlxAW4.m4a
```

Compare the object `size` with the local file size. Compare `md5_hash` where available. Only then mark `done`.

### Snapshot retention

Keep the last N timestamped snapshots (for example 90 days) or all of them; total state size is tiny compared with the audio. `state/latest/` is the quick-restore path.

### Restore procedure

On a fresh VM:

```bash
gcloud storage cp gs://akbar-december-2024-backup/yt_top_500_music_playlist/state/latest/manifest.json /tmp/manifest.json
cp /tmp/manifest.json data/manifest.json
gcloud storage cp -r gs://akbar-december-2024-backup/yt_top_500_music_playlist/state/latest/meta ./restored-meta
python download/reconcile.py --from-gcs
```

If `manifest.json` is corrupt, try the `.bak` copy. If both are unusable, rebuild the manifest from `top_500_songs.pkl` and `events.jsonl`, then reconcile against the GCS `audio/` listing.

`reconcile.py` should:

- reset stale `downloading` rows older than 20 minutes to `failed_retryable`;
- check every `uploaded`/`done` row against GCS (exists, size, hash);
- re-queue rows whose local file is missing but GCS object is also missing;
- detect local files not present in the manifest and hash-match them by video ID;
- remove orphaned `.part` files;
- print a summary before the session starts.

---

## 11. Session workflow

### Preflight (once per VM session)

1. Acquire the global lock (`flock data/manifest.lock`) so two sessions cannot run at once.
2. `pip install -U yt-dlp` and record the version.
3. Confirm `ffmpeg`, `ffprobe`, `node`, and `gcloud` are present.
4. Check that the absolute `cookies_path` from config exists, is fresh, and is mode `600` (outside the repo).
5. Confirm GCS write access with a tiny probe object under `state/probe/`.
6. Restore the manifest from GCS if it is missing locally.
7. Run `reconcile.py`.

### Main loop

```text
while within time/track budget:
    if upload_tick_due():
        run_uploader()
    row = claim_next_track()          # pending or due failed_retryable, ORDER BY rank
    if row is None:
        run_uploader(force=True)
        break
    mark downloading + create attempt event
    run yt-dlp for one video
    on success:
        verify local file
        mark downloaded
    on classified error:
        update status, error_class, next_retry_utc
    save manifest atomically
    if upload_tick_due():
        run_uploader()
    sleep jittered 10–25 seconds
```

`claim_next_track()` updates the manifest before launching `yt-dlp`, so a crash leaves the row in `downloading` for the next session to reconcile.

### Graceful stop

On `SIGINT`/`SIGTERM`:

- stop the current `yt-dlp` process;
- mark the current row `failed_retryable` (or `pending` if nothing was written);
- run the uploader once to flush pending files and snapshot the manifest;
- release the lock.

### End of session

1. Export `reports/report.csv` and `reports/report.json`.
2. Force a manifest snapshot to `state/snapshots/<timestamp>/` and update `state/latest/`.
3. Upload the session log.
4. Print: attempted, succeeded, failed, blocked, remaining, bytes, next recommended run time.

---

## 12. Recommended rollout

| Milestone | Work | Exit condition |
|---|---|---|
| 0. Preflight | Install/verify tools, cookies, GCS probe, lock file | `yt-dlp --version`, `ffprobe`, and GCS write probe succeed |
| 1. Manifest importer | Read the pre-deduped pickle (495 rows), write JSON | 495 rows → 495 tracks |
| 2. Pilot | Download **5 tracks** with full rate limiting and a 5-minute upload tick | At least 3 succeed; manifest restore works |
| 3. Calibration | Run **25–50 tracks**, measure median time/track and block rate | Success rate ≥ 80%; adjust delay if needed |
| 4. Bulk sessions | Repeated 60–120 minute sessions | `done` count grows; manifest restores cleanly each time |
| 5. Failure sweep | Retry `failed_retryable`; inspect `unavailable`/`quarantined` | Remaining failures have a documented reason |
| 6. Final audit | Verify every `done` row against GCS; export report | 495/495 accounted for, or explicit terminal reasons |

Do **not** start with all 495. The first 5-track pilot is the cheapest way to discover that the VM’s IP is blocked or that cookies are expired.

---

## 13. Acceptance criteria

- [ ] All **495 unique videos** are represented in the manifest with a terminal status.
- [ ] Every `done` track has a GCS object whose size (and hash where available) matches the manifest.
- [ ] No duplicate audio object is uploaded for the same `video_id`.
- [ ] A fresh VM can restore `state/latest/manifest.json` and continue without re-downloading completed tracks.
- [ ] Every failed track has `error_class`, `attempt_count`, `last_attempt_utc`, and a human-readable message.
- [ ] Uploads happen on the configured interval (default 5 minutes) and on session stop.
- [ ] Session logs show deliberate pacing and cooldowns; no unbounded retry loop.
- [ ] `report.csv` / `report.json` summarize done, failed, blocked, unavailable, quarantined, and remaining counts.

---

## 14. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Datacenter IP bot check | Downloads fail en masse | Cookies, `--impersonate`, conservative pacing, stop on first block |
| Cookies expire | Sudden 403s | Detect block class, stop session, refresh cookies, retry later |
| YouTube changes extraction | yt-dlp breaks | Update yt-dlp each session; record version per attempt |
| Video removed/private | Permanent gaps | Mark `unavailable`; report for manual search |
| VM crash mid-download | Stale `downloading` row | Startup reconciliation resets stale rows and removes `.part` files |
| Crash between 5-minute uploads | Up to 5 minutes of local-only work | Local files persist; forced upload on shutdown; configurable shorter interval |
| GCS upload failure | Local-only data | Keep local copy; retry on next upload tick; never mark `done` without verification |
| Duplicate video IDs | Wasted downloads | Pickle is pre-deduped; unique key in manifest; yt-dlp archive as second guard |
| Lossy re-encode | Quality loss | Preserve native container; transcode only as an optional later pass |
| Copyright/ToS | Legal/account risk | Personal archive only; no redistribution; respect blocks and rate limits |

---

## 15. Decisions to confirm before implementation

1. **Cookies:** Can you export a fresh Netscape `cookies.txt` from a logged-in YouTube browser session? This is the single biggest determinant of success on a VM.
2. **Format:** Keep native `m4a`/`webm` (recommended), or transcode everything to MP3/AAC?
3. **Local retention:** Keep local copies after GCS verification (recommended, disk is ample), or delete them once `done`?
4. **Snapshot retention:** Keep all `state/snapshots/` indefinitely, or expire after 30–90 days?

---

## 16. Planned CLI

The repository does **not** yet contain the `download/` scripts. This is the
interface to implement. All commands are run from the repository root:

```bash
cd /content/yt_top_1000_playlist
```

Recommended one-time setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U yt-dlp
```

`ffmpeg`/`ffprobe` remain system packages.

### 16.1 Initialize the manifest

```bash
python download/import_songs.py \
  --pickle top_500_songs.pkl \
  --manifest data/manifest.json
```

| Flag | Meaning |
|---|---|
| `--pickle PATH` | Source pickle. Default: `top_500_songs.pkl` |
| `--manifest PATH` | Output JSON manifest. Default: `data/manifest.json` |
| `--force` | Rebuild an existing manifest, after writing a `.bak` |
| `--dry-run` | Report counts without writing |

Expected result: **495 tracks loaded**. The pickle is already deduplicated, so
`import_songs.py` does not drop or merge rows.

### 16.2 Run a download session

```bash
python download/run_session.py --config download/config.yaml
```

Common examples:

```bash
# Pilot: only 5 tracks, no upload yet
python download/run_session.py --config download/config.yaml \
  --max-tracks 5 --no-upload

# Normal session: 50 tracks or 120 minutes, whichever comes first
python download/run_session.py --config download/config.yaml \
  --max-tracks 50 --max-minutes 120

# Debug one specific video
python download/run_session.py --config download/config.yaml \
  --video-id fbK2ySlxAW4 --max-tracks 1

# Only ranks 1–50
python download/run_session.py --config download/config.yaml \
  --ranks 1-50

# Retry tracks whose backoff has expired
python download/run_session.py --config download/config.yaml --retry-failed

# Show what would be attempted, without downloading
python download/run_session.py --config download/config.yaml --dry-run
```

| Flag | Meaning |
|---|---|
| `--config PATH` | Config file. Default: `download/config.yaml` |
| `--manifest PATH` | Override manifest path |
| `--cookies PATH` | Override `cookies_path` from config |
| `--gcs-prefix URI` | Override GCS destination |
| `--max-tracks N` | Maximum tracks this session. Default from config (50) |
| `--max-minutes N` | Time budget. Default from config (120) |
| `--delay-min SEC` | Minimum inter-track delay. Default 10 |
| `--delay-max SEC` | Maximum inter-track delay. Default 25 |
| `--upload-interval MIN` | Periodic upload interval. Default 5 |
| `--upload-max-pending N` | Force an upload after N pending files. Default 10 |
| `--ranks RANGE` | Restrict to ranks, e.g. `1-50` or `1,5,10-20` |
| `--video-id ID` | Download one specific YouTube video |
| `--retry-failed` | Include retryable failures whose `next_retry_utc` has passed |
| `--no-upload` | Download/verify only; skip GCS uploads |
| `--no-download` | Reconcile/upload only; do not download new tracks |
| `--dry-run` | Print the selected tracks and settings; download nothing |
| `--reconcile-only` | Check state and exit |
| `--log-level LEVEL` | `debug`, `info`, `warning`, or `error` |

Precedence: **CLI flag > environment variable > config file**.

Supported environment variables:

```bash
COOKIES_PATH=/content/www.youtube.com_cookies.txt
GCS_PREFIX=gs://akbar-december-2024-backup/yt_top_500_music_playlist
MAX_TRACKS=50
MAX_MINUTES=120
UPLOAD_INTERVAL_MINUTES=5
LOG_LEVEL=info
```

The session is **resumable by default**. Running the same command again after a
crash or VM restart restores/reconciles the manifest and continues with the next
eligible track. No `--resume` flag is needed.

### 16.3 Force an upload tick

```bash
python download/uploader.py --config download/config.yaml --once --force
```

| Flag | Meaning |
|---|---|
| `--config PATH` | Config file |
| `--once` | Run one upload cycle and exit (default) |
| `--watch` | Loop every `upload_interval_minutes` |
| `--force` | Upload all `downloaded` tracks, not only due ones |
| `--max-files N` | Limit files uploaded this cycle |
| `--verify-only` | Re-check GCS objects without uploading |

### 16.4 Reconcile before/after a session

```bash
python download/reconcile.py --config download/config.yaml --check-gcs --fix
```

| Flag | Meaning |
|---|---|
| `--from-gcs` | Restore `state/latest/manifest.json` first |
| `--check-gcs` | Verify `done`/`uploaded` objects against GCS |
| `--fix` | Apply repairs; without it, only report |
| `--reset-stale MIN` | Reset `downloading` rows older than N minutes. Default 20 |
| `--prune-partials` | Delete orphaned `.part` files |
| `--json` | Machine-readable output |

### 16.5 Generate a report

```bash
python download/report.py \
  --manifest data/manifest.json \
  --out reports \
  --format csv,json
```

| Flag | Meaning |
|---|---|
| `--manifest PATH` | Manifest to read |
| `--out DIR` | Output directory. Default `reports/` |
| `--format LIST` | `csv`, `json`, or both |
| `--include-failed` | Include failed/unavailable rows in detail |

### 16.6 Long VM sessions

Use `tmux` or `nohup` so an SSH disconnect does not kill the session:

```bash
# tmux
tmux new -s music
python download/run_session.py --config download/config.yaml
# detach: Ctrl-b, then d

# or nohup
nohup python download/run_session.py --config download/config.yaml \
  > data/logs/session.out 2>&1 &
```

Either way, periodic uploads and the manifest snapshots protect progress. The
next invocation resumes automatically.

