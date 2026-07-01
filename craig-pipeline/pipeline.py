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
craig-pipeline: deterministic orchestrator for the Craig recording pipeline.

Single entry point. Invoked by the `craig-watch-followup` cron tick — the
LLM session that fires the cron does ONE thing: subprocess this script
and report its JSON output. All orchestration (Discord status edits,
git ops, llm-wiki ingest, debrief generation, debrief.py invocation,
self-cron-removal) is handled here in code. Only ONE LLM call remains
in the loop: generation of the debrief JSON via OpenRouter.

Why deterministic: in production the LLM-as-orchestrator was observed
committing without pushing, silently skipping ingest, vandalizing
index.md (writing tool-call response chatter into the file body), and
lying about what it did. Code can't lie about whether it pushed.

For each pending entry in $WIKI_PATH/.craig-pending/*.json:
  0. Refetch the Craig panel via Discord REST. If "Recording ended."
     not yet present: silence (no status post), keep pending, continue.
  1. Post initial status message in CRAIG_EVENTS_CHANNEL_ID (reply to
     the original Craig panel via message_reference). Edit it through
     each phase: Drive poll -> Groq -> push -> ingest -> debrief.
  2. Run craig-transcript-record/scan.py (subprocess), stream its
     progress[] events to the Discord status edit.
  3. git add + commit + push the new transcript.
  4. llm-wiki ingest: agent loop with read_file/list_dir/propose_edit/
     done tools. Edits are buffered, never applied directly by the LLM.
     Apply transactionally. Run guards (line-loss >30%, hallucination
     regex, file >5000 lines, >20 files touched). On guard fail: revert
     working tree, dump debug, continue (transcript stays committed).
  5. Generate debrief JSON via OpenRouter (gemini-3-flash-preview),
     validate against schema.json, write to raw/debriefs/<basename>.json.
  6. Subprocess meeting-debrief/debrief.py to post the recap + thread.
  7. Delete the pending JSON.

When .craig-pending/ becomes empty after the loop:
  - Post one ✅ in DISCORD_HOME_CHANNEL.
  - Self-remove the craig-watch-followup cron via `hermes cron remove`.

Required env: WIKI_PATH, CRAIG_DISCORD_BOT_TOKEN, CRAIG_EVENTS_CHANNEL_ID,
DISCORD_HOME_CHANNEL, OPENROUTER_API_KEY.
Optional env:
  HERMES_SKILLS_DIR (default /opt/data/skills-shared)
  HERMES_CLI_PATH (default /opt/hermes/.venv/bin/hermes)
  LLM_WIKI_SKILL_PATH (default /opt/hermes/skills/research/llm-wiki/SKILL.md)
  OPENROUTER_MODEL (default google/gemini-3-flash-preview)
  MEETING_DEBRIEF_MIN_DURATION_S (default 180; 10 in test)
  PIPELINE_INGEST_MAX_TURNS (default 30)
  PIPELINE_INGEST_MAX_TOOL_CALLS (default 50)

Output: single JSON object on stdout summarizing the run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

import requests
import yaml
from jsonschema import Draft202012Validator

# ----------------------------- env / paths -----------------------------

REQUIRED_ENV = (
    "WIKI_PATH",
    "CRAIG_DISCORD_BOT_TOKEN",
    "CRAIG_EVENTS_CHANNEL_ID",
    "DISCORD_HOME_CHANNEL",
    "OPENROUTER_API_KEY",
)
_missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if _missing:
    print(json.dumps({"status": "error", "reason": "missing-env",
                      "detail": f"missing required env vars: {', '.join(_missing)}",
                      "missing": _missing}, ensure_ascii=False))
    sys.exit(2)

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
PENDING_DIR = WIKI_PATH / ".craig-pending"
DEBUG_DIR = WIKI_PATH / ".craig-pipeline-debug"
TRANSCRIPTS_DIR = WIKI_PATH / "raw" / "transcripts"
DEBRIEFS_DIR = WIKI_PATH / "raw" / "debriefs"

CRAIG_DISCORD_BOT_TOKEN = os.environ["CRAIG_DISCORD_BOT_TOKEN"]
CRAIG_EVENTS_CHANNEL_ID = os.environ["CRAIG_EVENTS_CHANNEL_ID"]
DISCORD_HOME_CHANNEL = os.environ["DISCORD_HOME_CHANNEL"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

SKILLS_ROOT = pathlib.Path(os.environ.get("HERMES_SKILLS_DIR", "/opt/data/skills-shared"))
SCAN_PY = SKILLS_ROOT / "craig-transcript-record" / "scan.py"
DEBRIEF_PY = SKILLS_ROOT / "meeting-debrief" / "debrief.py"
DEBRIEF_SCHEMA = SKILLS_ROOT / "meeting-debrief" / "schema.json"
HERMES_CLI = pathlib.Path(os.environ.get("HERMES_CLI_PATH", "/opt/hermes/.venv/bin/hermes"))
LLM_WIKI_SKILL_PATH = pathlib.Path(
    os.environ.get("LLM_WIKI_SKILL_PATH", "/opt/hermes/skills/research/llm-wiki/SKILL.md")
)

OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-3-flash-preview")
MIN_DURATION_S = int(os.environ.get("MEETING_DEBRIEF_MIN_DURATION_S", "180"))
INGEST_MAX_TURNS = int(os.environ.get("PIPELINE_INGEST_MAX_TURNS", "30"))
INGEST_MAX_TOOL_CALLS = int(os.environ.get("PIPELINE_INGEST_MAX_TOOL_CALLS", "50"))

DISCORD_API = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://github.com/l-etabli/hermes-skills, 1.0)"
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"

FOLLOWUP_CRON_NAME = "craig-watch-followup"

# The github-app refresher sidecar mints a fresh installation token
# every 45 min and writes it as `GITHUB_TOKEN=<token>` to this file.
# The Hermes gateway reloads it via load_dotenv() on each LLM session,
# but a subprocess like pipeline.py inherits a snapshot env that may
# predate the latest refresh — and `terminal.env_passthrough` on
# config.yaml is no help if the sandbox launched before the token
# rotation. So we always re-read the file at push time.
GITHUB_ENV_FILE = pathlib.Path(os.environ.get("HERMES_ENV_FILE", "/opt/data/.env"))

# Hallucination markers we've actually seen the LLM write into wiki
# pages: tool-call response chatter that should never end up on disk.
HALLUCINATION_RES = [
    re.compile(r"File unchanged since last read", re.IGNORECASE),
    re.compile(r"earlier read_file result", re.IGNORECASE),
    re.compile(r"response from earlier tool", re.IGNORECASE),
    re.compile(r"<system-reminder>", re.IGNORECASE),
    re.compile(r"```tool_call|```tool_result", re.IGNORECASE),
]

# ----------------------------- discord helpers -----------------------------

def _discord_request(method: str, path: str, **kwargs) -> tuple[dict | None, str | None]:
    headers = {
        "Authorization": f"Bot {CRAIG_DISCORD_BOT_TOKEN}",
        "User-Agent": DISCORD_USER_AGENT,
    }
    if "json" in kwargs:
        headers["Content-Type"] = "application/json"
    headers.update(kwargs.pop("headers", None) or {})
    try:
        r = requests.request(method, f"{DISCORD_API}{path}",
                             headers=headers, timeout=15, **kwargs)
    except requests.RequestException as exc:
        return None, f"network: {type(exc).__name__}: {exc}"
    if not r.ok:
        return None, f"discord-{r.status_code}: {r.text[:300]}"
    if r.status_code == 204 or not r.text:
        return {}, None
    try:
        return r.json(), None
    except ValueError:
        return None, f"discord-bad-json: {r.text[:200]}"


def discord_post(channel_id: str, content: str, reply_to: str | None = None) -> tuple[str | None, str | None]:
    body: dict = {"content": content[:2000]}
    if reply_to:
        # fail_if_not_exists=false so the post still works if Craig's panel
        # was deleted between listener-time and pipeline-time.
        body["message_reference"] = {
            "message_id": reply_to,
            "fail_if_not_exists": False,
        }
        body["allowed_mentions"] = {"replied_user": False}
    msg, err = _discord_request("POST", f"/channels/{channel_id}/messages", json=body)
    if err:
        return None, err
    return str(msg["id"]), None


def discord_edit(channel_id: str, message_id: str, content: str) -> str | None:
    _, err = _discord_request(
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
        json={"content": content[:2000]},
    )
    return err


def discord_get_message(channel_id: str, message_id: str) -> tuple[dict | None, str | None]:
    return _discord_request("GET", f"/channels/{channel_id}/messages/{message_id}")


def discord_create_thread(channel_id: str, message_id: str, name: str) -> tuple[str | None, str | None]:
    """Open a thread anchored on `message_id`. Returns (thread_id, err).
    auto_archive_duration matches the channel-default (60 min) so our
    fallback threads behave like the Hermes auto-threads — short
    archive window, anything older than 1h after pipeline completion
    is closed by Discord automatically."""
    msg, err = _discord_request(
        "POST",
        f"/channels/{channel_id}/messages/{message_id}/threads",
        json={"name": name[:100], "auto_archive_duration": 60},
    )
    if err:
        return None, err
    return str(msg["id"]), None


def discord_unarchive_thread(thread_id: str) -> str | None:
    """Reopen a thread if Discord auto-archived it. Hermes auto-threads
    archive after 24h by default; a slow drive cook + delayed pipeline
    tick can cross that boundary. Returns err or None."""
    _, err = _discord_request(
        "PATCH",
        f"/channels/{thread_id}",
        json={"archived": False},
    )
    return err


def find_existing_auto_thread(channel_id: str, craig_id: str,
                              panel_id: str | None) -> str | None:
    """Try to locate an existing thread in #craig-events whose anchor
    message references this `craig_id` — typically the auto-thread
    Hermes creates on the listener's response (Hermes is configured
    with `auto_thread: true`). Reusing it keeps the status flow next
    to the listener's earlier output instead of spawning a parallel
    "Recording <date>" thread.

    Active threads must be listed via the guild-level endpoint
    `/guilds/{gid}/threads/active` — Discord's `/channels/{cid}/threads/
    active` does NOT exist (returns 404, observed 2026-04-26 — every
    earlier reuse attempt fell through to the create-our-own branch
    because of this 404). We resolve guild_id by GETting the channel
    once, then filter the guild's active threads by parent_id.
    Archived threads keep the channel-level endpoint, which is real.

    For threads created from a message Discord guarantees
    `thread.id == message.id`. Walk candidates, fetch the message at
    `thread.id`, match `craig_id` against its rendered body."""
    targets: list[dict] = []

    ch_obj, err = _discord_request("GET", f"/channels/{channel_id}")
    guild_id = (ch_obj or {}).get("guild_id") if not err else None
    if guild_id:
        resp, err = _discord_request("GET", f"/guilds/{guild_id}/threads/active")
        if not err and resp:
            for t in resp.get("threads", []):
                if str(t.get("parent_id")) == str(channel_id):
                    targets.append(t)

    resp, err = _discord_request(
        "GET", f"/channels/{channel_id}/threads/archived/public?limit=20",
    )
    if not err and resp:
        targets.extend(resp.get("threads") or [])

    seen: set[str] = set()
    for t in targets:
        anchor_msg_id = t.get("id")
        if not anchor_msg_id or anchor_msg_id in seen:
            continue
        seen.add(anchor_msg_id)

        if panel_id and str(anchor_msg_id) == str(panel_id):
            return str(anchor_msg_id)

        msg, merr = _discord_request(
            "GET", f"/channels/{channel_id}/messages/{anchor_msg_id}",
        )
        if merr or not msg:
            continue
        body = extract_message_text(msg)
        if craig_id and craig_id in body:
            return str(anchor_msg_id)
    return None


# Copied from craig-listener/listener.py (Components V2 walker). Craig's
# recording panel uses Discord Components V2 (flags & 32768) and the
# panel text lives nested in components[].components[].content, not in
# .content or .embeds — a plain msg["content"] would return empty.
def extract_message_text(msg: dict) -> str:
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


ENDED_RE = re.compile(r"Recording\s+ended\.", re.IGNORECASE)


# ----------------------------- status renderer -----------------------------

PHASE_ICON = {"done": "✅", "inprogress": "⏳", "error": "⚠️"}


def fmt_phase(kind: str, text: str) -> str:
    return f"{PHASE_ICON.get(kind, '•')} {text}"


def parse_first_seen(pending: dict) -> str:
    raw = pending.get("first_seen_at") or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H%M UTC")
    except Exception:
        return raw or "?"


# ----------------------------- pending I/O -----------------------------

def list_pending() -> list[pathlib.Path]:
    if not PENDING_DIR.exists():
        return []
    return sorted(PENDING_DIR.glob("*.json"))


def load_pending(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_pending(path: pathlib.Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def delete_pending(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ----------------------------- scan.py wrapper -----------------------------

def run_scan(craig_id: str, on_progress) -> tuple[int, dict, list[dict]]:
    """Run scan.py as a subprocess, stream progress events through
    on_progress(event_dict). Returns (rc, final_result, all_progress)."""
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
    progress: list[dict] = []
    result: dict | None = None
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("status") == "progress":
            progress.append(obj)
            try:
                on_progress(obj)
            except Exception:
                pass
        else:
            result = obj
    proc.wait()
    if result is None:
        stderr = (proc.stderr.read() if proc.stderr else "")[-500:]
        return proc.returncode or 1, {
            "status": "error", "reason": "scan-py-bad-json", "detail": stderr,
        }, progress
    return proc.returncode, result, progress


# ----------------------------- git ops -----------------------------

GIT_AUTHOR_NAME = os.environ.get("HERMES_GIT_AUTHOR_NAME", "Hermes Pipeline")
GIT_AUTHOR_EMAIL = os.environ.get("HERMES_GIT_AUTHOR_EMAIL", "hermes-pipeline@l-etabli.local")


def git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    # Force author identity inline so the script works in any subshell,
    # without depending on `git config --global user.email/user.name`
    # being set in the sandbox. Hermes spawns terminal sandboxes
    # without a persistent global git config, and the previous tick's
    # success was an artefact of the LLM session having configured it
    # ad-hoc — not something we can rely on.
    config_args = (
        "-c", f"user.name={GIT_AUTHOR_NAME}",
        "-c", f"user.email={GIT_AUTHOR_EMAIL}",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *config_args, *args],
        capture_output=True, text=True, check=check,
    )


def _read_github_token() -> str | None:
    """Pull the freshest GITHUB_TOKEN from the instance .env (the file
    the github-app refresher sidecar writes to). Returns None if the
    file is unreadable or has no GITHUB_TOKEN line — in which case
    git push will fail with a clear push-no-token error rather than
    git's interactive-prompt-on-no-tty 'No such device' confusion."""
    try:
        text = GITHUB_ENV_FILE.read_text(encoding="utf-8")
    except Exception:
        # Fall back to env var (set if terminal.env_passthrough exposed it).
        return os.environ.get("GITHUB_TOKEN") or None
    for line in text.splitlines():
        if line.startswith("GITHUB_TOKEN="):
            return line.split("=", 1)[1].strip() or None
    return os.environ.get("GITHUB_TOKEN") or None


def _push_url_with_token(repo: pathlib.Path, token: str) -> str | None:
    """Compose an https push URL with the token embedded inline so we
    don't have to touch .git/config or write a credential helper to
    disk (both leak the token long-term). The token is passed only to
    this single git push process and never stored."""
    try:
        remote = git(repo, "remote", "get-url", "origin").stdout.strip()
    except subprocess.CalledProcessError:
        return None
    m = re.match(r"^https://github\.com/([^/]+/[^/]+?)(\.git)?$", remote)
    if not m:
        return None
    return f"https://x-access-token:{token}@github.com/{m.group(1)}.git"


def git_commit_push(repo: pathlib.Path, files: list[str], msg: str) -> tuple[str | None, str | None]:
    """Stage `files`, commit with msg if there's anything new, then push.
    The push runs unconditionally because a previous tick may have
    committed locally but failed to push (e.g. the original push-fail
    we just fixed left an unpushed commit on local main); skipping push
    on `nothing to commit` would leave that orphan forever.

    Returns (sha, err). sha is the new commit's short hash on commit,
    or None if commit was a no-op AND the push was a no-op."""
    try:
        for f in files:
            git(repo, "add", "--", f)
        # `git status --porcelain` lists untracked entries too (??),
        # so a clean working tree with stray untracked dirs (like the
        # gitignored .craig-debriefs-pending/) would falsely report
        # "something to commit" and trigger an empty `git commit` that
        # dies with "nothing to commit, working tree clean". Use
        # `diff --cached --quiet` instead — exit 0 means nothing staged.
        diff = git(repo, "diff", "--cached", "--quiet", check=False)
        new_commit = False
        if diff.returncode != 0:
            git(repo, "commit", "-m", msg)
            new_commit = True

        token = _read_github_token()
        if not token:
            return None, "push-no-token: GITHUB_TOKEN absent from /opt/data/.env"
        push_url = _push_url_with_token(repo, token)
        if not push_url:
            return None, "push-bad-remote: origin is not an https://github.com/... URL"

        push = subprocess.run(
            ["git", "-C", str(repo), "push", push_url, "HEAD"],
            capture_output=True, text=True,
        )
        if push.returncode != 0:
            scrubbed = (push.stderr or push.stdout).replace(token, "***")[:200]
            return None, f"push-failed: {scrubbed}"

        if not new_commit:
            # Nothing changed in this call AND push was up-to-date → no-op.
            up_to_date = "Everything up-to-date" in (push.stderr or "") or not (push.stderr or "").strip()
            if up_to_date:
                return None, None
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        return head[:8], None
    except subprocess.CalledProcessError as exc:
        return None, f"git: {exc.cmd[1:]} -> {exc.stderr[:200]}"


def github_link(repo: pathlib.Path, sha: str) -> str:
    """Best-effort GitHub commit URL from origin remote. Falls back to sha."""
    try:
        remote = git(repo, "remote", "get-url", "origin").stdout.strip()
    except subprocess.CalledProcessError:
        return sha
    m = re.match(r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/.]+)(\.git)?$", remote)
    if not m:
        return sha
    return f"https://github.com/{m.group(1)}/commit/{sha}"


# ----------------------------- openrouter -----------------------------

def openrouter_chat(messages: list[dict], tools: list[dict] | None = None,
                    response_format: dict | None = None,
                    model: str | None = None) -> tuple[dict | None, str | None]:
    body: dict = {
        "model": model or OPENROUTER_MODEL,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if response_format:
        body["response_format"] = response_format
    try:
        r = requests.post(
            OPENROUTER_API,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/l-etabli/hermes-skills",
                "X-Title": "craig-pipeline",
            },
            json=body,
            timeout=120,
        )
    except requests.RequestException as exc:
        return None, f"network: {type(exc).__name__}: {exc}"
    if not r.ok:
        return None, f"openrouter-{r.status_code}: {r.text[:400]}"
    try:
        return r.json(), None
    except ValueError:
        return None, f"openrouter-bad-json: {r.text[:200]}"


# ----------------------------- llm-wiki ingest -----------------------------

INGEST_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the wiki vault. Path is wiki-relative.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Wiki-relative path (e.g. 'index.md', 'people/jerome.md')."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries in a wiki directory. Path is wiki-relative.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Wiki-relative directory path. Use '.' for the wiki root."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_edit",
            "description": (
                "Buffer an edit to the wiki. Does NOT touch the filesystem — "
                "the orchestrator validates and applies all buffered edits "
                "atomically when you call `done`. Returns 'buffered' or "
                "'rejected: <reason>'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "append", "replace", "patch"],
                        "description": (
                            "create: new file. append: append text to existing file. "
                            "replace: overwrite full content (rare, use for refactors). "
                            "patch: find/replace a single substring."
                        ),
                    },
                    "path": {"type": "string", "description": "Wiki-relative path. Cannot be inside raw/ or .craig-*/."},
                    "content": {
                        "type": "string",
                        "description": "Required for create/append/replace. Full content (create/replace) or text to append (append).",
                    },
                    "find": {"type": "string", "description": "Required for patch. Exact substring to find."},
                    "replace_with": {"type": "string", "description": "Required for patch. Replacement text."},
                },
                "required": ["action", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Signal end of ingest session. Pass a short rationale describing what was changed.",
            "parameters": {
                "type": "object",
                "properties": {"rationale": {"type": "string"}},
                "required": ["rationale"],
            },
        },
    },
]


INGEST_OVERRIDE = """\

---

# Override (orchestrator-injected, mandatory)

Tu n'as PAS access à `write_file`, `git`, `bash` ou shell d'aucune sorte.
Pour proposer des éditions au vault, utilise EXCLUSIVEMENT l'outil
`propose_edit` qui buffere ta demande sans toucher au filesystem.
L'orchestrator validera (guards anti-vandalisme) et appliquera les
éditions de manière transactionnelle quand tu appelleras `done`.

Outils disponibles : `read_file`, `list_dir`, `propose_edit`, `done`.

Règles strictes :
- N'écris JAMAIS dans `raw/` (transcripts + debriefs gérés par le code).
- N'écris JAMAIS de chatter de tool-call, de `<system-reminder>`, ou
  de paraphrase d'un read_file dans le contenu d'un fichier wiki.
- Préfère `append`/`patch` à `replace` (replace écrase tout le fichier).
- Si tu ne sais pas quoi faire, appelle `done` avec un rationale court.
  Un ingest vide vaut mieux qu'un ingest halluciné.
- Limite ferme : 30 turns max, 50 tool calls max. Au-delà l'orchestrator
  coupe court et récupère ton buffer en l'état.
"""


def _resolve_wiki_path(p: str, *, for_write: bool) -> tuple[pathlib.Path | None, str | None]:
    """Resolve a user-supplied wiki-relative path.

    Common rejects (read AND write): empty, absolute, .. escape, .git/,
    or anything resolving outside the wiki root.

    Write-only rejects: raw/ (transcripts + debriefs are owned by the
    pipeline, not the LLM), and .craig-*/ (runtime state). Reads on
    those paths ARE allowed — the LLM legitimately needs to read the
    transcript it's about to ingest, otherwise it hallucinates the
    contents and proposes patches that miss (this exact failure mode
    was observed on 2026-04-26 — the agent guessed a `patch find` for
    log.md that wasn't there, the apply step blew up, the guards
    caught it but the wiki ingest was a no-op)."""
    if not p or not isinstance(p, str):
        return None, "empty path"
    if p.startswith("/"):
        return None, "absolute path not allowed"
    norm = pathlib.PurePosixPath(p)
    if any(part == ".." for part in norm.parts):
        return None, "path escape (..) not allowed"
    parts = norm.parts
    if parts and parts[0] == ".git":
        return None, ".git/ is off-limits"
    if for_write:
        if parts and parts[0] == "raw":
            return None, "raw/ is read-only for ingest (transcripts + debriefs are pipeline-owned)"
        if parts and parts[0].startswith(".craig"):
            return None, ".craig-*/ is runtime state, not wiki content"
    abs_path = (WIKI_PATH / norm).resolve()
    try:
        abs_path.relative_to(WIKI_PATH.resolve())
    except ValueError:
        return None, "path resolves outside wiki root"
    return abs_path, None


def _is_safe_wiki_path(p: str) -> tuple[pathlib.Path | None, str | None]:
    """Backwards-compat shim — defaults to write-mode (the strictest).
    New code should call _resolve_wiki_path directly with for_write=."""
    return _resolve_wiki_path(p, for_write=True)


def _wiki_baseline(wiki: pathlib.Path) -> dict[str, dict]:
    """sha256 + line-count snapshot for every .md outside raw/ and
    .craig-*/. Used to compute diffs and enforce line-loss guards."""
    out: dict[str, dict] = {}
    for p in wiki.rglob("*.md"):
        rel = p.relative_to(wiki).as_posix()
        if rel.startswith("raw/") or rel.startswith(".craig"):
            continue
        if "/.git/" in str(p) or rel.startswith(".git/"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        out[rel] = {
            "sha": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "lines": len(text.splitlines()),
        }
    return out


def _hallucination_match(text: str) -> str | None:
    for rx in HALLUCINATION_RES:
        if rx.search(text):
            return rx.pattern
    return None


def _read_llm_wiki_skill() -> str:
    """Best-effort load of the upstream llm-wiki SKILL.md. If unreachable
    (mount missing, path env wrong) we fall back to a brief built-in
    description so the agent still has guidance — better an imperfect
    ingest than crashing the pipeline."""
    if LLM_WIKI_SKILL_PATH.exists():
        try:
            return LLM_WIKI_SKILL_PATH.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return (
        "# llm-wiki (fallback brief)\n\n"
        "Tu maintiens un vault Markdown personnel (Obsidian-style avec "
        "liens [[X]]). Pour chaque transcript ingéré : ajoute une entrée "
        "courte dans log.md (date + résumé 1 ligne), met à jour les pages "
        "people/ et concepts/ mentionnées (ajoute des liens vers le "
        "transcript), crée de nouvelles pages concepts si pertinent. "
        "Ne touche PAS à raw/. Préfère append/patch à replace.\n"
    )


def _handle_ingest_tool(name: str, args: dict, buffer: list[dict]) -> dict:
    if name == "read_file":
        # Reads can target raw/transcripts/<file>.md — the LLM needs to
        # actually see the transcript it's ingesting, otherwise it
        # patches log.md from a hallucinated body.
        abs_path, err = _resolve_wiki_path(args.get("path", ""), for_write=False)
        if err:
            return {"error": err}
        if not abs_path or not abs_path.exists():
            return {"error": "file not found"}
        if abs_path.is_dir():
            return {"error": "is a directory; use list_dir"}
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"error": f"read failed: {type(exc).__name__}: {exc}"}
        # Cap below the agent loop's 8KB tool-result string truncation
        # (see _agent_loop) so json.dumps wrapping never slices a UTF-8
        # rune in the middle and feeds the LLM a malformed result.
        if len(text) > 6_000:
            return {"content": text[:6_000], "truncated": True, "total_bytes": len(text)}
        return {"content": text}

    if name == "list_dir":
        path = args.get("path", "")
        if path in (".", ""):
            abs_path = WIKI_PATH
        else:
            abs_path, err = _resolve_wiki_path(path, for_write=False)
            if err:
                return {"error": err}
        if not abs_path or not abs_path.exists() or not abs_path.is_dir():
            return {"error": "not a directory"}
        entries = []
        for e in sorted(abs_path.iterdir()):
            rel = e.relative_to(WIKI_PATH).as_posix()
            # .git is genuinely off-limits even for listing — no value
            # to the LLM, lots of clutter. .craig-*/ is gitignored
            # state. raw/ on the other hand IS legitimate to list when
            # the LLM wants to cite siblings of the transcript.
            if rel.startswith(".git") or rel.startswith(".craig"):
                continue
            entries.append({"name": e.name, "type": "dir" if e.is_dir() else "file"})
        return {"entries": entries}

    if name == "propose_edit":
        action = args.get("action")
        path = args.get("path", "")
        # Writes still bound to the read-write surface (excludes raw/
        # and .craig-*/) — only reads got the broader path.
        abs_path, err = _resolve_wiki_path(path, for_write=True)
        if err:
            return {"status": "rejected", "reason": err}
        if not abs_path:
            return {"status": "rejected", "reason": "bad path"}
        if action == "create":
            if abs_path.exists():
                return {"status": "rejected", "reason": "file already exists; use append/patch"}
            content = args.get("content", "")
            if not content.strip():
                return {"status": "rejected", "reason": "empty content"}
            buffer.append({"action": "create", "path": path, "content": content})
            return {"status": "buffered"}
        if action == "append":
            if not abs_path.exists():
                return {"status": "rejected", "reason": "file does not exist; use create"}
            content = args.get("content", "")
            if not content.strip():
                return {"status": "rejected", "reason": "empty content"}
            buffer.append({"action": "append", "path": path, "content": content})
            return {"status": "buffered"}
        if action == "replace":
            if not abs_path.exists():
                return {"status": "rejected", "reason": "file does not exist; use create"}
            content = args.get("content", "")
            if not content.strip():
                return {"status": "rejected", "reason": "empty content"}
            buffer.append({"action": "replace", "path": path, "content": content})
            return {"status": "buffered"}
        if action == "patch":
            if not abs_path.exists():
                return {"status": "rejected", "reason": "file does not exist"}
            find = args.get("find", "")
            replace_with = args.get("replace_with", "")
            if not find:
                return {"status": "rejected", "reason": "empty find"}
            buffer.append({"action": "patch", "path": path, "find": find, "replace_with": replace_with})
            return {"status": "buffered"}
        return {"status": "rejected", "reason": f"unknown action: {action}"}

    if name == "done":
        return {"status": "done"}

    return {"error": f"unknown tool: {name}"}


def _agent_loop(transcript_path: pathlib.Path) -> tuple[list[dict], str, str | None]:
    """Drive the OpenRouter agent loop. Returns (buffer, terminal_reason,
    rationale). terminal_reason ∈ {'done', 'turn-limit', 'tool-call-limit',
    'no-tool-calls', 'llm-error'}."""
    skill_md = _read_llm_wiki_skill()
    system = skill_md + INGEST_OVERRIDE
    transcript_rel = transcript_path.relative_to(WIKI_PATH).as_posix()
    user_prompt = (
        f"Voici un nouveau transcript à ingérer dans le wiki : `{transcript_rel}`.\n\n"
        f"1. Lis-le avec `read_file`.\n"
        f"2. Explore le vault (people/, concepts/, log.md, index.md...) avec "
        f"`list_dir` et `read_file` pour repérer les pages existantes "
        f"correspondant aux entités/concepts/personnes mentionnés.\n"
        f"3. Propose les éditions nécessaires via `propose_edit` : entrée "
        f"de log, mise à jour de pages personnes/concepts, nouvelles pages "
        f"concept si pertinent, mise à jour des index/dataview.\n"
        f"4. Termine avec `done` quand tu as fini, ou immédiatement avec "
        f"`done` si le transcript ne mérite pas d'ingest (smalltalk, test "
        f"technique, recording vide).\n"
    )
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    buffer: list[dict] = []
    rationale: str | None = None
    tool_calls_total = 0

    for turn in range(INGEST_MAX_TURNS):
        resp, err = openrouter_chat(messages, tools=INGEST_TOOLS)
        if err:
            return buffer, "llm-error", err
        try:
            choice = resp["choices"][0]
            assistant = choice["message"]
        except (KeyError, IndexError, TypeError):
            return buffer, "llm-error", f"bad-response: {str(resp)[:300]}"

        # Forward the assistant message verbatim — OpenAI/OpenRouter
        # require the same tool_calls payload to be replayed back
        # before the matching `tool` messages.
        messages.append({
            "role": "assistant",
            "content": assistant.get("content") or "",
            "tool_calls": assistant.get("tool_calls") or [],
        })
        tool_calls = assistant.get("tool_calls") or []
        if not tool_calls:
            return buffer, "no-tool-calls", rationale

        done_called = False
        for tc in tool_calls:
            tool_calls_total += 1
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _handle_ingest_tool(name, args, buffer)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False)[:8000],
            })
            if name == "done":
                done_called = True
                rationale = args.get("rationale")
            if tool_calls_total >= INGEST_MAX_TOOL_CALLS:
                return buffer, "tool-call-limit", rationale

        if done_called:
            return buffer, "done", rationale

    return buffer, "turn-limit", rationale


def llm_wiki_ingest(transcript_path: pathlib.Path) -> dict:
    """Run the agent loop, apply edits transactionally, enforce guards,
    commit + push on success. Returns a summary dict with keys: status
    ('committed'|'no-op'|'aborted'|'llm-error'), reason?, sha?, n_files,
    n_created, n_updated, debug_path?, terminal_reason."""
    baseline = _wiki_baseline(WIKI_PATH)
    buffer, terminal, rationale = _agent_loop(transcript_path)

    summary: dict = {
        "terminal_reason": terminal,
        "rationale": rationale,
        "buffered_edits": len(buffer),
    }
    if terminal == "llm-error":
        summary["status"] = "llm-error"
        summary["reason"] = rationale
        return summary
    if not buffer:
        summary["status"] = "no-op"
        summary["n_files"] = 0
        summary["n_created"] = 0
        summary["n_updated"] = 0
        return summary

    # Touched-files tally (path -> existed_before? for revert step).
    touched: dict[str, bool] = {}
    n_created = 0
    applied: list[dict] = []

    try:
        for edit in buffer:
            path = edit["path"]
            abs_path, err = _is_safe_wiki_path(path)
            if err or not abs_path:
                raise RuntimeError(f"path validation regressed for {path}: {err}")
            existed_before = abs_path.exists()
            if path not in touched:
                touched[path] = existed_before
            if edit["action"] == "create":
                if abs_path.exists():
                    raise RuntimeError(f"create raced: {path} exists")
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                content = edit["content"]
                if not content.endswith("\n"):
                    content += "\n"
                abs_path.write_text(content, encoding="utf-8")
                n_created += 1 if not existed_before else 0
                applied.append(edit)
            elif edit["action"] == "append":
                size = abs_path.stat().st_size
                needs_sep = False
                if size > 0:
                    with abs_path.open("rb") as fh:
                        fh.seek(-1, 2)
                        needs_sep = fh.read(1) != b"\n"
                content = edit["content"]
                if not content.endswith("\n"):
                    content += "\n"
                with abs_path.open("a", encoding="utf-8") as fh:
                    fh.write(("\n" if needs_sep else "") + content)
                applied.append(edit)
            elif edit["action"] == "replace":
                content = edit["content"]
                if not content.endswith("\n"):
                    content += "\n"
                abs_path.write_text(content, encoding="utf-8")
                applied.append(edit)
            elif edit["action"] == "patch":
                cur = abs_path.read_text(encoding="utf-8", errors="replace")
                if edit["find"] not in cur:
                    raise RuntimeError(f"patch find not in {path}")
                # Replace only first occurrence — multiple identical
                # substrings in a wiki page are usually intentional
                # (links repeated across sections). Forcing the LLM to
                # disambiguate by enlarging `find` is safer than
                # silently rewriting all occurrences.
                new = cur.replace(edit["find"], edit["replace_with"], 1)
                abs_path.write_text(new, encoding="utf-8")
                applied.append(edit)
    except Exception as exc:
        _revert_touched(touched)
        debug_path = _dump_debug(buffer, baseline, f"apply-failed: {exc}")
        summary["status"] = "aborted"
        summary["reason"] = f"apply-failed: {type(exc).__name__}: {exc}"
        summary["debug_path"] = debug_path
        return summary

    # Guards — applied to working tree state.
    guard_fail = _check_guards(touched, baseline, applied)
    if guard_fail:
        _revert_touched(touched)
        debug_path = _dump_debug(buffer, baseline, guard_fail)
        summary["status"] = "aborted"
        summary["reason"] = guard_fail
        summary["debug_path"] = debug_path
        return summary

    # Commit + push.
    files = sorted(touched.keys())
    basename = transcript_path.stem
    sha, err = git_commit_push(WIKI_PATH, files, f"wiki: ingest {basename}")
    if err:
        # Hard to recover cleanly here — the working tree edits are
        # legitimate per guards, but couldn't be pushed. Leave them in
        # the working tree so a human can inspect; the next pipeline
        # tick won't try to re-ingest because the pending JSON will
        # be deleted only after the debrief step. Actually — we DO
        # delete pending after debrief, so partial commits would loop.
        # Safer: revert and report.
        _revert_touched(touched)
        debug_path = _dump_debug(buffer, baseline, f"git-push-failed: {err}")
        summary["status"] = "aborted"
        summary["reason"] = err
        summary["debug_path"] = debug_path
        return summary

    n_updated = sum(1 for p, existed in touched.items() if existed)
    n_created_final = sum(1 for p, existed in touched.items() if not existed)
    summary["status"] = "committed" if sha else "no-op"
    summary["sha"] = sha
    summary["n_files"] = len(touched)
    summary["n_created"] = n_created_final
    summary["n_updated"] = n_updated
    return summary


def _check_guards(touched: dict[str, bool], baseline: dict[str, dict],
                  applied: list[dict]) -> str | None:
    """Returns None if all guards pass, else a short reason string."""
    if len(touched) > 20:
        return f"runaway-files: {len(touched)} > 20"

    # Hallucination regex on every inserted-content payload.
    for edit in applied:
        haystacks = []
        if edit["action"] in ("create", "append", "replace"):
            haystacks.append(edit.get("content") or "")
        elif edit["action"] == "patch":
            haystacks.append(edit.get("replace_with") or "")
        for h in haystacks:
            hit = _hallucination_match(h)
            if hit:
                return f"hallucination-regex: {hit} in edit on {edit['path']}"

    # Per-file checks: line-loss for existing, size cap for new.
    for path, existed_before in touched.items():
        abs_path = WIKI_PATH / path
        if not abs_path.exists():
            return f"file-vanished: {path}"
        text = abs_path.read_text(encoding="utf-8", errors="replace")
        new_lines = len(text.splitlines())
        if existed_before:
            old_lines = baseline.get(path, {}).get("lines", 0)
            if old_lines > 0 and new_lines < 0.7 * old_lines:
                return f"line-loss: {path} {old_lines} -> {new_lines}"
        else:
            if new_lines > 5000:
                return f"oversized-new-file: {path} {new_lines} lines"
    return None


def _revert_touched(touched: dict[str, bool]) -> None:
    """Best-effort revert of working-tree edits. For files that existed
    before, `git checkout -- <path>` restores the committed version. For
    files that didn't exist (created during this run), `rm` them."""
    for path, existed_before in touched.items():
        abs_path = WIKI_PATH / path
        try:
            if existed_before:
                git(WIKI_PATH, "checkout", "--", path, check=False)
            else:
                if abs_path.exists():
                    abs_path.unlink()
        except Exception:
            pass


def _dump_debug(buffer: list[dict], baseline: dict[str, dict], reason: str) -> str:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = DEBUG_DIR / f"{ts}.json"
    payload = {
        "reason": reason,
        "buffered_edits": buffer,
        "baseline_files": len(baseline),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path.relative_to(WIKI_PATH))


# ----------------------------- debrief generation -----------------------------

# Mirrors meeting-debrief/debrief.py:parse_frontmatter byte-for-byte —
# duplicated rather than shared because each skill is a self-contained
# `uv run` script (no inter-skill package). Keep the two in sync.
def parse_frontmatter(md_text: str) -> tuple[dict, str]:
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


DEBRIEF_SYSTEM = """\
Tu es un assistant qui transforme un transcript de meeting en un debrief
JSON STRICT conforme au schéma fourni. Règles absolues :

- Tu te bases EXCLUSIVEMENT sur le contenu du transcript fourni dans le
  user message. Tu n'inventes RIEN qui ne soit textuellement dans le
  transcript : pas d'entité, pas de personne, pas d'action, pas de
  décision, pas de date.
- **Langue de sortie** : tu rédiges TOUS les champs textuels (`tldr`,
  `decisions`, `open_questions`, `action_items[].description`) dans la
  MÊME LANGUE que le transcript. Transcript en français → debrief en
  français. Anglais → anglais. Transcript multilingue → langue
  dominante. Ne traduis JAMAIS vers l'anglais "par défaut".
- **Couvre l'intégralité du transcript** : les décisions stratégiques,
  business, ou gouvernance arrivent souvent en fin de conversation,
  après la partie technique. Analyse jusqu'au dernier message — ne
  t'arrête pas aux premiers segments.
- Si le transcript ne contient AUCUNE action concrète (smalltalk,
  brainstorming sans engagement, test technique, recording silencieux),
  retourne `"action_items": []`. UN debrief vide est meilleur qu'UN
  debrief halluciné.
- Le `tldr` doit être 2-5 puces markdown, courtes. Le recap markdown
  rendu côté Discord (TLDR + decisions + open_questions + actions) doit
  rester sous ~3500 chars : reste concis, mais n'élague pas une décision
  importante pour gagner 50 chars. Le rendu Discord splittera en
  plusieurs messages si nécessaire.
- Pour chaque action_item : `id` 1-based unique, `type` ∈ taxonomie du
  schema, `suggested_action.kind` ∈ enum schema, `target` inféré
  raisonnablement (préfère un repo plausible owner/repo, un email
  clair). `confidence` ∈ [0,1], honnête : <0.6 si doute.
- Réponds UNIQUEMENT par un objet JSON valide, sans ```json fence ni
  texte autour.
"""


def generate_debrief(transcript_path: pathlib.Path, schema: dict) -> tuple[dict | None, str | None]:
    md_text = transcript_path.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_frontmatter(md_text)
    transcript_rel = transcript_path.relative_to(WIKI_PATH).as_posix()
    schema_dump = json.dumps(schema, ensure_ascii=False, indent=2)
    user = (
        f"Schéma cible (JSON Schema Draft 2020-12) :\n```json\n{schema_dump}\n```\n\n"
        f"Le champ `transcript_path` doit valoir EXACTEMENT : `{transcript_rel}`.\n\n"
        f"Transcript à débriefer :\n\n```markdown\n{body[:60_000]}\n```\n"
    )
    messages = [
        {"role": "system", "content": DEBRIEF_SYSTEM},
        {"role": "user", "content": user},
    ]
    validator = Draft202012Validator(schema)
    last_err: str | None = None
    for attempt in range(3):
        resp, err = openrouter_chat(
            messages,
            response_format={"type": "json_object"},
        )
        if err:
            last_err = err
            time.sleep(2 * (attempt + 1))
            continue
        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            last_err = f"bad-response: {str(resp)[:300]}"
            continue
        # Strip a stray ```json fence if the model adds one despite instructions.
        text = (content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            last_err = f"bad-json: {exc}"
            messages.append({"role": "assistant", "content": content or ""})
            messages.append({"role": "user", "content": f"Ta réponse n'était pas du JSON valide ({exc}). Réessaie, JSON pur, pas de fence."})
            continue
        # Force transcript_path even if model hallucinated a different value.
        data["transcript_path"] = transcript_rel
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if not errors:
            return data, None
        last_err = "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:5])
        messages.append({"role": "assistant", "content": content or ""})
        messages.append({"role": "user", "content": f"Ta réponse ne valide pas le schéma : {last_err}. Corrige et renvoie un JSON conforme."})
    return None, last_err or "unknown"


def run_debrief_py(debrief_path: pathlib.Path) -> tuple[int, dict]:
    if not DEBRIEF_PY.exists():
        return 1, {"status": "error", "reason": "debrief-py-missing", "detail": str(DEBRIEF_PY)}
    cmd = [
        "uv", "run",
        "--with", "requests",
        "--with", "jsonschema",
        "--with", "pyyaml",
        str(DEBRIEF_PY),
        "--debrief-path", str(debrief_path.relative_to(WIKI_PATH))
            if debrief_path.is_relative_to(WIKI_PATH) else str(debrief_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    last_json: dict | None = None
    for ln in (proc.stdout or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            last_json = json.loads(ln)
        except json.JSONDecodeError:
            continue
    if last_json is None:
        return proc.returncode or 1, {
            "status": "error", "reason": "debrief-py-bad-json",
            "detail": (proc.stdout + proc.stderr)[-500:],
        }
    return proc.returncode, last_json


# ----------------------------- self-cron removal -----------------------------

def _find_followup_cron_id() -> str | None:
    """Parse `hermes cron list` and return the job id of the
    `craig-watch-followup` cron, or None if absent. The CLI prints
    blocks like `<hex> [active]` followed by `  Name: <name>` lines —
    the trailing ` [active]` on the id line means we can't match the
    whole stripped line against the hex regex, so split on whitespace
    and check the first token only."""
    if not HERMES_CLI.exists():
        return None
    list_proc = subprocess.run(
        [str(HERMES_CLI), "cron", "list"],
        capture_output=True, text=True, timeout=15,
    )
    lines = list_proc.stdout.splitlines()
    for i, line in enumerate(lines):
        if FOLLOWUP_CRON_NAME in line and "Name" in line:
            for j in range(i - 1, max(-1, i - 5), -1):
                tokens = lines[j].strip().split()
                if tokens and re.fullmatch(r"[0-9a-fA-F-]{6,}", tokens[0]):
                    return tokens[0]
    return None


def maybe_self_delete_cron() -> dict:
    """Post the ✅ summary to #hermes-perso and remove the followup cron
    when there is nothing left to process. Order matters: cron removal
    runs FIRST, the ✅ is posted only after the cron is gone, so a
    parser failure or remove error doesn't spam the channel each tick
    (observed prod behaviour 2026-04-26 — the ✅ posted unconditionally
    and an unrelated parse bug kept the cron alive for ~12 ticks)."""
    if list_pending():
        return {"status": "skipped", "reason": "pending-not-empty"}

    if not HERMES_CLI.exists():
        return {"status": "skipped", "reason": f"hermes-cli-missing: {HERMES_CLI}"}
    job_id = _find_followup_cron_id()
    if not job_id:
        return {"status": "no-cron-found"}

    rm = subprocess.run(
        [str(HERMES_CLI), "cron", "remove", job_id],
        capture_output=True, text=True, timeout=15,
    )
    if rm.returncode != 0:
        return {"status": "remove-failed",
                "detail": (rm.stderr or rm.stdout)[:200], "job_id": job_id}

    msg = "✅ Suivi craig-watch terminé, tous les recordings ont été traités."
    msg_id, err = discord_post(DISCORD_HOME_CHANNEL, msg)
    notif = {"posted_message_id": msg_id, "post_error": err}
    return {**notif, "status": "removed", "job_id": job_id}


# ----------------------------- per-pending pipeline -----------------------------

def _reopen_if_archived(thread_id: str) -> bool:
    """GET the thread, unarchive if needed. Returns True iff the thread
    still exists and is reachable (False on 404 / deletion)."""
    thread_obj, err = _discord_request("GET", f"/channels/{thread_id}")
    if err or not thread_obj:
        return False
    if thread_obj.get("thread_metadata", {}).get("archived"):
        discord_unarchive_thread(thread_id)
    return True


def _ensure_status_thread(pending: dict, pending_path: pathlib.Path,
                          date_hhmm: str) -> tuple[str | None, str | None]:
    """Resolve the thread we'll post phase messages into. Order of
    preference:

    1. A previously-persisted thread_id from the pending JSON (idempotent
       resume after a crash mid-flow).
    2. An existing auto-thread Hermes already created on the listener's
       reply or on Craig's panel itself — reusing it keeps the status
       flow under the same conversation tree instead of spawning a
       parallel "Recording <date>" thread next to it (the user-flagged
       complaint on 2026-04-26).
    3. Create our own: post a header message in #craig-events as a
       reply to Craig's panel, then open a thread on that header.

    Returns (thread_id, err)."""
    craig_id = pending.get("craig_id") or ""
    panel_id = pending.get("message_id")

    thread_id = pending.get("pipeline_thread_id")
    if thread_id and _reopen_if_archived(thread_id):
        return thread_id, None
    if thread_id:
        # 404'd — fall through and re-resolve.
        pending.pop("pipeline_thread_id", None)
        pending.pop("pipeline_header_message_id", None)

    existing = find_existing_auto_thread(CRAIG_EVENTS_CHANNEL_ID, craig_id, panel_id)
    if existing:
        _reopen_if_archived(existing)
        pending["pipeline_thread_id"] = existing
        pending.pop("pipeline_header_message_id", None)
        save_pending(pending_path, pending)
        return existing, None

    header = (
        f"🎙️ Recording {date_hhmm}\n"
        f"Suivi du pipeline ↓ (réponses dans le thread ci-dessous)."
    )
    new_header_id, err = discord_post(
        CRAIG_EVENTS_CHANNEL_ID, header, reply_to=panel_id,
    )
    if err:
        return None, f"header-post: {err}"
    pending["pipeline_header_message_id"] = new_header_id

    new_thread_id, err = discord_create_thread(
        CRAIG_EVENTS_CHANNEL_ID, new_header_id, f"Recording {date_hhmm}",
    )
    if err:
        save_pending(pending_path, pending)
        return None, f"thread-create: {err}"

    pending["pipeline_thread_id"] = new_thread_id
    save_pending(pending_path, pending)
    return new_thread_id, None


def process_pending(pending_path: pathlib.Path) -> dict:
    pending = load_pending(pending_path)
    craig_id = pending.get("craig_id") or pending_path.stem
    channel_id = pending.get("channel_id") or CRAIG_EVENTS_CHANNEL_ID
    panel_id = pending.get("message_id")
    date_hhmm = parse_first_seen(pending)

    summary: dict = {"craig_id": craig_id, "phase": "start"}

    if panel_id:
        panel, err = discord_get_message(channel_id, panel_id)
        if err:
            summary.update({"phase": "panel-refetch-failed", "detail": err})
            return summary
        body = extract_message_text(panel or {})
        if not ENDED_RE.search(body):
            summary.update({"phase": "still-recording"})
            return summary
    # Manual pendings without a panel_id (debug runs) skip the refetch
    # and assume the recording is already over.

    thread_id, err = _ensure_status_thread(pending, pending_path, date_hhmm)
    if err or not thread_id:
        summary.update({"phase": "status-init-failed", "detail": err})
        return summary

    def post_phase(kind: str, text: str) -> None:
        _, perr = discord_post(thread_id, fmt_phase(kind, text))
        if perr:
            # Don't fail the pipeline on a status post error — log and move on.
            summary.setdefault("status_post_errors", []).append(perr)

    post_phase("inprogress", "Téléchargement Drive…")

    # Phase 2 — scan.py with live progress messages in the thread.
    posted_phases: set[str] = set()

    def on_progress(ev: dict) -> None:
        phase = ev.get("phase")
        if phase in posted_phases:
            return
        posted_phases.add(phase or "")
        if phase == "zip-found":
            mb = (ev.get("size_bytes") or 0) / 1e6
            post_phase("done", f"Zip Drive trouvé ({mb:.1f} MB).")
            post_phase("inprogress", "Transcription Groq Whisper…")
        elif phase == "groq-start":
            # zip-found already announced groq; only post if zip-found
            # wasn't seen (unusual).
            if "zip-found" not in posted_phases:
                post_phase("inprogress", "Transcription Groq Whisper…")
        elif phase == "writing-raw":
            post_phase("done", "Transcription terminée, écriture du fichier…")

    rc, scan_result, _ = run_scan(craig_id, on_progress)

    if scan_result.get("status") == "error":
        post_phase("error", f"Scan: {scan_result.get('reason')} — "
                            f"{scan_result.get('detail','')[:200]}")
        summary.update({"phase": "scan-error", "scan": scan_result})
        return summary

    if scan_result.get("status") == "skipped" and scan_result.get("reason") == "already-transcribed":
        as_basename = scan_result.get("as", "")
        transcript_rel = f"raw/transcripts/{as_basename}" if as_basename else None
        post_phase("done", f"Transcript déjà présent (`{as_basename}`), reprise du pipeline.")
    else:
        transcript_rel = scan_result.get("path")

    if not transcript_rel:
        post_phase("error", f"Scan: pas de path retourné — {scan_result.get('reason','')}")
        summary.update({"phase": "scan-no-path", "scan": scan_result})
        return summary

    transcript_abs = WIKI_PATH / transcript_rel
    duration_s = int(scan_result.get("duration_s") or 0)
    if not duration_s:
        try:
            fm, _ = parse_frontmatter(transcript_abs.read_text(encoding="utf-8", errors="replace"))
            duration_s = int(fm.get("duration_s") or 0)
        except Exception:
            duration_s = 0

    if scan_result.get("status") != "skipped":
        post_phase("done", f"Transcrit : `{transcript_rel}` "
                           f"({duration_s//60}m {duration_s%60}s).")

    # Phase 3 — git push transcript.
    post_phase("inprogress", "Commit + push GitHub (transcript)…")
    sha, push_err = git_commit_push(
        WIKI_PATH, [transcript_rel],
        f"raw: capture transcript {transcript_abs.stem}",
    )
    if push_err:
        post_phase("error", f"Push transcript: {push_err[:200]}")
        summary.update({"phase": "push-error", "detail": push_err})
        return summary
    if sha:
        link = github_link(WIKI_PATH, sha)
        push_text = f"Pushé : [`{sha}`]({link})." if link.startswith("http") else f"Pushé (`{sha}`)."
    else:
        push_text = "Push transcript : rien à pusher (déjà sur origin)."
    post_phase("done", push_text)

    # Phase 4 — llm-wiki ingest.
    post_phase("inprogress", "Ingest llm-wiki (agent loop bufferisé)…")
    ingest = llm_wiki_ingest(transcript_abs)
    summary["ingest"] = ingest
    if ingest["status"] == "committed":
        post_phase("done",
                   f"Wiki ingéré : {ingest['n_files']} fichier(s) "
                   f"({ingest['n_created']} créé(s), {ingest['n_updated']} maj), "
                   f"commit `{ingest.get('sha','?')}`.")
    elif ingest["status"] == "no-op":
        post_phase("done", "Wiki ingest : aucune édition proposée par le LLM.")
    elif ingest["status"] in ("aborted", "llm-error"):
        post_phase("error",
                   f"Ingest aborté ({ingest.get('reason','?')[:200]}). "
                   f"Transcript reste committé, debrief continue.")

    # Always proceed to debrief even on ingest abort.
    if duration_s and duration_s < MIN_DURATION_S:
        post_phase("done", f"Recording court ({duration_s}s < {MIN_DURATION_S}s) — "
                           f"pas de debrief généré.")
        delete_pending(pending_path)
        summary.update({"phase": "done-short"})
        return summary

    post_phase("inprogress", "Génération debrief (OpenRouter)…")

    DEBRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    debrief_path = DEBRIEFS_DIR / f"{transcript_abs.stem}.json"
    try:
        schema = json.loads(DEBRIEF_SCHEMA.read_text(encoding="utf-8"))
    except Exception as exc:
        post_phase("error", f"Schema debrief introuvable: {exc}")
        summary.update({"phase": "schema-load-error", "detail": str(exc)})
        return summary

    if debrief_path.exists():
        try:
            debrief_data = json.loads(debrief_path.read_text(encoding="utf-8"))
            errors = list(Draft202012Validator(schema).iter_errors(debrief_data))
            if errors:
                debrief_path.unlink()
        except Exception:
            try:
                debrief_path.unlink()
            except OSError:
                pass

    if not debrief_path.exists():
        debrief_data, gen_err = generate_debrief(transcript_abs, schema)
        if not debrief_data:
            post_phase("error", f"Génération debrief LLM échouée : "
                                f"{(gen_err or 'unknown')[:200]}")
            summary.update({"phase": "debrief-generation-failed", "detail": gen_err})
            return summary
        debrief_path.write_text(
            json.dumps(debrief_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # Phase 6 — debrief.py (POST recap + thread in #hermes-perso).
    rc, deb_result = run_debrief_py(debrief_path)
    summary["debrief_py"] = deb_result
    if deb_result.get("status") == "posted":
        deb_thread_id = deb_result.get("thread_id")
        post_phase("done",
                   f"Debrief posté dans `#hermes-perso`"
                   + (f" (thread `{deb_thread_id}`)." if deb_thread_id else "."))
    elif deb_result.get("status") == "skipped":
        post_phase("done", f"Debrief skipped : {deb_result.get('reason')}.")
    else:
        post_phase("error", f"Debrief.py : {deb_result.get('reason','?')} — "
                            f"{deb_result.get('detail','')[:200]}")
        summary.update({"phase": "debrief-py-error"})
        return summary

    post_phase("done", "Pipeline terminé.")
    delete_pending(pending_path)
    summary.update({"phase": "done"})
    return summary


# ----------------------------- main -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--craig-id", help="Process only this craig_id (debug).")
    args = ap.parse_args()

    pendings = list_pending()
    if args.craig_id:
        pendings = [p for p in pendings if p.stem == args.craig_id]

    out_summaries: list[dict] = []
    for p in pendings:
        try:
            out_summaries.append(process_pending(p))
        except Exception as exc:
            out_summaries.append({
                "craig_id": p.stem,
                "phase": "exception",
                "detail": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-1000:],
            })

    cron_action = maybe_self_delete_cron() if not args.craig_id else {"status": "skipped", "reason": "debug-mode"}

    print(json.dumps({
        "status": "ok",
        "processed_count": len(out_summaries),
        "summaries": out_summaries,
        "cron_action": cron_action,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({
            "status": "error", "reason": "unexpected",
            "detail": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-1000:],
        }, ensure_ascii=False))
        sys.exit(1)
