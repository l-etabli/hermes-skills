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
craig-listener: react to a Craig recording-panel message in #craig-events.

The Craig panel goes through two states (in the SAME Discord message,
edited in place — Craig does not post a separate "ended" message):

  initial:  "🔴 Recording...        Recording ID: <id>"
  ended:    "Recording ended.       Recording ID: <id>"

Hermes upstream does not support `on_message_edit`, so this listener
only sees the INITIAL state. To bridge the gap, the listener:

  1. Extracts the Recording ID from the message body.
  2. If the body already shows "Recording ended." (rare: panel was
     missed live and Hermes only sees it after the fact), invokes
     craig-transcript-record/scan.py directly.
  3. Otherwise, writes a pending-state file to
     $WIKI_PATH/.craig-pending/<craig_id>.json containing
     craig_id, channel_id, message_id, first_seen_at. The cron-based
     craig-watch skill picks it up from there, refetches the message
     periodically, and triggers the transcription when it sees
     "Recording ended.".

No routing decisions: Discord category permissions guarantee that if
this instance can read a Craig message, the recording is its own.

Input (one of):
  --message "<raw text>"        --message-file <path>        (else: stdin)
Plus REQUIRED for pending state:
  --channel-id <discord channel id>
  --message-id <discord message id>

Output (single JSON object on stdout):
  {"status": "pending",        "craig_id": "...", "state_path": ".craig-pending/<id>.json"}
  {"status": "already-pending", "craig_id": "...", "state_path": ".craig-pending/<id>.json"}
  {"status": "processed", "craig_id": "...", "path": "...", "progress": [...], ...}  # passed-through from scan.py
  {"status": "skipped",   "craig_id": "...", "reason": "...", "progress": [...]?, ...}
  {"status": "error",     "craig_id": "...", "reason": "...", "detail": "...", "progress": [...]?}
  {"status": "error",     "reason": "no-recording-id|empty-message|...", "detail": "..."}

Exit codes: 0 for pending/processed/skipped, 1 for runtime errors,
2 for argument errors.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

REQUIRED_ENV = ("WIKI_PATH", "DISCORD_BOT_TOKEN", "CRAIG_EVENTS_CHANNEL_ID")
_missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if _missing:
    print(json.dumps({"status": "error", "reason": "missing-env",
                      "detail": f"missing required env vars: {', '.join(_missing)}",
                      "missing": _missing}, ensure_ascii=False))
    sys.exit(2)

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
PENDING_DIR = WIKI_PATH / ".craig-pending"

SKILLS_ROOT = pathlib.Path(
    os.environ.get("HERMES_SKILLS_DIR", "/opt/data/skills-shared")
)
SCAN_PY = SKILLS_ROOT / "craig-transcript-record" / "scan.py"

RECORDING_ID_RE = re.compile(r"Recording\s+ID\s*:\s*([A-Za-z0-9_-]+)", re.IGNORECASE)
ENDED_RE = re.compile(r"Recording\s+ended\.", re.IGNORECASE)

DISCORD_API = "https://discord.com/api/v10"


def discover_message_via_api(craig_id: str) -> tuple[str | None, str | None, str | None]:
    """Find the Discord message that announced this craig_id by listing
    the recent messages of #craig-events and matching on `Recording ID:
    <craig_id>` in the body (content + embeds). Used when the LLM didn't
    provide --channel-id / --message-id (it tends to hallucinate them).

    Returns (channel_id, message_id, error). On success, error is None.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("CRAIG_EVENTS_CHANNEL_ID")
    if not token or not channel_id:
        return None, None, "missing DISCORD_BOT_TOKEN or CRAIG_EVENTS_CHANNEL_ID env"

    # urllib only — listener.py doesn't otherwise import requests, and we
    # want this self-discovery to work without uv-with-requests gymnastics.
    import urllib.request
    import urllib.error

    url = f"{DISCORD_API}/channels/{channel_id}/messages?limit=25"
    # Cloudflare in front of discord.com refuses the default
    # Python-urllib User-Agent with `error code: 1010` (browser-signature
    # ban). Discord's API spec also REQUIRES bots to send this exact
    # format: https://discord.com/developers/docs/reference#user-agent
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (https://github.com/l-etabli/hermes-skills, 1.0)",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            messages = json.load(r)
    except urllib.error.HTTPError as e:
        return None, None, f"discord-{e.code}: {e.read()[:200].decode(errors='replace')}"
    except Exception as e:
        return None, None, f"network: {type(e).__name__}: {e}"

    needle = f"Recording ID: {craig_id}"
    needle_lower = needle.lower()
    for m in messages:
        haystack_parts = [m.get("content", "") or ""]
        for em in m.get("embeds", []) or []:
            for f in ("title", "description"):
                v = em.get(f)
                if v:
                    haystack_parts.append(v)
            for fld in em.get("fields", []) or []:
                v = fld.get("value")
                if v:
                    haystack_parts.append(v)
        haystack = "\n".join(haystack_parts).lower()
        if needle_lower in haystack:
            return channel_id, str(m["id"]), None

    return None, None, f"no recent message in channel {channel_id} contains 'Recording ID: {craig_id}'"

# Markers that strongly suggest the message is a Craig recording panel
# even if our `Recording ID:` regex fails to match (e.g. Craig changed
# the format). When ANY of these is present but the ID extraction fails,
# we surface a noisy `format-mismatch` rather than the quiet
# `no-recording-id` so the operator knows our parser is stale.
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


def read_message(args: argparse.Namespace) -> str:
    if args.message is not None:
        return args.message
    if args.message_file:
        return pathlib.Path(args.message_file).read_text(encoding="utf-8", errors="replace")
    return sys.stdin.read()


def invoke_scan(craig_id: str) -> tuple[int, dict, list[dict]]:
    """Run scan.py and parse its multi-line JSON stdout. Returns
    (returncode, canonical_result, progress_entries) — the progress
    list mirrors what scan.py emitted in order, so the LLM can replay
    the phases on a Discord status message."""
    if not SCAN_PY.exists():
        return 1, {"status": "error", "reason": "scan-py-missing",
                   "detail": f"{SCAN_PY} not found (HERMES_SKILLS_DIR misconfigured?)"}, []
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


def write_pending(craig_id: str, channel_id: str, message_id: str) -> tuple[pathlib.Path, bool]:
    """Returns (path, is_new). is_new=False means an entry already existed
    (listener fired twice on the same recording — duplicate Discord event)."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = PENDING_DIR / f"{craig_id}.json"
    if path.exists():
        return path, False
    payload = {
        "craig_id": craig_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--message", help="Raw Craig message body (string).")
    g.add_argument("--message-file", help="Path to a file containing the Craig message body.")
    ap.add_argument("--channel-id", help="Discord channel ID where the Craig message lives (required unless body shows 'Recording ended.').")
    ap.add_argument("--message-id", help="Discord message ID of the Craig panel (required unless body shows 'Recording ended.').")
    args = ap.parse_args()

    message = read_message(args)
    if not message.strip():
        emit({"status": "error", "reason": "empty-message",
              "detail": "no message body provided (use --message, --message-file, or stdin)"})
        return 2

    m = RECORDING_ID_RE.search(message)
    if not m:
        snippet = message.strip().replace("\n", " ")[:200]
        if looks_like_craig(message):
            # Loud: parser is stale, operator must check Craig's format.
            emit({"status": "error", "reason": "format-mismatch",
                  "detail": "message has Craig markers but Recording ID regex failed — Craig format may have changed: "
                            + snippet})
            return 1
        # Quiet: not a Craig panel at all.
        emit({"status": "error", "reason": "no-recording-id", "detail": snippet})
        return 2
    craig_id = m.group(1)

    if ENDED_RE.search(message):
        # Already ended: skip the pending dance, transcribe now.
        rc, payload, progress = invoke_scan(craig_id)
        payload["craig_id"] = craig_id
        if progress:
            payload["progress"] = progress
        emit(payload)
        return rc

    # Recording in progress -> hand off to craig-watch via pending state.
    # Source the channel_id / message_id with the following priority:
    #   1. CLI args, when both are provided AND both look like real
    #      Discord snowflakes (the LLM tends to hallucinate them, so we
    #      gate aggressively).
    #   2. Self-discovery via Discord REST API: look up the message by
    #      `Recording ID: <craig_id>` in the recent #craig-events list.
    # The CLI overrides exist mostly as an escape hatch for manual runs.
    channel_id, message_id = args.channel_id, args.message_id

    use_self_discovery = False
    if not channel_id or not message_id:
        use_self_discovery = True
    elif not re.fullmatch(r"[0-9]+", channel_id) or not re.fullmatch(r"[0-9]+", message_id):
        # Non-numeric -> not snowflakes, fall back.
        use_self_discovery = True
    elif channel_id == message_id:
        # LLM passed the same value twice — classic hallucination.
        use_self_discovery = True

    if use_self_discovery:
        channel_id, message_id, err = discover_message_via_api(craig_id)
        if err:
            emit({"status": "error", "reason": "discord-discovery-failed",
                  "detail": err, "craig_id": craig_id})
            return 1

    state_path, is_new = write_pending(craig_id, channel_id, message_id)
    emit({
        "status": "pending" if is_new else "already-pending",
        "craig_id": craig_id,
        "state_path": str(state_path.relative_to(WIKI_PATH)),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
