from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_listener(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("CRAIG_DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("CRAIG_EVENTS_CHANNEL_ID", "123")

    module_path = Path(__file__).with_name("listener.py")
    spec = importlib.util.spec_from_file_location("craig_listener_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_followup_cron_delivery_stays_local(monkeypatch, tmp_path):
    listener = _load_listener(monkeypatch, tmp_path)
    cli_path = "/opt/hermes/.venv/bin/hermes"
    monkeypatch.setenv("HERMES_CLI_PATH", cli_path)
    monkeypatch.setattr(listener.pathlib.Path, "exists", lambda _self: True)

    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[-3:] == ["cron", "list", "--all"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="created", stderr="")

    monkeypatch.setattr(listener.subprocess, "run", fake_run)

    assert listener.ensure_followup_cron() == "created"
    assert len(calls) == 2
    create_command = calls[1]
    assert create_command[:4] == [
        cli_path,
        "cron",
        "create",
        "every 5m",
    ]
    deliver_index = create_command.index("--deliver")
    assert create_command[deliver_index + 1] == "local"
    assert create_command[-3:] == [
        "--script",
        "craig-pipeline-runner.sh",
        "--no-agent",
    ]


def test_existing_followup_cron_is_not_recreated(monkeypatch, tmp_path):
    listener = _load_listener(monkeypatch, tmp_path)
    monkeypatch.setattr(listener.pathlib.Path, "exists", lambda _self: True)
    run = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout=(
            "  abcdef123456 [active]\n"
            f"    Name:      {listener.FOLLOWUP_CRON_NAME}\n"
        ),
        stderr="",
    )
    monkeypatch.setattr(listener.subprocess, "run", run)

    assert listener.ensure_followup_cron() == "already-running"


def test_paused_followup_cron_is_not_recreated_or_resumed(monkeypatch, tmp_path):
    listener = _load_listener(monkeypatch, tmp_path)
    monkeypatch.setattr(listener.pathlib.Path, "exists", lambda _self: True)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "  8a3f6b097b60 [paused]\n"
                "    Name:      craig-watch-followup\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(listener.subprocess, "run", fake_run)

    assert listener.ensure_followup_cron() == "paused"
    assert calls == [[
        "/opt/hermes/.venv/bin/hermes", "cron", "list", "--all"
    ]]
