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
craig-watch: cron-driven follow-up for craig-listener pending entries.

Each tick:
  1. Lists $WIKI_PATH/.craig-pending/*.json (entries written by craig-listener).
  2. For each entry, refetches the Discord message via the bot REST API
     (`GET /channels/{channel_id}/messages/{message_id}`).
  3. If the message body now contains "Recording ended.", invokes
     craig-transcript-record/scan.py --craig-id <id> to download +
     transcribe the Drive zip. On `processed` or `skipped`, removes the
     pending entry.
  4. If the message has not transitioned yet, leaves the entry alone.
  5. If the entry is older than MAX_AGE_HOURS (24h), drops it as
     `expired` (Craig probably crashed or the recording was abandoned).

Required env: WIKI_PATH, DISCORD_BOT_TOKEN.
Optional env: HERMES_SKILLS_DIR (default /opt/data/skills-shared),
              CRAIG_WATCH_MAX_AGE_HOURS (default 24).

Output (single JSON object on stdout):
  {
    "checked":   N,
    "processed": [{"craig_id": "...", "path": "...", "progress": [...], ...}, ...],
    "skipped":   [{"craig_id": "...", "reason": "...", "progress": [...]}, ...],
    "still_pending": [{"craig_id": "...", "first_seen_at": "..."}, ...],
    "expired":   [{"craig_id": "...", "first_seen_at": "...", "age_hours": N}, ...],
    "errors":    [{"craig_id": "...", "stage": "discord-fetch|scan-py|...", "detail": "...", "progress": [...]?}, ...]
  }

  Each `progress` list (when present) is the ordered trail of phase
  markers emitted by scan.py: drive-poll-start → zip-found → groq-start
  → writing-raw. The LLM uses these to keep a live Discord status
  message in sync with reality.

Exit code: 0 always (a cron run shouldn't fail noisily; per-entry errors
go in the errors[] field).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

import requests

REQUIRED_ENV = ("WIKI_PATH", "DISCORD_BOT_TOKEN")
_missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if _missing:
    print(json.dumps({"checked": 0, "processed": [], "skipped": [],
                      "still_pending": [], "expired": [],
                      "errors": [{"stage": "missing-env",
                                  "detail": f"missing required env vars: {', '.join(_missing)}",
                                  "missing": _missing}]}, ensure_ascii=False))
    sys.exit(0)

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
PENDING_DIR = WIKI_PATH / ".craig-pending"
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

SKILLS_ROOT = pathlib.Path(
    os.environ.get("HERMES_SKILLS_DIR", "/opt/data/skills-shared")
)
SCAN_PY = SKILLS_ROOT / "craig-transcript-record" / "scan.py"

MAX_AGE_HOURS = float(os.environ.get("CRAIG_WATCH_MAX_AGE_HOURS", "24"))
DISCORD_API = "https://discord.com/api/v10"
# Cloudflare fronting discord.com banks browser-signature filtering on
# the User-Agent and rejects `python-requests/X.Y.Z` with `error code:
# 1010`. Discord's spec also requires this exact format for bots:
# https://discord.com/developers/docs/reference#user-agent
DISCORD_USER_AGENT = "DiscordBot (https://github.com/l-etabli/hermes-skills, 1.0)"
ENDED_RE = re.compile(r"Recording\s+ended\.", re.IGNORECASE)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def fetch_message(channel_id: str, message_id: str) -> tuple[dict | None, str | None]:
    """Returns (message_dict, error_str)."""
    try:
        r = requests.get(
            f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}",
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "User-Agent": DISCORD_USER_AGENT,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        return None, f"network: {exc}"
    if r.status_code == 404:
        return None, "message-not-found-or-no-access"
    if r.status_code == 401 or r.status_code == 403:
        return None, f"discord-auth-{r.status_code}"
    if not r.ok:
        return None, f"discord-{r.status_code}: {r.text[:200]}"
    return r.json(), None


def message_body(msg: dict) -> str:
    """Concatenate the message content + all embed text fields."""
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
    return "\n".join(parts)


def invoke_scan(craig_id: str) -> tuple[int, dict, list[dict]]:
    """Run scan.py and parse its multi-line JSON stdout.

    scan.py emits zero or more `{"status": "progress", "phase": ...}`
    lines followed by a single canonical result line. We return the
    result as the second element, and the list of progress entries
    (in emission order) as the third — callers can surface them as a
    live UX trail (e.g. edit a Discord status message).
    """
    if not SCAN_PY.exists():
        return 1, {"status": "error", "reason": "scan-py-missing",
                   "detail": f"{SCAN_PY} not found"}, []
    cmd = [
        "uv", "run",
        "--with", "google-auth",
        "--with", "google-api-python-client",
        "--with", "requests",
        str(SCAN_PY),
        "--craig-id", craig_id,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    progress: list[dict] = []
    result: dict | None = None
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if obj.get("status") == "progress":
            progress.append(obj)
        else:
            result = obj
    if result is None:
        return 1, {"status": "error", "reason": "scan-py-bad-json",
                   "detail": (proc.stdout + proc.stderr)[-500:]}, progress
    return proc.returncode, result, progress


def age_hours(iso: str) -> float:
    try:
        seen = datetime.fromisoformat(iso)
    except Exception:
        return 0.0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).total_seconds() / 3600


def main() -> int:
    result = {
        "checked": 0,
        "processed": [],
        "skipped": [],
        "still_pending": [],
        "expired": [],
        "errors": [],
    }

    if not PENDING_DIR.exists():
        emit(result)
        return 0

    # Sequential per entry on purpose: parallel scan.py invocations would
    # contend on git push to the wiki and risk hitting Groq rate limits.
    # If a Drive poll inside scan.py overruns the 5-min cron tick, the
    # next tick may double-fire — that's fine because scan.py is
    # idempotent (pre-checks the raw by craig_id and drive_id). For
    # extra belt-and-braces, wrap the cron in `flock -n` upstream.
    entries = sorted(PENDING_DIR.glob("*.json"))
    result["checked"] = len(entries)

    for entry_path in entries:
        try:
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
        except Exception as exc:
            result["errors"].append({"craig_id": entry_path.stem, "stage": "load-pending",
                                     "detail": str(exc)})
            continue

        craig_id = entry.get("craig_id") or entry_path.stem
        first_seen_at = entry.get("first_seen_at", "")
        age = age_hours(first_seen_at)

        if age > MAX_AGE_HOURS:
            result["expired"].append({"craig_id": craig_id,
                                      "first_seen_at": first_seen_at,
                                      "age_hours": round(age, 1)})
            entry_path.unlink(missing_ok=True)
            continue

        msg, err = fetch_message(entry["channel_id"], entry["message_id"])
        if err:
            result["errors"].append({"craig_id": craig_id, "stage": "discord-fetch",
                                     "detail": err})
            continue

        body = message_body(msg or {})
        if not ENDED_RE.search(body):
            result["still_pending"].append({"craig_id": craig_id,
                                            "first_seen_at": first_seen_at})
            continue

        rc, payload, progress = invoke_scan(craig_id)
        payload["craig_id"] = craig_id
        if progress:
            payload["progress"] = progress
        status = payload.get("status")
        if status == "processed":
            result["processed"].append(payload)
            entry_path.unlink(missing_ok=True)
        elif status == "skipped":
            result["skipped"].append(payload)
            entry_path.unlink(missing_ok=True)
        else:
            # Error: keep the pending entry so the next tick retries.
            err = {"craig_id": craig_id, "stage": "scan-py",
                   "detail": payload.get("detail") or payload.get("reason") or "unknown"}
            if progress:
                err["progress"] = progress
            result["errors"].append(err)

    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
