from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_debrief(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("CRAIG_DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("CRAIG_HOME_CHANNEL", "456")

    module_path = Path(__file__).with_name("debrief.py")
    spec = importlib.util.spec_from_file_location(
        "meeting_debrief_under_test", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_recap_thread_reuses_latest_complete_attempt(
    monkeypatch, tmp_path
):
    debrief = _load_debrief(monkeypatch, tmp_path)
    chunks = ["recap-first", "### Actions\n1. action"]
    calls = []

    def fake_get(path):
        calls.append(path)
        if path == "/channels/456/messages?limit=100":
            return (
                [
                    {"id": "new-actions", "content": chunks[1]},
                    {"id": "new-parent", "content": chunks[0]},
                    {"id": "old-actions", "content": chunks[1]},
                    {"id": "old-parent", "content": chunks[0]},
                ],
                None,
            )
        if path == "/channels/new-parent":
            return {
                "id": "new-parent",
                "parent_id": "456",
                "thread_metadata": {"archived": False},
            }, None
        raise AssertionError(path)

    monkeypatch.setattr(debrief, "discord_get", fake_get)

    result = debrief.find_existing_recap_thread(chunks)

    assert result == {
        "parent_message_id": "new-parent",
        "actions_message_id": "new-actions",
        "actions_message_content": chunks[1],
        "last_message_id": "new-actions",
        "thread_id": "new-parent",
    }
    assert calls == [
        "/channels/456/messages?limit=100",
        "/channels/new-parent",
    ]


def test_create_thread_accepts_discord_auto_thread_conflict(
    monkeypatch, tmp_path
):
    debrief = _load_debrief(monkeypatch, tmp_path)
    monkeypatch.setattr(
        debrief,
        "discord_post",
        lambda *_args, **_kwargs: (
            None,
            'discord-400: {"message":"already created","code":160004}',
        ),
    )
    monkeypatch.setattr(
        debrief,
        "discord_get",
        lambda path: (
            {
                "id": "parent-message",
                "parent_id": "456",
                "thread_metadata": {"archived": False},
            },
            None,
        )
        if path == "/channels/parent-message"
        else (_ for _ in ()).throw(AssertionError(path)),
    )

    thread, error = debrief.create_or_reuse_thread(
        "parent-message", "Debrief meeting.md"
    )

    assert error is None
    assert thread["id"] == "parent-message"
