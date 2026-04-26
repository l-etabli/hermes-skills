#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "google-auth",
#   "google-api-python-client",
#   "requests",
# ]
# ///
"""
craig-transcript-record: transcribe ONE Craig recording (by recording ID) from
Google Drive into $WIKI_PATH/raw/transcripts/.

Single mode of operation:
  scan.py --craig-id <RECORDING_ID>

The script polls the Drive folder until Craig's `craig_<id>_*.flac.zip`
appears (Craig cooks + uploads ~1-15 min after the recording stops),
downloads it, mixes the per-speaker flacs via ffmpeg, transcribes via
Groq Whisper, and writes a raw transcript with full frontmatter. The
script makes NO routing decision: routing is handled at the Discord
layer (each Hermes instance only sees Craig events for its own
category, by Discord permissions). If you invoked this script, the
recording is yours to transcribe.

Idempotent: a Drive file's `id` is recorded in the raw frontmatter and
re-runs are skipped silently.

Output (stdout, one JSON object per line):
  Zero or more progress lines (only on the long path — Drive poll +
  transcription) describing the current phase, useful for end-user
  visibility (a Discord status message edited live by the LLM):

    {"status": "progress", "phase": "drive-poll-start"}
    {"status": "progress", "phase": "zip-found", "name": "...", "size_bytes": N}
    {"status": "progress", "phase": "groq-start"}
    {"status": "progress", "phase": "writing-raw"}

  Followed by a single canonical result line (always the LAST line of
  stdout — callers MUST parse the last line as the result):

  {
    "status": "processed",
    "path":   "raw/transcripts/2026-04-25-1437-craig.md",
    "drive_id": "...",
    "name":   "craig_xxx_2026-04-25_14-37-58.flac.zip"
  }
  {
    "status": "skipped",
    "reason": "already-transcribed",
    "as":     "raw/transcripts/2026-04-25-1437-craig.md"
  }
  {
    "status": "error",
    "reason": "drive-poll-timeout" | "empty-transcription" | "...",
    "detail": "human-readable"
  }

Run via:
  uv run --with google-auth --with google-api-python-client --with requests \
      /opt/data/skills-shared/craig-transcript-record/scan.py --craig-id <ID>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import traceback
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from datetime import datetime

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

REQUIRED_ENV = ("WIKI_PATH", "GROQ_API_KEY",
                "GOOGLE_APPLICATION_CREDENTIALS", "AUDIO_DRIVE_FOLDER_ID")
_missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if _missing:
    print(json.dumps({"status": "error", "reason": "missing-env",
                      "detail": f"missing required env vars: {', '.join(_missing)}",
                      "missing": _missing}, ensure_ascii=False))
    sys.exit(2)

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
TRANSCRIPTS_DIR = WIKI_PATH / "raw" / "transcripts"
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
SA_PATH = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
DRIVE_FOLDER_ID = os.environ["AUDIO_DRIVE_FOLDER_ID"]

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MAX_BYTES = 25 * 1024 * 1024

# Drive polling: Craig cooks the recording then uploads to Drive. In
# practice this is 1-5 min for short recordings, up to 15 min for long
# ones. We poll with exponential-ish backoff and give up at 20 min.
POLL_INTERVALS_S = [30, 30, 60, 60, 60, 120, 120, 120, 120, 120, 120, 120, 120]
POLL_TIMEOUT_S = 20 * 60


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "audio"


def s_to_ts(s: float) -> str:
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def find_existing_transcript(*, drive_id: str | None = None, craig_id: str | None = None) -> str | None:
    """Single pass over raw/transcripts/. Matches by drive_id and/or craig_id
    (whichever is provided). Used both for the cheap pre-poll check
    (craig_id only) and the post-Drive check (drive_id)."""
    if not TRANSCRIPTS_DIR.exists():
        return None
    needles = []
    if drive_id:
        needles.append(f"drive_id: {drive_id}")
    if craig_id:
        needles.append(f"craig.horse/rec/{craig_id}")
    if not needles:
        return None
    for md in TRANSCRIPTS_DIR.glob("*.md"):
        try:
            head = md.read_text(encoding="utf-8", errors="replace")[:1500]
        except Exception:
            continue
        if any(n in head for n in needles):
            return md.name
    return None


def find_craig_zip(drive, craig_id: str) -> dict | None:
    q = (
        f"'{DRIVE_FOLDER_ID}' in parents and trashed=false "
        f"and name contains 'craig_{craig_id}_' and name contains '.flac.zip'"
    )
    files = (
        drive.files()
        .list(
            q=q,
            orderBy="createdTime desc",
            fields="files(id,name,createdTime,size,mimeType,md5Checksum)",
            pageSize=10,
        )
        .execute()
        .get("files", [])
    )
    return files[0] if files else None


def poll_for_zip(drive, craig_id: str) -> dict | None:
    """Poll Drive until Craig's zip appears or timeout. Returns the file dict or None."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    attempt = 0
    while True:
        f = find_craig_zip(drive, craig_id)
        if f:
            return f
        if time.monotonic() >= deadline:
            return None
        wait = POLL_INTERVALS_S[min(attempt, len(POLL_INTERVALS_S) - 1)]
        # Cap so we don't overshoot the deadline noticeably.
        remaining = deadline - time.monotonic()
        time.sleep(min(wait, max(1, int(remaining))))
        attempt += 1


def download_to(drive, file_id: str, dst: pathlib.Path):
    # Stream straight to disk. A multi-hour Craig zip can be 500MB-1GB,
    # buffering in BytesIO would peak the container's memory.
    with open(dst, "wb") as fh:
        dl = MediaIoBaseDownload(fh, drive.files().get_media(fileId=file_id))
        done = False
        while not done:
            _, done = dl.next_chunk()


def extract_and_mix(zip_path: pathlib.Path, tmpdir: pathlib.Path) -> tuple[pathlib.Path, list[str]]:
    flac_paths: list[pathlib.Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for zi in zf.infolist():
            if zi.filename.lower().endswith(".flac"):
                p = tmpdir / pathlib.Path(zi.filename).name
                p.write_bytes(zf.read(zi))
                flac_paths.append(p)
    if not flac_paths:
        raise RuntimeError("zip contains no .flac files")
    speakers = [p.stem.split("-", 1)[-1] for p in flac_paths]
    if len(flac_paths) == 1:
        return flac_paths[0], speakers
    out = tmpdir / "mix.flac"
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in flac_paths:
        cmd += ["-i", str(p)]
    cmd += [
        "-filter_complex", f"amix=inputs={len(flac_paths)}:normalize=0",
        "-c:a", "flac", str(out),
    ]
    subprocess.run(cmd, check=True)
    return out, speakers


def transcribe_groq(audio_path: pathlib.Path) -> dict:
    size = audio_path.stat().st_size
    if size > GROQ_MAX_BYTES:
        raise RuntimeError(
            f"audio {size/1e6:.1f}MB > {GROQ_MAX_BYTES/1e6:.0f}MB Groq limit; "
            "re-encode lower (ffmpeg -ab 96k -ar 16000) or split"
        )
    with open(audio_path, "rb") as fh:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (audio_path.name, fh, "audio/flac")},
            data={
                "model": "whisper-large-v3-turbo",
                "response_format": "verbose_json",
                "language": "fr",
                "temperature": "0",
            },
            timeout=300,
        )
    r.raise_for_status()
    return r.json()


def parse_recorded_at(name: str, drive_created: str) -> str:
    m = re.search(
        r"craig_[A-Za-z0-9_-]+_(\d{4})-(\d{1,2})-(\d{1,2})_(\d{1,2})-(\d{1,2})-(\d{1,2})",
        name,
    )
    if m:
        y, mo, d, h, mi, s = map(int, m.groups())
        return datetime(y, mo, d, h, mi, s).strftime("%Y-%m-%d %H:%M")
    try:
        return datetime.fromisoformat(drive_created.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M")


def write_transcript(f: dict, tx: dict, speakers: list[str], craig_id: str) -> pathlib.Path | None:
    name = f["name"]
    recorded_at = parse_recorded_at(name, f.get("createdTime", ""))
    date = recorded_at.split(" ")[0]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    duration_s = int(tx.get("duration", 0))
    dur_text = f"{duration_s // 60}m {duration_s % 60}s"
    lang = tx.get("language", "?")

    # Format slug as <HHMM>-craig (e.g. "1240-craig"). Combined with the
    # date prefix in out_path below, the full filename reads
    # "<date>-<HHMM>-craig.md" — sortable, human-readable, no opaque ID.
    # craig_id remains in source_url + drive_id frontmatter for traceability.
    hhmm = recorded_at.split(" ")[1].replace(":", "") if " " in recorded_at else "0000"
    slug = f"{hhmm}-craig"
    src_url = f"https://craig.horse/rec/{craig_id}"

    speakers_line = ", ".join(speakers) if speakers else "???"
    single_speaker = speakers[0] if len(speakers) == 1 else None
    body_lines = []
    for seg in tx.get("segments", []):
        ts = s_to_ts(seg["start"])
        sp = single_speaker or "???"
        body_lines.append(f"[{ts}] **{sp}**: {seg['text'].strip()}\n")
    body = "\n".join(body_lines).rstrip() + "\n"

    body_full = (
        f"# Transcription Craig {recorded_at}\n\n"
        f"- **Source audio** : `{name}`\n"
        f"- **Date** : {recorded_at}\n"
        f"- **Durée** : {dur_text}\n"
        f"- **Participants** : {speakers_line}\n"
        f"- **Transcription** : Groq Whisper Large v3 Turbo (langue détectée : `{lang}`)\n\n"
        f"{body}"
    )
    sha = hashlib.sha256(body_full.encode("utf-8")).hexdigest()
    fm = (
        f"---\n"
        f"source: craig\n"
        f"source_url: {src_url}\n"
        f"drive_id: {f['id']}\n"
        f"drive_md5: {f.get('md5Checksum','')}\n"
        f"recorded_at: {recorded_at}\n"
        f"ingested: {today}\n"
        f"duration_s: {duration_s}\n"
        f"sha256: {sha}\n"
        f"---\n\n"
    )

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TRANSCRIPTS_DIR / f"{date}-{slug}.md"
    if out_path.exists():
        existing = out_path.read_text(encoding="utf-8", errors="replace")[:1500]
        if f"drive_id: {f['id']}" in existing:
            return None
        out_path = TRANSCRIPTS_DIR / f"{date}-{slug}-2.md"
    out_path.write_text(fm + body_full, encoding="utf-8")
    return out_path


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def emit_progress(phase: str, **kwargs) -> None:
    payload = {"status": "progress", "phase": phase, **kwargs}
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--craig-id", required=True, help="Craig recording ID (from the Craig event message).")
    args = ap.parse_args()

    craig_id = args.craig_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", craig_id):
        emit({"status": "error", "reason": "bad-craig-id", "detail": craig_id})
        return 2

    # Cheap pre-poll: if we already have a raw for this craig_id, skip
    # without hitting Drive at all.
    pre = find_existing_transcript(craig_id=craig_id)
    if pre:
        emit({"status": "skipped", "reason": "already-transcribed", "as": pre})
        return 0

    creds = service_account.Credentials.from_service_account_file(
        SA_PATH, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    emit_progress("drive-poll-start")
    f = poll_for_zip(drive, craig_id)
    if not f:
        emit({"status": "error", "reason": "drive-poll-timeout",
              "detail": f"no craig_{craig_id}_*.flac.zip in folder after {POLL_TIMEOUT_S//60}min"})
        return 1

    try:
        size_bytes = int(f.get("size") or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    emit_progress("zip-found", name=f["name"], size_bytes=size_bytes)

    existing = find_existing_transcript(drive_id=f["id"])
    if existing:
        emit({"status": "skipped", "reason": "already-transcribed", "as": existing})
        return 0

    try:
        with tempfile.TemporaryDirectory() as td:
            tmpdir = pathlib.Path(td)
            zip_path = tmpdir / f["name"]
            download_to(drive, f["id"], zip_path)
            audio, speakers = extract_and_mix(zip_path, tmpdir)
            emit_progress("groq-start")
            tx = transcribe_groq(audio)
            if not tx.get("segments"):
                emit({"status": "error", "reason": "empty-transcription",
                      "detail": "Groq returned 0 segments (silence?)"})
                return 1
            emit_progress("writing-raw")
            out = write_transcript(f, tx, speakers, craig_id)
            if out is None:
                emit({"status": "skipped", "reason": "race-already-written"})
                return 0
            emit({
                "status": "processed",
                "path": str(out.relative_to(WIKI_PATH)),
                "drive_id": f["id"],
                "name": f["name"],
                "duration_s": int(tx.get("duration", 0)),
            })
            return 0
    except subprocess.CalledProcessError as exc:
        emit({"status": "error", "reason": "ffmpeg-failed", "detail": str(exc)})
        return 1
    except requests.HTTPError as exc:
        emit({"status": "error", "reason": "groq-http-error", "detail": str(exc)})
        return 1
    except Exception as exc:
        emit({"status": "error", "reason": "unexpected",
              "detail": f"{type(exc).__name__}: {exc}",
              "traceback": traceback.format_exc()[-500:]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
