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


def test_ended_panel_is_queued_once_without_inline_transcription(monkeypatch, tmp_path, capsys):
    import json
    import sys
    listener = _load_listener(monkeypatch, tmp_path)
    panel = {'id': '456', 'content': '', 'embeds': [], 'components': [
        {'type': 17, 'components': [
            {'type': 10, 'content': 'Recording ended.\n**Recording ID:** `recording`'},
        ]},
    ]}
    monkeypatch.setattr(listener, 'find_latest_craig_panel', lambda: (panel, None))
    monkeypatch.setattr(listener, 'ensure_followup_cron', lambda: 'already-running')
    monkeypatch.setattr(sys, 'argv', ['listener.py'])
    monkeypatch.setattr(listener.subprocess, 'run', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('no inline scan')))
    assert listener.main() == 0
    first = json.loads(capsys.readouterr().out)
    state = (tmp_path / '.craig-pending/recording.json').read_text()
    assert first['status'] == 'pending'
    assert listener.main() == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'already-pending'
    assert (tmp_path / '.craig-pending/recording.json').read_text() == state


def test_terminal_recordings_are_not_requeued(monkeypatch, tmp_path, capsys):
    import json
    import sys
    listener = _load_listener(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, 'argv', ['listener.py'])
    monkeypatch.setattr(listener, 'find_latest_craig_panel', lambda: ({
        'id': '456', 'content': 'Recording ended. Recording ID: recording',
    }, None))
    monkeypatch.setattr(listener, 'ensure_followup_cron', lambda: (_ for _ in ()).throw(AssertionError('no cron')))
    for folder, reason in [('.craig-pending/completed', 'already-completed'), ('.craig-failed', 'quarantined')]:
        marker = tmp_path / folder / 'recording.json'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{}')
        assert listener.main() == 0
        assert json.loads(capsys.readouterr().out)['reason'] == reason
        assert not (tmp_path / '.craig-pending/recording.json').exists()
        marker.unlink()
