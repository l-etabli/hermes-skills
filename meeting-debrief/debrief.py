#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
#   "jsonschema",
#   "pyyaml",
# ]
# ///
"""
meeting-debrief/debrief.py: post-ingest plumbing for one transcript.

The LLM-generated debrief JSON (matching schema.json) is already on disk
when this script runs — this script does NOT call any LLM. It validates,
gates by duration, dedups by transcript sha256, formats the recap as
markdown, posts it to DISCORD_HOME_CHANNEL, opens a thread, posts a
validation prompt in the thread, and writes the pending-state file
consumed by dispatch.py once the user replies.

Usage:
  uv run --with requests --with jsonschema --with pyyaml \
      /opt/data/skills-shared/meeting-debrief/debrief.py \
      --debrief-path raw/debriefs/2026-04-25-craig-xxx.json

Required env: WIKI_PATH, DISCORD_BOT_TOKEN, DISCORD_HOME_CHANNEL.
Optional env: MEETING_DEBRIEF_MIN_DURATION_S (default 180).

Output (single JSON object on stdout):
  {"status": "posted",
   "thread_id": "...", "message_id": "...",
   "channel_id": "...", "action_count": N,
   "debrief_path": "raw/debriefs/...json"}
  {"status": "skipped", "reason": "too-short|already-debriefed|no-actions",
   "detail": "..."}
  {"status": "error",   "reason": "schema-mismatch|discord-...|...",
   "detail": "..."}

Exit codes: 0 (posted/skipped), 1 (runtime error), 2 (arg/env error).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import traceback
from datetime import datetime, timezone

import requests
import yaml
from jsonschema import Draft202012Validator

REQUIRED_ENV = ("WIKI_PATH", "DISCORD_BOT_TOKEN", "DISCORD_HOME_CHANNEL")
_missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if _missing:
    print(json.dumps({"status": "error", "reason": "missing-env",
                      "detail": f"missing required env vars: {', '.join(_missing)}",
                      "missing": _missing}, ensure_ascii=False))
    sys.exit(2)

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_HOME_CHANNEL = os.environ["DISCORD_HOME_CHANNEL"]
MIN_DURATION_S = int(os.environ.get("MEETING_DEBRIEF_MIN_DURATION_S", "180"))

DEBRIEFS_DIR = WIKI_PATH / "raw" / "debriefs"
PENDING_DIR = WIKI_PATH / ".craig-debriefs-pending"
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent / "schema.json"
DISCORD_API = "https://discord.com/api/v10"


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def parse_frontmatter(md_text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) for a transcript file."""
    if not md_text.startswith("---\n"):
        return {}, md_text
    end = md_text.find("\n---\n", 4)
    if end == -1:
        return {}, md_text
    fm_block = md_text[4:end]
    body = md_text[end + 5:]
    try:
        fm = yaml.safe_load(fm_block) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, body


def transcript_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def find_existing_debrief(transcript_sha: str, current_path: pathlib.Path) -> str | None:
    """Lookup another debrief in raw/debriefs/ that already covers this transcript."""
    if not DEBRIEFS_DIR.exists():
        return None
    for f in DEBRIEFS_DIR.glob("*.json"):
        if f.resolve() == current_path.resolve():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("transcript_sha256") == transcript_sha:
            return f.name
    return None


def format_recap(debrief: dict, transcript_path: str) -> str:
    """Render the debrief as a markdown Discord message."""
    lines: list[str] = []
    lines.append(f"## 🎙️ Debrief — `{transcript_path}`")
    lines.append("")
    lines.append("### TLDR")
    lines.append(debrief["tldr"].rstrip())
    lines.append("")

    decisions = debrief.get("decisions") or []
    if decisions:
        lines.append("### Décisions")
        for d in decisions:
            lines.append(f"- {d}")
        lines.append("")

    questions = debrief.get("open_questions") or []
    if questions:
        lines.append("### Questions ouvertes")
        for q in questions:
            lines.append(f"- {q}")
        lines.append("")

    actions = debrief.get("action_items") or []
    if actions:
        lines.append("### Actions proposées")
        for a in actions:
            sa = a.get("suggested_action") or {}
            target = sa.get("target", "?")
            kind = sa.get("kind", "?")
            conf = a.get("confidence", 0)
            lines.append(
                f"{a['id']}. **[{a['type']}]** {a['description']} "
                f"_(→ {kind} `{target}`, conf {conf:.2f})_"
            )
        lines.append("")

    return "\n".join(lines).rstrip()


def discord_post(path: str, body: dict) -> tuple[dict | None, str | None]:
    try:
        r = requests.post(
            f"{DISCORD_API}{path}",
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=15,
        )
    except requests.RequestException as exc:
        return None, f"network: {exc}"
    if not r.ok:
        return None, f"discord-{r.status_code}: {r.text[:300]}"
    try:
        return r.json(), None
    except ValueError:
        return None, f"discord-bad-json: {r.text[:200]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--debrief-path", required=True,
                    help="Path to the LLM-generated debrief JSON (absolute or wiki-relative).")
    args = ap.parse_args()

    debrief_path = pathlib.Path(args.debrief_path)
    if not debrief_path.is_absolute():
        debrief_path = WIKI_PATH / debrief_path
    if not debrief_path.exists():
        emit({"status": "error", "reason": "debrief-not-found",
              "detail": str(debrief_path)})
        return 2

    try:
        debrief = json.loads(debrief_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        emit({"status": "error", "reason": "debrief-bad-json",
              "detail": str(exc)})
        return 1

    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        emit({"status": "error", "reason": "schema-load-failed",
              "detail": f"{type(exc).__name__}: {exc}"})
        return 1

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(debrief), key=lambda e: list(e.path))
    if errors:
        emit({"status": "error", "reason": "schema-mismatch",
              "detail": "; ".join(
                  f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}"
                  for e in errors[:5]
              )})
        return 1

    transcript_rel = debrief["transcript_path"]
    transcript_abs = WIKI_PATH / transcript_rel
    if not transcript_abs.exists():
        emit({"status": "error", "reason": "transcript-not-found",
              "detail": transcript_rel})
        return 1

    md_text = transcript_abs.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_frontmatter(md_text)

    duration_s = int(fm.get("duration_s") or 0)
    if duration_s and duration_s < MIN_DURATION_S:
        emit({"status": "skipped", "reason": "too-short",
              "detail": f"duration_s={duration_s} < {MIN_DURATION_S}"})
        return 0

    sha = transcript_sha256(body)
    existing = find_existing_debrief(sha, debrief_path)
    if existing:
        emit({"status": "skipped", "reason": "already-debriefed",
              "detail": f"transcript already debriefed by {existing}"})
        return 0

    actions = debrief.get("action_items") or []
    recap_md = format_recap(debrief, transcript_rel)

    msg, err = discord_post(
        f"/channels/{DISCORD_HOME_CHANNEL}/messages",
        {"content": recap_md},
    )
    if err:
        emit({"status": "error", "reason": "discord-post-recap", "detail": err})
        return 1
    message_id = msg["id"]

    thread, err = discord_post(
        f"/channels/{DISCORD_HOME_CHANNEL}/messages/{message_id}/threads",
        {"name": f"Debrief {transcript_rel.split('/')[-1]}"[:90],
         "auto_archive_duration": 1440},
    )
    if err:
        emit({"status": "error", "reason": "discord-create-thread",
              "detail": err, "message_id": message_id})
        return 1
    thread_id = thread["id"]

    if actions:
        prompt = (
            f"Valides-tu ces {len(actions)} actions ? Réponds avec les "
            f"numéros à valider (`1,3`), `tous` pour tout valider, `aucun` "
            f"pour tout refuser, ou corrige une cible (`2 → owner/other-repo`)."
        )
    else:
        prompt = "Aucune action proposée. Tu peux fermer le thread."

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    pending = {
        "thread_id": thread_id,
        "channel_id": DISCORD_HOME_CHANNEL,
        "message_id": message_id,
        "transcript_path": transcript_rel,
        "debrief_path": str(debrief_path.relative_to(WIKI_PATH))
            if debrief_path.is_relative_to(WIKI_PATH) else str(debrief_path),
        "transcript_sha256": sha,
        "recap_markdown": recap_md,
        "action_items": actions,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    pending_path = PENDING_DIR / f"{thread_id}.json"
    pending_path.write_text(
        json.dumps(pending, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    _, err = discord_post(
        f"/channels/{thread_id}/messages",
        {"content": prompt},
    )
    if err:
        emit({"status": "error", "reason": "discord-post-thread-prompt",
              "detail": err, "thread_id": thread_id,
              "message_id": message_id,
              "pending_path": str(pending_path.relative_to(WIKI_PATH))})
        return 1

    if not actions:
        try:
            pending_path.unlink()
        except OSError:
            pass
        emit({"status": "posted", "reason": "no-actions",
              "thread_id": thread_id, "message_id": message_id,
              "channel_id": DISCORD_HOME_CHANNEL,
              "action_count": 0,
              "debrief_path": pending["debrief_path"]})
        return 0

    emit({"status": "posted",
          "thread_id": thread_id,
          "message_id": message_id,
          "channel_id": DISCORD_HOME_CHANNEL,
          "action_count": len(actions),
          "debrief_path": pending["debrief_path"]})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        emit({"status": "error", "reason": "unexpected",
              "detail": f"{type(exc).__name__}: {exc}",
              "traceback": traceback.format_exc()[-500:]})
        sys.exit(1)
