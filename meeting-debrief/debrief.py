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

Required env: WIKI_PATH, CRAIG_DISCORD_BOT_TOKEN, CRAIG_HOME_CHANNEL.
Optional env: MEETING_DEBRIEF_MIN_DURATION_S (default 180).

Output (single JSON object on stdout):
  {"status": "posted",
   "thread_id": "...", "parent_message_id": "...",
   "actions_message_id": "...", "channel_id": "...",
   "action_count": N, "chunks_posted": N,
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
import re
import sys
import traceback
from datetime import datetime, timezone

import requests
import yaml
from jsonschema import Draft202012Validator

REQUIRED_ENV = ("WIKI_PATH",)
_missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if not (os.environ.get("CRAIG_DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN")):
    _missing.append("CRAIG_DISCORD_BOT_TOKEN")
if not (os.environ.get("CRAIG_HOME_CHANNEL") or os.environ.get("DISCORD_HOME_CHANNEL")):
    _missing.append("CRAIG_HOME_CHANNEL")
if _missing:
    print(json.dumps({"status": "error", "reason": "missing-env",
                      "detail": f"missing required env vars: {', '.join(_missing)}",
                      "missing": _missing}, ensure_ascii=False))
    sys.exit(2)

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
DISCORD_BOT_TOKEN = os.environ.get("CRAIG_DISCORD_BOT_TOKEN") or os.environ["DISCORD_BOT_TOKEN"]
DISCORD_HOME_CHANNEL = os.environ.get("CRAIG_HOME_CHANNEL") or os.environ["DISCORD_HOME_CHANNEL"]
MIN_DURATION_S = int(os.environ.get("MEETING_DEBRIEF_MIN_DURATION_S", "180"))

DEBRIEFS_DIR = WIKI_PATH / "raw" / "debriefs"
PENDING_DIR = WIKI_PATH / ".craig-debriefs-pending"
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent / "schema.json"
DISCORD_API = "https://discord.com/api/v10"
# Cloudflare in front of discord.com refuses python-requests' default
# UA with `error code: 1010` (browser-signature ban). Discord's spec
# also explicitly REQUIRES bots to send this exact format:
# https://discord.com/developers/docs/reference#user-agent
DISCORD_USER_AGENT = "DiscordBot (https://github.com/l-etabli/hermes-skills, 1.0)"
# Discord caps message content at 2000 chars. Leave a buffer for any
# trailing edits made by dispatch.py (the ✅/❌ annotation lines add a
# few chars per action) and for safety against width miscounts.
DISCORD_CONTENT_MAX = 1900


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
                "User-Agent": DISCORD_USER_AGENT,
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


def split_recap(recap: str, max_chunk: int = DISCORD_CONTENT_MAX) -> list[str]:
    """Split the recap into one or more Discord messages of ≤ max_chunk
    chars. Splits at section boundaries (`### ...`).

    The "Actions proposées" section is always emitted as its OWN chunk:
    dispatch.py edits it in place to add ✅/❌ marks, so it must fit in
    a single message. If that section alone exceeds max_chunk (rare,
    given the LLM prompt caps the full recap at ~3500), it is hard-cut
    with a footer marker — the full version remains in raw/debriefs/."""
    if len(recap) <= max_chunk:
        return [recap]

    parts = re.split(r"(?=^### )", recap, flags=re.MULTILINE)
    parts = [p.rstrip() for p in parts if p.strip()]

    chunks: list[str] = []
    current = ""
    for part in parts:
        is_actions = part.startswith("### Actions")
        if is_actions:
            if current:
                chunks.append(current.rstrip())
                current = ""
            if len(part) > max_chunk:
                marker = "\n_(actions tronquées — voir `raw/debriefs/` pour la version complète)_"
                part = part[: max_chunk - len(marker)].rstrip() + marker
            chunks.append(part)
            continue
        candidate = (current + "\n\n" + part).strip() if current else part
        if len(candidate) <= max_chunk:
            current = candidate
        else:
            if current:
                chunks.append(current.rstrip())
                current = part if len(part) <= max_chunk else part[:max_chunk].rstrip()
            else:
                chunks.append(part[:max_chunk].rstrip())
                current = ""
    if current:
        chunks.append(current.rstrip())
    return chunks


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
    full_recap_md = format_recap(debrief, transcript_rel)
    recap_chunks = split_recap(full_recap_md)

    parent_message_id: str | None = None
    actions_message_id: str | None = None
    actions_message_content: str | None = None
    last_message_id: str | None = None
    for i, chunk in enumerate(recap_chunks):
        msg, err = discord_post(
            f"/channels/{DISCORD_HOME_CHANNEL}/messages",
            {"content": chunk},
        )
        if err:
            emit({"status": "error", "reason": "discord-post-recap",
                  "detail": err, "chunk_index": i,
                  "chunks_posted": i,
                  "parent_message_id": parent_message_id})
            return 1
        mid = msg["id"]
        if i == 0:
            parent_message_id = mid
        if chunk.lstrip().startswith("### Actions"):
            actions_message_id = mid
            actions_message_content = chunk
        last_message_id = mid

    # If no chunk started with "### Actions" (e.g. actions were absent or
    # the section title was rephrased by the formatter), fall back to the
    # last message — dispatch.py's annotate is a no-op on text without
    # numbered action lines, so this stays safe.
    if not actions_message_id:
        actions_message_id = last_message_id
        actions_message_content = recap_chunks[-1]

    thread, err = discord_post(
        f"/channels/{DISCORD_HOME_CHANNEL}/messages/{parent_message_id}/threads",
        {"name": f"Debrief {transcript_rel.split('/')[-1]}"[:90],
         "auto_archive_duration": 1440},
    )
    if err:
        emit({"status": "error", "reason": "discord-create-thread",
              "detail": err, "parent_message_id": parent_message_id})
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
        "parent_message_id": parent_message_id,
        "actions_message_id": actions_message_id,
        "actions_message_content": actions_message_content,
        "transcript_path": transcript_rel,
        "debrief_path": str(debrief_path.relative_to(WIKI_PATH))
            if debrief_path.is_relative_to(WIKI_PATH) else str(debrief_path),
        "transcript_sha256": sha,
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
              "parent_message_id": parent_message_id,
              "pending_path": str(pending_path.relative_to(WIKI_PATH))})
        return 1

    if not actions:
        try:
            pending_path.unlink()
        except OSError:
            pass
        emit({"status": "posted", "reason": "no-actions",
              "thread_id": thread_id,
              "parent_message_id": parent_message_id,
              "channel_id": DISCORD_HOME_CHANNEL,
              "action_count": 0,
              "chunks_posted": len(recap_chunks),
              "debrief_path": pending["debrief_path"]})
        return 0

    emit({"status": "posted",
          "thread_id": thread_id,
          "parent_message_id": parent_message_id,
          "actions_message_id": actions_message_id,
          "channel_id": DISCORD_HOME_CHANNEL,
          "action_count": len(actions),
          "chunks_posted": len(recap_chunks),
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
