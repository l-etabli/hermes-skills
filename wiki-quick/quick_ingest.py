#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""
wiki-quick: drop conversationnel Discord -> raw/notes/<slug>.md + commit/push.

Pas d'appel LLM (le LLM ingest a lieu plus tard via `llm-wiki ingest`).
Pose juste un fichier avec frontmatter, pull --rebase, commit, push.

Usage:
    cat content.md | uv run --with pyyaml quick_ingest.py \\
        --channel "#perso-home" --participants "jerome,manu" \\
        --title "stack typescript piloti"

Le contenu passe sur stdin.

Output: JSON sur stdout.
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

import yaml

WIKI_PATH = pathlib.Path(os.environ.get("WIKI_PATH", ""))
NOTES_DIR = WIKI_PATH / "raw" / "notes"
GITHUB_ENV_FILE = pathlib.Path(os.environ.get("HERMES_ENV_FILE", "/opt/data/.env"))
GIT_AUTHOR_NAME = os.environ.get("HERMES_GIT_AUTHOR_NAME", "Hermes wiki-quick")
GIT_AUTHOR_EMAIL = os.environ.get("HERMES_GIT_AUTHOR_EMAIL", "hermes-wiki-quick@l-etabli.local")

SLUG_STOPWORDS = {"le", "la", "les", "un", "une", "des", "de", "du", "et", "ou",
                  "à", "a", "au", "aux", "the", "of", "and", "to", "in", "on", "for"}


def fail(reason: str, **extra) -> None:
    print(json.dumps({"status": "error", "reason": reason, **extra}, ensure_ascii=False))
    sys.exit(1)


def slugify(text: str, max_words: int = 4) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", " ", text, flags=re.UNICODE)
    words = [w for w in text.split() if w and w not in SLUG_STOPWORDS]
    if not words:
        words = ["note"]
    return "-".join(words[:max_words])[:60].strip("-") or "note"


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
    if not m:
        return None
    return f"https://x-access-token:{token}@github.com/{m.group(1)}.git"


def github_commit_url(sha: str) -> str:
    try:
        remote = git("remote", "get-url", "origin").stdout.strip()
    except subprocess.CalledProcessError:
        return sha
    m = re.match(r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/.]+)(\.git)?$", remote)
    return f"https://github.com/{m.group(1)}/commit/{sha}" if m else sha


def main() -> None:
    parser = argparse.ArgumentParser(description="Drop conversationnel -> raw/notes/")
    parser.add_argument("--channel", required=True, help="Discord channel name or id")
    parser.add_argument("--participants", default="", help="CSV of participant handles")
    parser.add_argument("--title", default="", help="Optional short title (defaults to first content line)")
    parser.add_argument("--source-url", default="", help="Optional Discord message URL")
    args = parser.parse_args()

    if not WIKI_PATH or not WIKI_PATH.is_dir():
        fail("missing-wiki-path", detail=f"WIKI_PATH={WIKI_PATH!s} not a directory")

    content = sys.stdin.read().strip()
    if not content:
        fail("empty-content", detail="stdin was empty — pipe the content to the script")

    title = args.title.strip() or content.splitlines()[0].strip().lstrip("#").strip()[:80]
    if not title:
        title = "note"

    NOTES_DIR.mkdir(parents=True, exist_ok=True)

    # Pull first so the slug uniqueness check sees concurrent writes.
    pull = git("pull", "--rebase", "origin", "HEAD", check=False)
    if pull.returncode != 0:
        fail("git-pull-failed", detail=(pull.stderr or pull.stdout)[:200])

    now = datetime.now(timezone.utc)
    base_slug = f"{now.strftime('%Y-%m-%d-%H%M')}-{slugify(title)}"
    path = NOTES_DIR / f"{base_slug}.md"
    suffix = 2
    while path.exists():
        path = NOTES_DIR / f"{base_slug}-{suffix}.md"
        suffix += 1

    participants = [p.strip() for p in args.participants.split(",") if p.strip()]
    fm = {
        "title": title,
        "source": "discord",
        "captured_by": "wiki-quick",
        "channel": args.channel,
        "participants": participants,
        "created": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if args.source_url:
        fm["source_url"] = args.source_url

    body = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + content + "\n"
    path.write_text(body, encoding="utf-8")

    rel = path.relative_to(WIKI_PATH).as_posix()
    msg = f"raw/notes: capture {path.stem}"

    git("add", "--", rel)
    diff = git("diff", "--cached", "--quiet", check=False)
    if diff.returncode == 0:
        # Nothing actually staged (extremely unlikely after we just wrote).
        print(json.dumps({"status": "noop", "path": rel}, ensure_ascii=False))
        return
    git("commit", "-m", msg)

    token = read_github_token()
    if not token:
        fail("push-no-token", detail="GITHUB_TOKEN absent from /opt/data/.env and env")
    push_url = push_url_with_token(token)
    if not push_url:
        fail("push-bad-remote", detail="origin is not a github.com URL")

    push = subprocess.run(
        ["git", "-C", str(WIKI_PATH), "push", push_url, "HEAD"],
        capture_output=True, text=True,
    )
    if push.returncode != 0:
        scrubbed = (push.stderr or push.stdout).replace(token, "***")[:200]
        fail("push-failed", detail=scrubbed)

    sha = git("rev-parse", "HEAD").stdout.strip()[:8]
    print(json.dumps({
        "status": "ok",
        "path": rel,
        "abs_path": str(path),
        "sha": sha,
        "url": github_commit_url(sha),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
