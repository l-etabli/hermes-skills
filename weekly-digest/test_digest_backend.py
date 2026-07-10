"""Regression tests for the subscription-backed weekly digest."""

from __future__ import annotations

import importlib.util
import os
import pathlib


HERE = pathlib.Path(__file__).resolve().parent


def load_digest(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("CRAIG_DISCORD_BOT_TOKEN", "discord-test-token")
    monkeypatch.setenv("CRAIG_HOME_CHANNEL", "123")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_HOME_CHANNEL", raising=False)
    spec = importlib.util.spec_from_file_location("weekly_digest", HERE / "digest.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_digest_uses_hermes_subscription_without_external_api_key(monkeypatch, tmp_path):
    digest = load_digest(monkeypatch, tmp_path)
    calls = []

    class Result:
        returncode = 0
        stdout = "Résumé GPT"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    text, error = digest.call_hermes(
        "recap",
        7,
        "messages",
        run_command=fake_run,
    )

    assert error is None
    assert text == "Résumé GPT"
    assert calls[0][0][1] == "-z"
    assert "messages" in calls[0][0][2]
