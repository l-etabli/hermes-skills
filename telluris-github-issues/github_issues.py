#!/usr/bin/env python3
"""Scoped GitHub Issues CLI for the Telluris product repository."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request


ALLOWED_REPOSITORY = "telluris-ai/telluris"
DEFAULT_ENV_FILE = "/opt/data/.env"
DEFAULT_API_URL = "https://api.github.com"
USER_AGENT = "telluris-github-issues/0.1"
MAX_BODY_BYTES = 100_000


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def fail(reason: str, detail: str, *, exit_code: int = 1) -> int:
    emit({"status": "error", "reason": reason, "detail": detail})
    return exit_code


def read_token() -> str | None:
    env_file = pathlib.Path(os.environ.get("HERMES_ENV_FILE", DEFAULT_ENV_FILE))
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if line.startswith("GITHUB_TOKEN="):
            return line.split("=", 1)[1].strip() or None
    return os.environ.get("GITHUB_TOKEN") or None


def parse_labels(raw: str) -> list[str]:
    return [label.strip() for label in raw.split(",") if label.strip()]


def issue_summary(issue: dict) -> dict:
    return {
        "number": issue["number"],
        "title": issue["title"],
        "url": issue["html_url"],
        "state": issue["state"],
    }


def issue_details(issue: dict) -> dict:
    return {
        **issue_summary(issue),
        "body": issue.get("body") or "",
        "labels": [
            label.get("name", "") if isinstance(label, dict) else str(label)
            for label in issue.get("labels", [])
        ],
    }


def read_text_file(path: str, *, token: str) -> str:
    resolved = pathlib.Path(path).resolve(strict=True)
    explicit_test = (
        os.environ.get("HERMES_GITHUB_TEST_MODE") == "1"
        and token.startswith("test_")
    )
    if not explicit_test and not resolved.is_relative_to(pathlib.Path("/tmp")):
        raise ValueError("body files must resolve under /tmp")
    if resolved.stat().st_size > MAX_BODY_BYTES:
        raise ValueError(f"body file exceeds {MAX_BODY_BYTES} bytes")
    return resolved.read_text(encoding="utf-8")


def resolve_api_url(token: str) -> str:
    override = os.environ.get("GITHUB_API_URL")
    if not override:
        return DEFAULT_API_URL
    parsed = urllib.parse.urlparse(override)
    is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    is_explicit_test = (
        os.environ.get("HERMES_GITHUB_TEST_MODE") == "1"
        and token.startswith("test_")
        and is_loopback
        and parsed.scheme == "http"
    )
    if not is_explicit_test:
        raise ValueError("GITHUB_API_URL is allowed only for loopback test tokens")
    return override.rstrip("/")


def request_json(
    method: str,
    path: str,
    token: str,
    api_url: str,
    payload: dict | None = None,
):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{api_url}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=ALLOWED_REPOSITORY)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create an issue")
    create.add_argument("--title", required=True)
    create.add_argument("--body-file", required=True)
    create.add_argument("--labels", default="")
    search = commands.add_parser("search", help="Search issues")
    search.add_argument("--query", required=True)
    view = commands.add_parser("view", help="View one issue")
    view.add_argument("number", type=int)
    update = commands.add_parser("update", help="Update an issue")
    update.add_argument("number", type=int)
    update.add_argument("--title")
    update.add_argument("--body-file")
    update.add_argument("--labels")
    comment = commands.add_parser("comment", help="Comment on an issue")
    comment.add_argument("number", type=int)
    comment.add_argument("--body-file", required=True)
    close = commands.add_parser("close", help="Close an issue as completed")
    close.add_argument("number", type=int)
    reopen = commands.add_parser("reopen", help="Reopen an issue")
    reopen.add_argument("number", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repo != ALLOWED_REPOSITORY:
        return fail(
            "repository-not-allowed",
            f"only {ALLOWED_REPOSITORY} is allowed",
            exit_code=2,
        )
    token = read_token()
    if not token:
        return fail(
            "missing-token",
            f"GITHUB_TOKEN absent from {os.environ.get('HERMES_ENV_FILE', DEFAULT_ENV_FILE)}",
            exit_code=2,
        )
    try:
        api_url = resolve_api_url(token)
    except ValueError as error:
        return fail("unsafe-api-override", str(error), exit_code=2)

    if args.command == "create":
        try:
            body = read_text_file(args.body_file, token=token)
        except (OSError, ValueError) as error:
            return fail("body-file-unreadable", str(error), exit_code=2)
        try:
            issue = request_json(
                "POST",
                f"/repos/{ALLOWED_REPOSITORY}/issues",
                token,
                api_url,
                {
                    "title": args.title,
                    "body": body,
                    "labels": parse_labels(args.labels),
                },
            )
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            return fail("github-http-error", f"HTTP {error.code}: {detail}")
        except (OSError, ValueError) as error:
            return fail("github-request-failed", f"{type(error).__name__}: {error}")
        emit({"status": "created", "issue": issue_summary(issue)})
        return 0

    if args.command == "search":
        query = urllib.parse.urlencode(
            {"q": f"{args.query} repo:{ALLOWED_REPOSITORY} is:issue"}
        )
        try:
            result = request_json("GET", f"/search/issues?{query}", token, api_url)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            return fail("github-http-error", f"HTTP {error.code}: {detail}")
        except (OSError, ValueError) as error:
            return fail("github-request-failed", f"{type(error).__name__}: {error}")
        emit(
            {
                "status": "ok",
                "total_count": result.get("total_count", 0),
                "issues": [issue_summary(issue) for issue in result.get("items", [])],
            }
        )
        return 0

    issue_path = f"/repos/{ALLOWED_REPOSITORY}/issues/{args.number}"
    try:
        if args.command == "view":
            issue = request_json("GET", issue_path, token, api_url)
            emit({"status": "ok", "issue": issue_details(issue)})
            return 0

        if args.command == "update":
            payload = {}
            if args.title is not None:
                payload["title"] = args.title
            if args.body_file is not None:
                payload["body"] = read_text_file(args.body_file, token=token)
            if args.labels is not None:
                payload["labels"] = parse_labels(args.labels)
            if not payload:
                return fail(
                    "no-update",
                    "provide --title, --body-file, or --labels",
                    exit_code=2,
                )
            issue = request_json("PATCH", issue_path, token, api_url, payload)
            emit({"status": "updated", "issue": issue_details(issue)})
            return 0

        if args.command == "comment":
            comment = request_json(
                "POST",
                f"{issue_path}/comments",
                token,
                api_url,
                {"body": read_text_file(args.body_file, token=token)},
            )
            emit(
                {
                    "status": "commented",
                    "comment": {
                        "id": comment["id"],
                        "url": comment["html_url"],
                    },
                }
            )
            return 0

        if args.command == "close":
            issue = request_json(
                "PATCH",
                issue_path,
                token,
                api_url,
                {"state": "closed", "state_reason": "completed"},
            )
            emit({"status": "closed", "issue": issue_summary(issue)})
            return 0

        if args.command == "reopen":
            issue = request_json(
                "PATCH",
                issue_path,
                token,
                api_url,
                {"state": "open"},
            )
            emit({"status": "reopened", "issue": issue_summary(issue)})
            return 0
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        return fail("github-http-error", f"HTTP {error.code}: {detail}")
    except OSError as error:
        return fail("file-or-network-error", f"{type(error).__name__}: {error}")
    except (KeyError, TypeError, ValueError) as error:
        return fail("github-response-invalid", f"{type(error).__name__}: {error}")

    return fail("unsupported-command", args.command, exit_code=2)


if __name__ == "__main__":
    raise SystemExit(main())
