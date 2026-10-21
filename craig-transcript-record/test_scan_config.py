import runpy
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("scan.py")


@pytest.fixture
def scan_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("AUDIO_DRIVE_FOLDER_ID", "test-folder")
    monkeypatch.setenv("CRAIG_GROQ_API_KEY", "test-key")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("CRAIG_DRIVE_CREDENTIALS", raising=False)
    return monkeypatch


def test_scan_accepts_dedicated_drive_path_when_provider_env_is_scrubbed(scan_env):
    scan_env.setenv("CRAIG_DRIVE_CREDENTIALS", "/opt/data/google-drive-sa.json")
    assert runpy.run_path(str(SCRIPT))["SA_PATH"] == "/opt/data/google-drive-sa.json"


def test_dedicated_drive_path_wins_over_unrelated_provider_credentials(scan_env):
    scan_env.setenv("CRAIG_DRIVE_CREDENTIALS", "/drive.json")
    scan_env.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/vertex.json")
    assert runpy.run_path(str(SCRIPT))["SA_PATH"] == "/drive.json"


def test_legacy_drive_path_remains_supported(scan_env):
    scan_env.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/legacy.json")
    assert runpy.run_path(str(SCRIPT))["SA_PATH"] == "/legacy.json"


def test_scan_fails_clearly_without_either_drive_path(scan_env, capsys):
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(SCRIPT))
    assert error.value.code == 2
    assert "CRAIG_DRIVE_CREDENTIALS" in capsys.readouterr().out
