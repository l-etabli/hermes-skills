#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
# ]
# ///
"""
meeting-debrief/dispatch.py: post-thread-reply plumbing.

Once the user has answered the validation prompt in the Discord thread
opened by debrief.py, the LLM parses their intent (validated/refused
IDs, optional target corrections) and invokes this script. It edits the
recap message to mark each action ✅/❌, updates (or removes) the
pending state file, and emits the list of actions to dispatch. The LLM
itself then invokes the downstream skills (github-issues, hermes-cron,
google-workspace, obsidian, linear) one by one — this script does NOT
call any of them.

Usage:
  uv run --with requests \
      /opt/data/skills-shared/meeting-debrief/dispatch.py \
      --thread-id <id> \
      [--validated 1,3] [--refused 2] \
      [--correction-json <path>]

  --correction-json points to a JSON file shaped like:
    {"2": {"target": "owner/other-repo"},
     "3": {"when": "2026-05-12"}}
  Overrides are applied to validated actions before they appear in
  to_invoke[].

Required env: WIKI_PATH, DISCORD_BOT_TOKEN.

Output (single JSON object on stdout):
  {"status": "dispatched",
   "to_invoke": [{...action...}, ...],
   "remaining": N}
  {"status": "error", "reason": "...", "detail": "..."}

Exit codes: 0 (dispatched), 1 (runtime error), 2 (arg/env error).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import traceback

import requests

REQUIRED_ENV = ("WIKI_PATH", "DISCORD_BOT_TOKEN")
_missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if _missing:
    print(json.dumps({"status": "error", "reason": "missing-env",
                      "detail": f"missing required env vars: {', '.join(_missing)}",
                      "missing": _missing}, ensure_ascii=False))
    sys.exit(2)

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
PENDING_DIR = WIKI_PATH / ".craig-debriefs-pending"
DISCORD_API = "https://discord.com/api/v10"
# Cloudflare in front of discord.com refuses python-requests' default
# UA with `error code: 1010`. Discord's spec also requires this exact
# format: https://discord.com/developers/docs/reference#user-agent
DISCORD_USER_AGENT = "DiscordBot (https://github.com/l-etabli/hermes-skills, 1.0)"


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def parse_id_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for tok in raw.replace(" ", "").split(","):
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


def apply_correction(action: dict, override: dict) -> dict:
    """Return a copy of action with suggested_action overridden by `override` keys."""
    merged = json.loads(json.dumps(action))
    sa = merged.setdefault("suggested_action", {})
    for k, v in override.items():
        sa[k] = v
    return merged


def annotate_recap(recap_md: str, validated: set[int], refused: set[int]) -> str:
    """Prefix each numbered action line with ✅/❌. Untouched actions stay as-is.

    The recap was rendered by debrief.format_recap() — action lines start
    with "<id>. **[type]** ...". We rewrite the leading "<id>. " to
    "<id>. ✅ " or "<id>. ❌ " when applicable.
    """
    out_lines: list[str] = []
    for line in recap_md.split("\n"):
        stripped = line.lstrip()
        head = stripped.split(".", 1)
        if len(head) == 2 and head[0].isdigit():
            try:
                aid = int(head[0])
            except ValueError:
                out_lines.append(line)
                continue
            if aid in validated:
                rest = head[1].lstrip()
                if not rest.startswith(("✅", "❌")):
                    indent = line[: len(line) - len(stripped)]
                    out_lines.append(f"{indent}{aid}. ✅ {rest}")
                    continue
            if aid in refused:
                rest = head[1].lstrip()
                if not rest.startswith(("✅", "❌")):
                    indent = line[: len(line) - len(stripped)]
                    out_lines.append(f"{indent}{aid}. ❌ {rest}")
                    continue
        out_lines.append(line)
    return "\n".join(out_lines)


def discord_patch_message(channel_id: str, message_id: str, content: str) -> str | None:
    try:
        r = requests.patch(
            f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}",
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": DISCORD_USER_AGENT,
            },
            json={"content": content},
            timeout=15,
        )
    except requests.RequestException as exc:
        return f"network: {exc}"
    if not r.ok:
        return f"discord-{r.status_code}: {r.text[:300]}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--thread-id", required=True)
    ap.add_argument("--validated", default="",
                    help="Comma-separated action IDs the user validated.")
    ap.add_argument("--refused", default="",
                    help="Comma-separated action IDs the user refused.")
    ap.add_argument("--correction-json", default=None,
                    help="Path to a JSON file mapping action_id (str) → override dict for suggested_action.")
    args = ap.parse_args()

    pending_path = PENDING_DIR / f"{args.thread_id}.json"
    if not pending_path.exists():
        emit({"status": "error", "reason": "pending-not-found",
              "detail": str(pending_path)})
        return 1

    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        emit({"status": "error", "reason": "pending-bad-json",
              "detail": str(exc)})
        return 1

    validated = set(parse_id_list(args.validated))
    refused = set(parse_id_list(args.refused))
    overlap = validated & refused
    if overlap:
        emit({"status": "error", "reason": "validated-refused-overlap",
              "detail": f"ids in both lists: {sorted(overlap)}"})
        return 2

    corrections: dict = {}
    if args.correction_json:
        try:
            corrections = json.loads(
                pathlib.Path(args.correction_json).read_text(encoding="utf-8")
            )
        except Exception as exc:
            emit({"status": "error", "reason": "correction-json-bad",
                  "detail": f"{type(exc).__name__}: {exc}"})
            return 2

    actions = pending.get("action_items") or []
    by_id = {int(a["id"]): a for a in actions}

    unknown = (validated | refused) - set(by_id)
    if unknown:
        emit({"status": "error", "reason": "unknown-action-ids",
              "detail": f"ids not in pending: {sorted(unknown)}"})
        return 2

    to_invoke: list[dict] = []
    for aid in sorted(validated):
        action = by_id[aid]
        override = corrections.get(str(aid)) or corrections.get(aid)
        if override:
            action = apply_correction(action, override)
        to_invoke.append(action)

    recap_md = pending.get("recap_markdown") or ""
    if recap_md:
        new_recap = annotate_recap(recap_md, validated, refused)
        if new_recap != recap_md:
            err = discord_patch_message(
                pending["channel_id"], pending["message_id"], new_recap,
            )
            if err:
                emit({"status": "error", "reason": "discord-patch-recap",
                      "detail": err})
                return 1
            pending["recap_markdown"] = new_recap

    remaining = [a for a in actions if int(a["id"]) not in (validated | refused)]
    pending["action_items"] = remaining
    if remaining:
        pending_path.write_text(
            json.dumps(pending, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    else:
        pending_path.unlink(missing_ok=True)

    emit({"status": "dispatched",
          "to_invoke": to_invoke,
          "remaining": len(remaining)})
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
