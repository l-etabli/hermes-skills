#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "pyyaml"]
# ///
"""
weekly-digest: récap vendredi 17h ou briefing lundi 9h, posté dans
$DISCORD_HOME_CHANNEL + appendé au log.md du KB.

Mode passé par le cron (--recap | --briefing). UN SEUL appel LLM
(génération du digest via OpenRouter). Tout le reste est code.

Usage:
    uv run --with requests --with pyyaml digest.py --recap
    uv run --with requests --with pyyaml digest.py --briefing
    uv run --with requests --with pyyaml digest.py --recap --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

REQUIRED_ENV = (
    "WIKI_PATH",
    "DISCORD_BOT_TOKEN",
    "DISCORD_HOME_CHANNEL",
    "OPENROUTER_API_KEY",
)
_missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if _missing:
    print(json.dumps({"status": "error", "reason": "missing-env", "missing": _missing}))
    sys.exit(2)

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
LOG_FILE = WIKI_PATH / "log.md"
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_HOME_CHANNEL = os.environ["DISCORD_HOME_CHANNEL"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-3-flash-preview")
GITHUB_ENV_FILE = pathlib.Path(os.environ.get("HERMES_ENV_FILE", "/opt/data/.env"))
GIT_AUTHOR_NAME = os.environ.get("HERMES_GIT_AUTHOR_NAME", "Hermes weekly-digest")
GIT_AUTHOR_EMAIL = os.environ.get("HERMES_GIT_AUTHOR_EMAIL", "hermes-weekly-digest@l-etabli.local")
INSTANCE_NAME = os.environ.get("HERMES_INSTANCE", pathlib.Path(WIKI_PATH).name)

DISCORD_API = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://github.com/l-etabli/hermes-skills, 1.0)"
DISCORD_EPOCH_MS = 1420070400000  # 2015-01-01 UTC, used in snowflake -> timestamp
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"


def fail(reason: str, **extra) -> None:
    print(json.dumps({"status": "error", "reason": reason, **extra}, ensure_ascii=False))
    sys.exit(1)


# ----------------------------- discord -----------------------------

def discord_request(method: str, path: str, **kwargs) -> tuple[object, str | None]:
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "User-Agent": DISCORD_USER_AGENT,
    }
    if "json" in kwargs:
        headers["Content-Type"] = "application/json"
    headers.update(kwargs.pop("headers", None) or {})
    url = f"{DISCORD_API}{path}"
    for attempt in range(5):
        try:
            r = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        except requests.RequestException as exc:
            return None, f"network: {type(exc).__name__}: {exc}"
        if r.status_code == 429:
            retry = float(r.headers.get("Retry-After") or "1")
            time.sleep(min(retry, 5))
            continue
        if not r.ok:
            return None, f"discord-{r.status_code}: {r.text[:200]}"
        if r.status_code == 204 or not r.text:
            return {}, None
        try:
            return r.json(), None
        except ValueError:
            return None, f"discord-bad-json: {r.text[:200]}"
    return None, "discord-rate-limit-exhausted"


def list_visible_text_channels() -> list[dict]:
    """Tous les text channels visibles au bot (toutes guildes confondues).
    Discord filtre déjà selon les perms du rôle bot — pas besoin de re-filtrer."""
    guilds, err = discord_request("GET", "/users/@me/guilds")
    if err:
        fail("discord-guilds", detail=err)
    out: list[dict] = []
    for g in guilds or []:
        chans, err = discord_request("GET", f"/guilds/{g['id']}/channels")
        if err:
            continue  # guild-level error: skip rather than abort
        for c in chans or []:
            # type 0 = GUILD_TEXT, 5 = GUILD_ANNOUNCEMENT
            if c.get("type") in (0, 5):
                out.append({
                    "id": c["id"],
                    "name": c.get("name", c["id"]),
                    "guild": g.get("name", g["id"]),
                })
    return out


def fetch_channel_messages(channel_id: str, since: datetime, hard_cap: int = 200) -> list[dict]:
    """Pagine `before` en remontant jusqu'à `since`. Retourne les messages
    triés du plus ancien au plus récent."""
    out: list[dict] = []
    before: str | None = None
    since_ms = int(since.timestamp() * 1000)
    while len(out) < hard_cap:
        params = {"limit": 100}
        if before:
            params["before"] = before
        msgs, err = discord_request("GET", f"/channels/{channel_id}/messages", params=params)
        if err or not msgs:
            break
        stop = False
        for m in msgs:
            ts_ms = (int(m["id"]) >> 22) + DISCORD_EPOCH_MS
            if ts_ms < since_ms:
                stop = True
                break
            if m.get("type") not in (0, 19):  # 0 default, 19 reply
                continue
            out.append({
                "ts": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(),
                "author": (m.get("author") or {}).get("username", "?"),
                "content": (m.get("content") or "").strip(),
            })
        if stop or len(msgs) < 100:
            break
        before = msgs[-1]["id"]
    out.reverse()
    return out


# ----------------------------- openrouter -----------------------------

PROMPTS = {
    "recap": (
        "Tu es l'assistant <instance>. Voici les messages des {days} derniers jours sur les "
        "channels Discord où tu es présent. Produis un RÉCAP COURT vendredi soir, "
        "**3 bullets max** : ce qui a été DÉCIDÉ, ce qui a été LIVRÉ, ce qui est À NOTER. "
        "Ton sec, factuel, pas de remplissage. Cite des @auteurs si pertinent. "
        "Si rien de notable cette semaine, dis-le en 1 phrase."
    ),
    "briefing": (
        "Tu es l'assistant <instance>. Voici les messages des {days} derniers jours sur les "
        "channels Discord où tu es présent. Produis un BRIEFING lundi matin : "
        "**OBJECTIFS** de la semaine, **ACTIONS REPRISES** du vendredi, **BLOCAGES** ouverts, "
        "**ÉCHÉANCES** proches. Prospectif et actionnable, pas un copier-coller du récap. "
        "Si trop peu de matière, propose 2-3 questions ouvertes pour cadrer la semaine."
    ),
}


def call_openrouter(mode: str, days: int, transcript: str) -> tuple[str | None, str | None]:
    system = PROMPTS[mode].format(days=days).replace("<instance>", INSTANCE_NAME)
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": transcript[:60000]},
        ],
    }
    try:
        r = requests.post(
            OPENROUTER_API,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/l-etabli/hermes-skills",
                "X-Title": "weekly-digest",
            },
            json=body,
            timeout=120,
        )
    except requests.RequestException as exc:
        return None, f"network: {type(exc).__name__}: {exc}"
    if not r.ok:
        return None, f"openrouter-{r.status_code}: {r.text[:300]}"
    try:
        choices = r.json().get("choices") or []
        return (choices[0]["message"]["content"] or "").strip(), None
    except (ValueError, KeyError, IndexError):
        return None, f"openrouter-bad-shape: {r.text[:200]}"


# ----------------------------- git -----------------------------

def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(WIKI_PATH),
         "-c", f"user.name={GIT_AUTHOR_NAME}",
         "-c", f"user.email={GIT_AUTHOR_EMAIL}",
         *args],
        capture_output=True, text=True, check=check,
    )


def read_github_token() -> str | None:
    try:
        text = GITHUB_ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return os.environ.get("GITHUB_TOKEN")
    for line in text.splitlines():
        if line.startswith("GITHUB_TOKEN="):
            return line.split("=", 1)[1].strip() or None
    return os.environ.get("GITHUB_TOKEN")


def push_url_with_token(token: str) -> str | None:
    try:
        remote = git("remote", "get-url", "origin").stdout.strip()
    except subprocess.CalledProcessError:
        return None
    m = re.match(r"^https://github\.com/([^/]+/[^/]+?)(\.git)?$", remote)
    return f"https://x-access-token:{token}@github.com/{m.group(1)}.git" if m else None


def append_log_and_push(mode: str, digest: str) -> tuple[str | None, str | None]:
    pull = git("pull", "--rebase", "origin", "HEAD", check=False)
    if pull.returncode != 0:
        return None, f"git-pull: {(pull.stderr or pull.stdout)[:200]}"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"\n## {today} — weekly-{mode}\n\n{digest.strip()}\n"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(entry)

    git("add", "--", "log.md")
    diff = git("diff", "--cached", "--quiet", check=False)
    if diff.returncode == 0:
        return None, None  # nothing staged
    git("commit", "-m", f"log: weekly-{mode} {today}")

    token = read_github_token()
    if not token:
        return None, "push-no-token"
    push_url = push_url_with_token(token)
    if not push_url:
        return None, "push-bad-remote"
    push = subprocess.run(
        ["git", "-C", str(WIKI_PATH), "push", push_url, "HEAD"],
        capture_output=True, text=True,
    )
    if push.returncode != 0:
        scrubbed = (push.stderr or push.stdout).replace(token, "***")[:200]
        return None, f"push-failed: {scrubbed}"
    return git("rev-parse", "HEAD").stdout.strip()[:8], None


# ----------------------------- main -----------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--recap", action="store_true")
    mode_group.add_argument("--briefing", action="store_true")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mode = "recap" if args.recap else "briefing"
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    channels = list_visible_text_channels()
    transcript_parts: list[str] = []
    total_msgs = 0
    for c in channels:
        msgs = fetch_channel_messages(c["id"], since)
        if not msgs:
            continue
        total_msgs += len(msgs)
        header = f"\n### #{c['name']} ({c['guild']})\n"
        body = "\n".join(f"[{m['ts']}] {m['author']}: {m['content']}" for m in msgs if m['content'])
        if body:
            transcript_parts.append(header + body)

    if not transcript_parts:
        transcript = "(aucun message dans la fenêtre)"
    else:
        transcript = "\n".join(transcript_parts)

    digest, err = call_openrouter(mode, args.days, transcript)
    if err:
        fail("openrouter", detail=err)

    if args.dry_run:
        print(json.dumps({
            "status": "dry-run",
            "mode": mode,
            "channels_scanned": len(channels),
            "messages_collected": total_msgs,
            "digest": digest,
        }, ensure_ascii=False))
        return

    posted, err = discord_request(
        "POST",
        f"/channels/{DISCORD_HOME_CHANNEL}/messages",
        json={"content": (digest or "(digest vide)")[:2000]},
    )
    if err:
        fail("discord-post", detail=err)
    discord_message_id = (posted or {}).get("id")

    log_sha, log_err = append_log_and_push(mode, digest or "")
    if log_err:
        # Posté Discord OK mais log raté — on remonte l'erreur sans crash hard.
        print(json.dumps({
            "status": "partial",
            "mode": mode,
            "channels_scanned": len(channels),
            "messages_collected": total_msgs,
            "discord_message_id": discord_message_id,
            "log_error": log_err,
        }, ensure_ascii=False))
        return

    print(json.dumps({
        "status": "ok",
        "mode": mode,
        "channels_scanned": len(channels),
        "messages_collected": total_msgs,
        "discord_message_id": discord_message_id,
        "log_sha": log_sha,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
