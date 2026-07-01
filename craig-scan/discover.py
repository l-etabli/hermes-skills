#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
# ]
# ///
"""
craig-scan/discover.py: list Craig recordings present in the
#craig-events history of this Hermes instance that have NOT yet been
transcribed and are NOT currently being watched.

This script does NOT transcribe anything. It only discovers candidates
and returns them so the LLM can ask the user which ones to process,
then invoke craig-transcript-record/scan.py per ID.

Pipeline:
  1. GET the last 100 messages of $CRAIG_EVENTS_CHANNEL_ID via the bot REST API.
  2. Keep messages whose body contains BOTH `Recording ID:` AND `Recording ended.`
     (the only state where the recording is finished and the zip should
     be on Drive).
  3. Dedupe against:
     - $WIKI_PATH/raw/transcripts/*.md filenames matching `*-craig-<id>-*`
     - $WIKI_PATH/raw/transcripts/*.md content containing `craig.horse/rec/<id>`
     - $WIKI_PATH/.craig-pending/<id>.json (already being watched by craig-watch)
  4. Emit a JSON list of candidates so the LLM can present and confirm.

Required env: WIKI_PATH, CRAIG_DISCORD_BOT_TOKEN, CRAIG_EVENTS_CHANNEL_ID.

Output (single JSON object on stdout):
  {
    "channel_id": "...",
    "fetched": N,
    "candidates": [
      {
        "craig_id": "...",
        "message_id": "...",
        "message_url": "https://discord.com/channels/<guild>/<channel>/<message>",
        "started_at": "15:06:15",            # parsed from the panel if present
        "channel_name": "salon de test",     # parsed from the panel if present
        "posted_at": "2026-04-25T13:06:15+00:00"
      }, ...
    ],
    "already_transcribed": [{"craig_id": "...", "as": "..."}, ...],
    "already_pending":     [{"craig_id": "..."}, ...]
  }
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from datetime import datetime

import requests

REQUIRED_ENV = ("WIKI_PATH", "CRAIG_DISCORD_BOT_TOKEN", "CRAIG_EVENTS_CHANNEL_ID")
_missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if _missing:
    print(json.dumps({"status": "error", "reason": "missing-env",
                      "detail": f"missing required env vars: {', '.join(_missing)}",
                      "missing": _missing}, ensure_ascii=False))
    sys.exit(2)

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
TRANSCRIPTS_DIR = WIKI_PATH / "raw" / "transcripts"
PENDING_DIR = WIKI_PATH / ".craig-pending"
CRAIG_DISCORD_BOT_TOKEN = os.environ["CRAIG_DISCORD_BOT_TOKEN"]
CHANNEL_ID = os.environ["CRAIG_EVENTS_CHANNEL_ID"]

DISCORD_API = "https://discord.com/api/v10"
DISCORD_HEADERS = {
    "Authorization": f"Bot {CRAIG_DISCORD_BOT_TOKEN}",
    "User-Agent": "DiscordBot (https://github.com/l-etabli/hermes-skills, 1.0)",
}
RECORDING_ID_RE = re.compile(
    # Craig Components V2 writes `**Recording ID:** \`<id>\``: skip past
    # bold/code wrappers (** and backticks) before the id.
    r"Recording[\s*]*ID[\s*]*:[\s*`]*([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
ENDED_RE = re.compile(r"Recording\s+ended\.", re.IGNORECASE)
TRANSCRIPT_SOURCE_URL_RE = re.compile(r"craig\.horse/rec/([A-Za-z0-9_-]+)", re.IGNORECASE)
STARTED_RE = re.compile(r"Started\s*:\s*([0-9:]+)", re.IGNORECASE)
CHANNEL_NAME_RE = re.compile(
    # Craig prefixes the channel name with an invisible Unicode character
    # (e.g. U+2060 word-joiner) and sometimes a `#`. Strip those (without
    # using `\W*`, which would also eat accented letters) before capturing
    # the channel name itself.
    r"Channel\s*:[\s#​-‏⁠﻿⁡-⁯]*([^\n]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Markers that flag a message as Craig-ish even if our parsers fail.
# Used to detect a stale parser instead of failing silently.
CRAIG_MARKER_RES = [
    re.compile(r"🔴\s*Recording", re.IGNORECASE),
    re.compile(r"Recording\s+ended\.", re.IGNORECASE),
    re.compile(r"Voice\s+Region\s*:", re.IGNORECASE),
    re.compile(r"Autorecord", re.IGNORECASE),
]


def looks_like_craig(body: str) -> bool:
    return any(rx.search(body) for rx in CRAIG_MARKER_RES)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def component_texts(node: object) -> list[str]:
    """Collect text-bearing fields from Discord Components V2 recursively."""
    if isinstance(node, list):
        return [text for child in node for text in component_texts(child)]
    if not isinstance(node, dict):
        return []

    own_texts = [
        node[field]
        for field in ("content", "title", "description", "label", "value")
        if isinstance(node.get(field), str) and node[field]
    ]
    child_texts = component_texts(node.get("components") or [])
    return own_texts + child_texts


def message_body(msg: dict) -> str:
    parts = [msg.get("content", "") or ""]
    for em in msg.get("embeds", []) or []:
        for field in ("title", "description"):
            v = em.get(field)
            if v:
                parts.append(v)
        for f in em.get("fields", []) or []:
            n, v = f.get("name", ""), f.get("value", "")
            if n:
                parts.append(n)
            if v:
                parts.append(v)
        author = (em.get("author") or {}).get("name")
        if author:
            parts.append(author)
        footer = (em.get("footer") or {}).get("text")
        if footer:
            parts.append(footer)
    parts.extend(component_texts(msg.get("components") or []))
    return "\n".join(parts)


def transcribed_ids() -> dict[str, str]:
    """Map craig_id -> raw filename for transcripts already present."""
    out: dict[str, str] = {}
    if not TRANSCRIPTS_DIR.exists():
        return out
    # Filename layout fallback: <date>-craig-<id>-<slugified_date>.md,
    # where <id> may contain `-` and `_`. Anchor on the trailing date+".md"
    # so the capture stops cleanly at the boundary.
    filename_pat = re.compile(r"-craig-([A-Za-z0-9_-]+?)-\d{4}-\d{2}-\d{2}\.md$")
    for md in TRANSCRIPTS_DIR.glob("*.md"):
        filename_match = filename_pat.search(md.name)
        if filename_match:
            out[filename_match.group(1)] = md.name

        body = md.read_text(encoding="utf-8")
        for source_url_match in TRANSCRIPT_SOURCE_URL_RE.finditer(body):
            out[source_url_match.group(1)] = md.name
    return out


def pending_ids() -> set[str]:
    if not PENDING_DIR.exists():
        return set()
    return {p.stem for p in PENDING_DIR.glob("*.json")}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Discover finished Craig recordings in #craig-events that are not transcribed yet.",
    )
    ap.add_argument("--limit", type=int, default=100, help="Discord messages to fetch, max 100")
    args = ap.parse_args()
    if args.limit < 1 or args.limit > 100:
        ap.error("--limit must be between 1 and 100")
    return args


def main() -> int:
    args = parse_args()
    r = requests.get(
        f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
        headers=DISCORD_HEADERS,
        params={"limit": args.limit},
        timeout=30,
    )
    if r.status_code in (401, 403):
        emit({"status": "error", "reason": f"discord-auth-{r.status_code}",
              "detail": r.text[:200]})
        return 1
    if r.status_code == 404:
        emit({"status": "error", "reason": "channel-not-found-or-no-access",
              "detail": f"CRAIG_EVENTS_CHANNEL_ID={CHANNEL_ID}"})
        return 1
    if not r.ok:
        emit({"status": "error", "reason": f"discord-{r.status_code}",
              "detail": r.text[:200]})
        return 1

    messages = r.json()

    # Discord history doesn't tell us the guild_id directly; look it up
    # once to build clickable message URLs.
    guild_id = None
    ch_resp = requests.get(
        f"{DISCORD_API}/channels/{CHANNEL_ID}",
        headers=DISCORD_HEADERS,
        timeout=15,
    )
    if ch_resp.ok:
        guild_id = (ch_resp.json() or {}).get("guild_id")

    done = transcribed_ids()
    in_flight = pending_ids()

    candidates: list[dict] = []
    already_transcribed: list[dict] = []
    already_pending: list[dict] = []
    unparseable_craig: list[dict] = []
    seen: set[str] = set()

    for msg in messages:
        body = message_body(msg)
        rid_match = RECORDING_ID_RE.search(body)
        if not rid_match:
            # Loud about Craig-ish messages we can't parse — likely
            # stale regex or Craig format change.
            if looks_like_craig(body):
                unparseable_craig.append({
                    "message_id": msg["id"],
                    "posted_at": msg.get("timestamp"),
                    "snippet": body.strip().replace("\n", " ")[:200],
                })
            continue
        if not ENDED_RE.search(body):
            # Recording still in progress; craig-watch handles those.
            continue
        craig_id = rid_match.group(1)
        if craig_id in seen:
            continue
        seen.add(craig_id)

        if craig_id in done:
            already_transcribed.append({"craig_id": craig_id, "as": done[craig_id]})
            continue
        if craig_id in in_flight:
            already_pending.append({"craig_id": craig_id})
            continue

        started = STARTED_RE.search(body)
        chan = CHANNEL_NAME_RE.search(body)
        msg_url = (
            f"https://discord.com/channels/{guild_id}/{CHANNEL_ID}/{msg['id']}"
            if guild_id else f"https://discord.com/channels/@me/{CHANNEL_ID}/{msg['id']}"
        )
        candidates.append({
            "craig_id": craig_id,
            "message_id": msg["id"],
            "message_url": msg_url,
            "started_at": started.group(1) if started else None,
            "channel_name": chan.group(1).strip() if chan else None,
            "posted_at": msg.get("timestamp"),
        })

    emit({
        "channel_id": CHANNEL_ID,
        "fetched": len(messages),
        "candidates": candidates,
        "already_transcribed": already_transcribed,
        "already_pending": already_pending,
        "unparseable_craig": unparseable_craig,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
