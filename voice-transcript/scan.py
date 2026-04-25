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
voice-transcript scan: list a Google Drive folder, transcribe new audio
via Groq Whisper, write transcripts to $WIKI_PATH/raw/transcripts/.
Idempotent: each candidate's Drive ID is recorded in the raw frontmatter
and re-runs skip already-processed files.

Filtering (deterministic, no LLM):
  - $HERMES_OWNED_CHANNEL_IDS (CSV) restricts Craig zips to those whose
    info.txt names one of the listed Discord channel IDs. Channels —
    not guilds — are the right discriminator: a single guild can host
    several Hermes instances, each owning a subset of channels (e.g. an
    "Établi" guild with #perso-*, #piloti-*, #telluris-* channels mapped
    to the corresponding instance). Empty / unset means no filter.
  - In auto-scan mode (default), direct audio files (mp3/m4a/wav/flac/ogg
    without an info.txt sibling) are ignored when $HERMES_OWNED_CHANNEL_IDS
    is non-empty — they have no discriminator and would otherwise be
    processed by every co-tenant instance.
  - In explicit-trigger mode (--filename <name>), the named file is always
    processed, regardless of channel filtering.

Output: a JSON object on stdout
  {
    "new":     ["raw/transcripts/...", ...],
    "skipped": [{"name": "...", "reason": "already-transcribed"}, ...],
    "errors":  [{"name": "...", "error": "..."}, ...]
  }

Run via:
  uv run --with google-auth --with google-api-python-client --with requests \
      /opt/data/skills-shared/voice-transcript/scan.py [--filename NAME] [--craig-id ID]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from datetime import datetime

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
TRANSCRIPTS_DIR = WIKI_PATH / "raw" / "transcripts"
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
SA_PATH = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
DRIVE_FOLDER_ID = os.environ["AUDIO_DRIVE_FOLDER_ID"]

# Comma-separated list of Discord Channel IDs this instance is allowed
# to process. Empty/unset means "no filter" (mono-instance setup).
OWNED_CHANNELS: set[str] = {
    c.strip() for c in os.environ.get("HERMES_OWNED_CHANNEL_IDS", "").split(",") if c.strip()
}

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MAX_BYTES = 25 * 1024 * 1024
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".flac", ".ogg")


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "audio"


def s_to_ts(s: float) -> str:
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def already_transcribed(drive_id: str) -> str | None:
    """Returns the existing filename if drive_id is already in raw/transcripts/, else None."""
    if not TRANSCRIPTS_DIR.exists():
        return None
    needle = f"drive_id: {drive_id}"
    for md in TRANSCRIPTS_DIR.glob("*.md"):
        try:
            head = md.read_text(encoding="utf-8", errors="replace")[:1500]
        except Exception:
            continue
        if needle in head:
            return md.name
    return None


def list_candidates(drive, craig_id: str | None):
    name_clauses = [f"name contains '{ext}'" for ext in (".flac.zip",) + AUDIO_EXTS]
    name_clauses.append("mimeType contains 'audio/'")
    q = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false and ({' or '.join(name_clauses)})"
    if craig_id:
        q += f" and name contains 'craig_{craig_id}_'"
    return (
        drive.files()
        .list(
            q=q,
            orderBy="createdTime desc",
            fields="files(id,name,createdTime,size,mimeType,md5Checksum)",
            pageSize=200,
        )
        .execute()
        .get("files", [])
    )


def download_to(drive, file_id: str, dst: pathlib.Path):
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = dl.next_chunk()
    dst.write_bytes(buf.getvalue())


# Match `Channel: <name> (123456789)` with possible leading whitespace.
# Channel IDs (not Guild IDs) are the right discriminator across Hermes
# instances that share a Discord server.
_CHANNEL_RE = re.compile(r"^\s*Channel\s*:\s*.+?\(([0-9]+)\)\s*$", re.MULTILINE | re.IGNORECASE)


def extract_channel_id_from_zip(zip_path: pathlib.Path) -> str | None:
    """Open a Craig zip, read info.txt, return the Discord Channel ID or None."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open("info.txt") as fh:
                text = fh.read().decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile):
        return None
    m = _CHANNEL_RE.search(text)
    return m.group(1) if m else None


def prepare_audio(drive, f: dict, tmpdir: pathlib.Path):
    """
    Returns (audio_path, speakers, source, owner_decision) where
    owner_decision is one of:
      - "owned"  : zip's Channel ID is in OWNED_CHANNELS (or no filter active)
      - "foreign": zip's Channel ID is set but not in OWNED_CHANNELS — caller skips
      - "no-info": no info.txt found (rare for Craig zips) — caller skips in scan mode
      - "n/a"    : not a Craig zip (audio direct), no channel concept — owner_decision irrelevant
    """
    name = f["name"]
    if name.endswith(".flac.zip"):
        zip_path = tmpdir / name
        download_to(drive, f["id"], zip_path)
        channel_id = extract_channel_id_from_zip(zip_path)
        if channel_id is None:
            return None, [], "craig", "no-info"
        if OWNED_CHANNELS and channel_id not in OWNED_CHANNELS:
            return None, [], "craig", "foreign"
        flac_paths: list[pathlib.Path] = []
        with zipfile.ZipFile(zip_path) as zf:
            for zi in zf.infolist():
                if zi.filename.lower().endswith(".flac"):
                    p = tmpdir / pathlib.Path(zi.filename).name
                    p.write_bytes(zf.read(zi))
                    flac_paths.append(p)
        speakers = [p.stem.split("-", 1)[-1] for p in flac_paths]
        if len(flac_paths) == 1:
            return flac_paths[0], speakers, "craig", "owned"
        out = tmpdir / "mix.flac"
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        for p in flac_paths:
            cmd += ["-i", str(p)]
        cmd += [
            "-filter_complex", f"amix=inputs={len(flac_paths)}:normalize=0",
            "-c:a", "flac", str(out),
        ]
        subprocess.run(cmd, check=True)
        return out, speakers, "craig", "owned"
    out = tmpdir / name
    download_to(drive, f["id"], out)
    return out, [], "manual", "n/a"


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


def parse_recorded_at(name: str, source: str, drive_created: str) -> str:
    if source == "craig":
        m = re.search(
            r"craig_[A-Za-z0-9]+_(\d{4})-(\d{1,2})-(\d{1,2})_(\d{1,2})-(\d{1,2})-(\d{1,2})",
            name,
        )
        if m:
            y, mo, d, h, mi, s = map(int, m.groups())
            return datetime(y, mo, d, h, mi, s).strftime("%Y-%m-%d %H:%M")
    try:
        return datetime.fromisoformat(drive_created.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M")


def write_transcript(f: dict, tx: dict, speakers: list[str], source: str) -> pathlib.Path | None:
    name = f["name"]
    recorded_at = parse_recorded_at(name, source, f.get("createdTime", ""))
    date = recorded_at.split(" ")[0]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    duration_s = int(tx.get("duration", 0))
    dur_text = f"{duration_s // 60}m {duration_s % 60}s"
    lang = tx.get("language", "?")

    if source == "craig":
        m = re.search(r"craig_([A-Za-z0-9]+)_", name)
        slug = f"craig-{m.group(1).lower()}-{slugify(date)}" if m else f"audio-{f['id'][:8]}"
    else:
        slug = slugify(pathlib.Path(name).stem) or f"audio-{f['id'][:8]}"

    if source == "craig":
        m = re.search(r"craig_([A-Za-z0-9]+)_", name)
        src_url = f"https://craig.horse/rec/{m.group(1)}" if m else f"drive://{DRIVE_FOLDER_ID}/{name}"
    else:
        src_url = f"drive://{DRIVE_FOLDER_ID}/{name}"

    speakers_line = ", ".join(speakers) if speakers else "???"
    single_speaker = speakers[0] if len(speakers) == 1 else None
    body_lines = []
    for seg in tx.get("segments", []):
        ts = s_to_ts(seg["start"])
        sp = single_speaker or "???"
        body_lines.append(f"[{ts}] **{sp}**: {seg['text'].strip()}\n")
    body = "\n".join(body_lines).rstrip() + "\n"

    body_full = (
        f"# Transcription {slug}\n\n"
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
        f"source: {source}\n"
        f"source_url: {src_url}\n"
        f"drive_id: {f['id']}\n"
        f"drive_md5: {f.get('md5Checksum','')}\n"
        f"recorded_at: {recorded_at}\n"
        f"ingested: {today}\n"
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--filename",
        help="Process only the file with this exact name (explicit-trigger mode); "
             "bypasses HERMES_OWNED_CHANNEL_IDS filtering for this single file.",
    )
    ap.add_argument(
        "--craig-id",
        help="Process only Craig zips matching this recording ID.",
    )
    args = ap.parse_args()

    creds = service_account.Credentials.from_service_account_file(
        SA_PATH, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    candidates = list_candidates(drive, args.craig_id)
    if args.filename:
        candidates = [f for f in candidates if f["name"] == args.filename]

    if not candidates:
        print(json.dumps({"new": [], "skipped": [], "errors": [],
                          "reason": "no candidates (empty folder, SA no access, or filter excluded all)"}))
        return

    new_files: list[str] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    explicit_trigger = bool(args.filename)

    with tempfile.TemporaryDirectory() as td:
        tmpdir = pathlib.Path(td)
        for f in candidates:
            existing = already_transcribed(f["id"])
            if existing:
                skipped.append({"name": f["name"], "reason": "already-transcribed", "as": existing})
                continue
            try:
                audio, speakers, source, owner = prepare_audio(drive, f, tmpdir)
                # Guild filtering, only when not explicit-trigger
                if not explicit_trigger:
                    if owner == "foreign":
                        skipped.append({"name": f["name"], "reason": "foreign-channel"})
                        continue
                    if owner == "no-info":
                        skipped.append({"name": f["name"], "reason": "no-channel-info"})
                        continue
                    if source == "manual" and OWNED_CHANNELS:
                        skipped.append({"name": f["name"], "reason": "manual-audio-no-discriminator"})
                        continue
                if audio is None:
                    skipped.append({"name": f["name"], "reason": f"prepare-returned-none ({owner})"})
                    continue
                tx = transcribe_groq(audio)
                if not tx.get("segments"):
                    skipped.append({"name": f["name"], "reason": "empty-transcription"})
                    continue
                out = write_transcript(f, tx, speakers, source)
                if out:
                    new_files.append(str(out.relative_to(WIKI_PATH)))
                else:
                    skipped.append({"name": f["name"], "reason": "race-already-written"})
            except Exception as exc:
                errors.append({"name": f["name"], "error": str(exc)})

    print(json.dumps({"new": new_files, "skipped": skipped, "errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
