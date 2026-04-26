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

RECORDING_ID_RE = re.compile(
    # Craig in Components V2 writes `**Recording ID:** \`<id>\``, so we
    # have to skip past stray markdown wrappers (** for bold, backticks
    # for code) on either side of the literal `Recording ID:` and again
    # before the id token. Whitespace + those two characters only.
    r"Recording[\s*]*ID[\s*]*:[\s*`]*([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
ENDED_RE = re.compile(r"Recording\s+ended\.", re.IGNORECASE)

DISCORD_API = "https://discord.com/api/v10"


def fetch_recent_messages() -> tuple[list[dict], str | None]:
    """List the most recent messages of #craig-events. Returns (msgs, err)."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("CRAIG_EVENTS_CHANNEL_ID")
    if not token or not channel_id:
        return [], "missing DISCORD_BOT_TOKEN or CRAIG_EVENTS_CHANNEL_ID env"

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
            return json.load(r), None
    except urllib.error.HTTPError as e:
        return [], f"discord-{e.code}: {e.read()[:200].decode(errors='replace')}"
    except Exception as e:
        return [], f"network: {type(e).__name__}: {e}"


# Craig's well-known bot user ID. We use this to filter the channel
# listing for *Craig's* most recent panel rather than picking up replies
# from humans / other bots / Hermes itself.
CRAIG_BOT_USER_ID = "272937604339466240"


def find_latest_craig_panel() -> tuple[dict | None, str | None]:
    """Pull the most recent message authored by Craig in #craig-events.
    Returns (message, err). Used by the no-arg invocation path: instead
    of trusting the LLM to pass a body or IDs, we go straight to the
    source of truth on Discord."""
    msgs, err = fetch_recent_messages()
    if err:
        return None, err
    for m in msgs:
        author = m.get("author") or {}
        if str(author.get("id") or "") == CRAIG_BOT_USER_ID:
            return m, None
    return None, f"no recent Craig message (author.id={CRAIG_BOT_USER_ID}) in #craig-events"


def discover_message_via_api(craig_id: str) -> tuple[str | None, str | None, str | None]:
    """Find the Discord message that announced this craig_id by listing
    the recent messages of #craig-events and matching on `Recording ID:
    <craig_id>` in the body (content + embeds + components V2). Used as
    a fallback when an explicit craig_id was passed by the caller (e.g.
    a manual debug run)."""
    channel_id = os.environ.get("CRAIG_EVENTS_CHANNEL_ID")
    msgs, err = fetch_recent_messages()
    if err:
        return None, None, err
    needle_lower = f"recording id: {craig_id}".lower()
    for m in msgs:
        if needle_lower in extract_message_text(m).lower():
            return channel_id, str(m["id"]), None
    return None, None, (
        f"no recent message in channel {channel_id} contains "
        f"'Recording ID: {craig_id}'. Note Craig uses Discord Components V2 "
        f"(flags=32800) — text lives in components[].content, not embeds."
    )


def extract_message_text(msg: dict) -> str:
    """Concatenate all text-bearing fields of a Discord message object.

    Covers: top-level `content`, classic `embeds[].title/description/fields`,
    AND Components V2 `components[].content` recursively (Craig's recording
    panel uses Components V2 — `flags & 32768`, IS_COMPONENTS_V2 — and the
    panel text lives inside nested TextDisplay blocks at
    `components[].components[].content`, NOT in `.content` or `.embeds`).
    """
    parts: list[str] = [msg.get("content") or ""]
    for em in msg.get("embeds") or []:
        for f in ("title", "description"):
            v = em.get(f)
            if v:
                parts.append(v)
        for fld in em.get("fields") or []:
            v = fld.get("value")
            if v:
                parts.append(v)

    def walk(node):
        if isinstance(node, dict):
            v = node.get("content")
            if isinstance(v, str) and v:
                parts.append(v)
            for child in node.get("components") or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(msg.get("components") or [])
    return "\n".join(parts)

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


FOLLOWUP_CRON_NAME = "craig-watch-followup"


def _followup_cron_prompt(hermes_cli: str) -> str:
    return f"""\
Tu es active par un cron tick toutes les 5 min pour suivre les recordings
Craig en cours de traitement.

Procedure stricte a chaque tick :
1. Liste $WIKI_PATH/.craig-pending/*.json (via le shell terminal).
2. Si la liste est VIDE :
   - Poste UN message dans le canal $DISCORD_HOME_CHANNEL via
     POST /channels/<id>/messages : '✅ Suivi craig-watch termine,
     tous les recordings ont ete traites.'
   - Recupere le job id de ce cron : `{hermes_cli} cron list` puis
     grep -B1 le nom `{FOLLOWUP_CRON_NAME}` (l'id est l'hex sur la
     ligne au-dessus de `Name:`).
   - Supprime le cron : `{hermes_cli} cron remove <id>`.
   - Termine. NE PAS continuer ni reactiver d'autre skill.
3. Sinon (au moins un pending) : active le skill craig-watch et
   execute sa procedure complete (refetch via Discord API -> si
   'Recording ended.' detecte -> scan.py -> commit/push wiki ->
   ingest llm-wiki -> meeting-debrief si processed.duration_s >= 180).
   IMPORTANT - visibilite : pour chaque pending traite, poste UN
   message de status dans $CRAIG_EVENTS_CHANNEL_ID (reply au message
   Craig original via message_reference.message_id = pending.message_id)
   et EDITE-LE au fil des phases (Recording ended -> Transcrit ->
   Wiki updated -> Debrief poste). Format detaille dans craig-watch
   SKILL.md § "Visibilite dans #craig-events". L'utilisateur DOIT voir
   l'avancement, pas attendre 10 min en aveugle.

Anti-patterns (lecons apprises, NE PAS faire) :
- N'invente jamais de craig_id, channel_id ou message_id : tout vient
  de l'API Discord ou des fichiers .craig-pending/.
- Si un script retourne `error`, surface l'erreur, NE PAS ecrire de
  pending JSON ni bypass le script toi-meme.
- Tous les calls REST Discord doivent inclure le header
  `User-Agent: DiscordBot (https://github.com/l-etabli/hermes-skills, 1.0)`
  (Cloudflare bloque le UA Python par defaut).
- Politique de dedup d'erreurs (cf. craig-watch SKILL.md) toujours
  appliquee : pas de spam si la meme erreur revient tick apres tick.
"""


def ensure_followup_cron() -> str:
    """Make sure a craig-watch-followup cron is scheduled. Idempotent.

    Returns one of: "created", "already-running", "failed: <reason>".
    """
    hermes_cli = os.environ.get("HERMES_CLI_PATH", "/opt/hermes/.venv/bin/hermes")
    if not pathlib.Path(hermes_cli).exists():
        return f"failed: hermes-cli-not-found: {hermes_cli}"

    list_proc = subprocess.run(
        [hermes_cli, "cron", "list"],
        capture_output=True, text=True, timeout=15,
    )
    if FOLLOWUP_CRON_NAME in list_proc.stdout:
        return "already-running"

    proc = subprocess.run(
        [
            hermes_cli, "cron", "create",
            "every 5m",
            "--name", FOLLOWUP_CRON_NAME,
            "--skill", "craig-watch",
            "--skill", "craig-transcript-record",
            "--skill", "llm-wiki",
            "--skill", "meeting-debrief",
            _followup_cron_prompt(hermes_cli),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return f"failed: cron-create: {(proc.stderr or proc.stdout)[:200]}"
    return "created"


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
    # Intentionally NO CLI args. The LLM was observed repeatedly:
    #   - inventing values for --message-id / --channel-id
    #   - feeding a stale body (with a craig_id from a previous test)
    #     via --message, then arguing 'security scan blocks me' when
    #     shell-escaping the zero-width chars failed
    #   - choosing the body-passing path even when SKILL.md said not to.
    # Removing the args entirely makes the wrong path unreachable.
    ap = argparse.ArgumentParser(
        description="Pull the latest Craig recording panel from "
                    "$CRAIG_EVENTS_CHANNEL_ID and write a pending entry. "
                    "Takes no arguments — invoke as a bare script.",
    )
    ap.parse_args()

    panel, err = find_latest_craig_panel()
    if err or not panel:
        emit({"status": "error", "reason": "discord-discovery-failed",
              "detail": err or "no Craig panel found"})
        return 1

    message = extract_message_text(panel)
    if not message.strip():
        emit({"status": "error", "reason": "empty-craig-panel",
              "detail": "Craig's latest message in #craig-events has no "
                        "extractable text (Components V2 walk returned empty). "
                        "Check Message Content Intent + bot Read Message History."})
        return 1

    m = RECORDING_ID_RE.search(message)
    if not m:
        snippet = message.strip().replace("\n", " ")[:200]
        emit({"status": "error", "reason": "format-mismatch",
              "detail": "Craig's latest panel has no Recording ID — format may have changed: "
                        + snippet})
        return 1
    craig_id = m.group(1)
    channel_id = os.environ["CRAIG_EVENTS_CHANNEL_ID"]
    message_id = str(panel["id"])

    if ENDED_RE.search(message):
        # Already ended: skip the pending dance, transcribe now.
        rc, payload, progress = invoke_scan(craig_id)
        payload["craig_id"] = craig_id
        if progress:
            payload["progress"] = progress
        emit(payload)
        return rc

    state_path, is_new = write_pending(craig_id, channel_id, message_id)
    emit({
        "status": "pending" if is_new else "already-pending",
        "craig_id": craig_id,
        "state_path": str(state_path.relative_to(WIKI_PATH)),
        "followup_cron": ensure_followup_cron(),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
