from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SCRIPT = Path(__file__).with_name("github_issues.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("github_issues_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _GitHubStub(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        self.__class__.requests.append(
            {
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "authorization": self.headers.get("Authorization"),
            }
        )
        issue = {
            "number": 12,
            "title": "Sidebar déjà signalée",
            "body": "Détails existants",
            "html_url": "https://github.test/telluris-ai/telluris/issues/12",
            "state": "open",
            "labels": [{"name": "bug"}],
        }
        payload = json.dumps(
            {"total_count": 1, "items": [issue]}
            if parsed.path == "/search/issues"
            else issue
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.__class__.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        if self.path.endswith("/comments"):
            response = {
                "id": 99,
                "html_url": "https://github.test/telluris-ai/telluris/issues/12#issuecomment-99",
                "body": body["body"],
            }
        else:
            response = {
                "number": 42,
                "title": body["title"],
                "html_url": "https://github.test/telluris-ai/telluris/issues/42",
                "state": "open",
            }
        payload = json.dumps(response).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_PATCH(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.__class__.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        payload = json.dumps(
            {
                "number": 12,
                "title": body.get("title", "Sidebar déjà signalée"),
                "body": body.get("body", "Détails existants"),
                "html_url": "https://github.test/telluris-ai/telluris/issues/12",
                "state": body.get("state", "open"),
                "labels": [{"name": label} for label in body.get("labels", ["bug"])],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args) -> None:
        return


def test_create_issue_uses_refreshed_app_token_without_exposing_it(tmp_path) -> None:
    _GitHubStub.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHubStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text("GITHUB_TOKEN=test_fresh-token\n")
        body_file = tmp_path / "issue.md"
        body_file.write_text("## Description\nLa sidebar reste ouverte.\n")
        env = {
            **os.environ,
            "HERMES_ENV_FILE": str(env_file),
            "GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}",
            "HERMES_GITHUB_TEST_MODE": "1",
        }
        env.pop("GITHUB_TOKEN", None)

        result = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "create",
                "--title",
                "Fermer la sidebar",
                "--body-file",
                str(body_file),
                "--labels",
                "bug,product",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "status": "created",
        "issue": {
            "number": 42,
            "title": "Fermer la sidebar",
            "url": "https://github.test/telluris-ai/telluris/issues/42",
            "state": "open",
        },
    }
    assert _GitHubStub.requests == [
        {
            "path": "/repos/telluris-ai/telluris/issues",
            "authorization": "Bearer test_fresh-token",
            "body": {
                "title": "Fermer la sidebar",
                "body": "## Description\nLa sidebar reste ouverte.\n",
                "labels": ["bug", "product"],
            },
        }
    ]
    assert "test_fresh-token" not in result.stdout
    assert "test_fresh-token" not in result.stderr


def test_search_issues_scopes_query_to_telluris_repository(tmp_path) -> None:
    _GitHubStub.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHubStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text("GITHUB_TOKEN=test_search-token\n")
        result = subprocess.run(
            [sys.executable, SCRIPT, "search", "--query", "sidebar admin"],
            env={
                **os.environ,
                "HERMES_ENV_FILE": str(env_file),
                "GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}",
                "HERMES_GITHUB_TEST_MODE": "1",
            },
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "status": "ok",
        "total_count": 1,
        "issues": [
            {
                "number": 12,
                "title": "Sidebar déjà signalée",
                "url": "https://github.test/telluris-ai/telluris/issues/12",
                "state": "open",
            }
        ],
    }
    assert _GitHubStub.requests[0]["path"] == "/search/issues"
    assert _GitHubStub.requests[0]["query"] == {
        "q": ["sidebar admin repo:telluris-ai/telluris is:issue"]
    }


def test_manage_existing_issue_lifecycle(tmp_path) -> None:
    _GitHubStub.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHubStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text("GITHUB_TOKEN=test_manage-token\n")
        body_file = tmp_path / "body.md"
        body_file.write_text("Nouveaux détails")
        comment_file = tmp_path / "comment.md"
        comment_file.write_text("Pris en compte dans le débrief.")
        env = {
            **os.environ,
            "HERMES_ENV_FILE": str(env_file),
            "GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}",
            "HERMES_GITHUB_TEST_MODE": "1",
        }
        commands = [
            ["view", "12"],
            [
                "update",
                "12",
                "--title",
                "Sidebar corrigée",
                "--body-file",
                str(body_file),
                "--labels",
                "bug,validated",
            ],
            ["comment", "12", "--body-file", str(comment_file)],
            ["close", "12"],
            ["reopen", "12"],
        ]
        results = [
            subprocess.run(
                [sys.executable, SCRIPT, *command],
                env=env,
                capture_output=True,
                text=True,
            )
            for command in commands
        ]
    finally:
        server.shutdown()
        thread.join()

    assert [result.returncode for result in results] == [0, 0, 0, 0, 0], [
        result.stderr for result in results
    ]
    assert [json.loads(result.stdout)["status"] for result in results] == [
        "ok",
        "updated",
        "commented",
        "closed",
        "reopened",
    ]
    assert [request["path"] for request in _GitHubStub.requests] == [
        "/repos/telluris-ai/telluris/issues/12",
        "/repos/telluris-ai/telluris/issues/12",
        "/repos/telluris-ai/telluris/issues/12/comments",
        "/repos/telluris-ai/telluris/issues/12",
        "/repos/telluris-ai/telluris/issues/12",
    ]
    assert _GitHubStub.requests[1]["body"] == {
        "title": "Sidebar corrigée",
        "body": "Nouveaux détails",
        "labels": ["bug", "validated"],
    }
    assert _GitHubStub.requests[3]["body"] == {
        "state": "closed",
        "state_reason": "completed",
    }
    assert _GitHubStub.requests[4]["body"] == {"state": "open"}


def test_real_installation_token_cannot_be_redirected_to_an_override_api(
    tmp_path,
) -> None:
    _GitHubStub.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHubStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text("GITHUB_TOKEN=ghs_real-installation-token\n")
        body_file = tmp_path / "issue.md"
        body_file.write_text("Ne doit pas être envoyé.")
        result = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "create",
                "--title",
                "Tentative de redirection",
                "--body-file",
                str(body_file),
            ],
            env={
                **os.environ,
                "HERMES_ENV_FILE": str(env_file),
                "GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}",
                "HERMES_GITHUB_TEST_MODE": "1",
            },
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "unsafe-api-override"
    assert _GitHubStub.requests == []


def test_real_token_rejects_body_file_outside_tmp(tmp_path) -> None:
    github_issues = _load_module()
    sensitive_file = tmp_path / ".env"
    sensitive_file.write_text("DISCORD_BOT_TOKEN=secret\n")

    try:
        github_issues.read_text_file(
            str(sensitive_file),
            token="ghs_real-installation-token",
        )
    except ValueError as error:
        assert "under /tmp" in str(error)
    else:
        raise AssertionError("sensitive file path was accepted")
