"""Regression tests for the Craig pipeline's Hermes sandbox env contract."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys


HERE = pathlib.Path(__file__).resolve().parent
SKILLS_ROOT = HERE.parent


def test_pipeline_accepts_dedicated_craig_environment_names(tmp_path):
    env = {
        **os.environ,
        "WIKI_PATH": str(tmp_path),
        "CRAIG_DISCORD_BOT_TOKEN": "test-token",
        "CRAIG_EVENTS_CHANNEL_ID": "123",
        "CRAIG_HOME_CHANNEL": "456",
        "CRAIG_GROQ_API_KEY": "test-groq-key",
        "HERMES_CLI_PATH": "/usr/bin/true",
    }
    env.pop("DISCORD_HOME_CHANNEL", None)
    env.pop("GROQ_API_KEY", None)

    result = subprocess.run(
        [sys.executable, str(HERE / "pipeline.py")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert result.returncode == 0, payload
    assert payload["status"] == "ok"


def test_transcriber_accepts_dedicated_craig_groq_key(tmp_path):
    env = {
        **os.environ,
        "WIKI_PATH": str(tmp_path),
        "CRAIG_GROQ_API_KEY": "test-groq-key",
        "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "google.json"),
        "AUDIO_DRIVE_FOLDER_ID": "folder-id",
    }
    env.pop("GROQ_API_KEY", None)

    result = subprocess.run(
        [
            sys.executable,
            str(SKILLS_ROOT / "craig-transcript-record" / "scan.py"),
            "--help",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout


def test_debrief_accepts_dedicated_craig_discord_environment(tmp_path):
    env = {
        **os.environ,
        "WIKI_PATH": str(tmp_path),
        "CRAIG_DISCORD_BOT_TOKEN": "test-token",
        "CRAIG_HOME_CHANNEL": "456",
    }
    env.pop("DISCORD_BOT_TOKEN", None)
    env.pop("DISCORD_HOME_CHANNEL", None)

    result = subprocess.run(
        [
            sys.executable,
            str(SKILLS_ROOT / "meeting-debrief" / "debrief.py"),
            "--help",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout


def test_debrief_dispatch_accepts_dedicated_craig_discord_token(tmp_path):
    env = {
        **os.environ,
        "WIKI_PATH": str(tmp_path),
        "CRAIG_DISCORD_BOT_TOKEN": "test-token",
    }
    env.pop("DISCORD_BOT_TOKEN", None)

    result = subprocess.run(
        [
            sys.executable,
            str(SKILLS_ROOT / "meeting-debrief" / "dispatch.py"),
            "--help",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout
