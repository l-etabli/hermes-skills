from __future__ import annotations

import importlib.util
import fcntl
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


def _load_pipeline(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("CRAIG_DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("CRAIG_EVENTS_CHANNEL_ID", "123")
    monkeypatch.setenv("CRAIG_HOME_CHANNEL", "456")
    monkeypatch.setenv("HERMES_CLI_PATH", "/usr/bin/true")

    module_path = Path(__file__).with_name("pipeline.py")
    spec = importlib.util.spec_from_file_location("craig_pipeline_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generate_debrief_uses_supported_hermes_backend_contract(monkeypatch, tmp_path):
    pipeline = _load_pipeline(monkeypatch, tmp_path)
    transcript = tmp_path / "raw" / "transcripts" / "meeting.md"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("---\nduration_s: 300\n---\nMeeting body.\n", encoding="utf-8")
    schema = {
        "type": "object",
        "required": ["transcript_path"],
        "properties": {"transcript_path": {"type": "string"}},
        "additionalProperties": True,
    }

    def fake_complete_text(config, messages, *, purpose):
        assert config is pipeline.LLM_CONFIG
        assert purpose == "generate meeting debrief JSON"
        return json.dumps({"transcript_path": "replaced-by-pipeline"}), None

    monkeypatch.setattr(pipeline, "llm_complete_text", fake_complete_text)

    result, error = pipeline.generate_debrief(transcript, schema)

    assert error is None
    assert result == {"transcript_path": "raw/transcripts/meeting.md"}


def test_process_exception_is_never_reported_as_global_ok(
    monkeypatch, tmp_path, capsys
):
    pipeline = _load_pipeline(monkeypatch, tmp_path)
    pending = tmp_path / ".craig-pending" / "recording.json"
    monkeypatch.setattr(pipeline, "list_pending", lambda: [pending])
    monkeypatch.setattr(
        pipeline,
        "process_pending",
        lambda _path: (_ for _ in ()).throw(
            TypeError(
                "complete_text() got an unexpected keyword argument "
                "'response_format'"
            )
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "maybe_self_delete_cron",
        lambda: {"status": "skipped"},
    )
    monkeypatch.setattr(sys, "argv", ["pipeline.py"])

    returncode = pipeline.main()
    payload = json.loads(capsys.readouterr().out)

    assert returncode != 0
    assert payload["status"] == "error"
    assert payload["summaries"][0]["phase"] == "exception"


def test_self_delete_finds_paused_exact_job_with_list_all(
    monkeypatch, tmp_path
):
    pipeline = _load_pipeline(monkeypatch, tmp_path)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[-3:] == ["cron", "list", "--all"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "  8a3f6b097b60 [paused]\n"
                    "    Name:      craig-watch-followup\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    monkeypatch.setattr(pipeline, "list_pending", lambda: [])
    monkeypatch.setattr(
        pipeline, "discord_post", lambda *_args, **_kwargs: ("message", None)
    )

    result = pipeline.maybe_self_delete_cron()

    assert result["status"] == "removed"
    assert result["job_id"] == "8a3f6b097b60"
    assert calls[0][-3:] == ["cron", "list", "--all"]


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, message: str):
    _git(repo, "add", ".")
    _git(
        repo,
        "-c", "user.name=Test",
        "-c", "user.email=test@example.com",
        "commit", "-m", message,
    )


def test_v1_pending_with_historical_ingest_resumes_without_reingest(
    monkeypatch, tmp_path
):
    pipeline = _load_pipeline(monkeypatch, tmp_path)
    _git(tmp_path, "init")
    transcript = tmp_path / "raw" / "transcripts" / "meeting.md"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "---\ncraig_id: recording-1\nduration_s: 300\n---\nBody\n",
        encoding="utf-8",
    )
    _commit(tmp_path, "raw: capture transcript meeting")
    note = tmp_path / "log.md"
    note.write_text("ingested\n", encoding="utf-8")
    _commit(tmp_path, "wiki: ingest meeting")

    pending = tmp_path / ".craig-pending" / "recording-1.json"
    pending.parent.mkdir()
    pending.write_text(
        json.dumps({
            "craig_id": "recording-1",
            "first_seen_at": datetime.now(timezone.utc).isoformat(),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pipeline, "_ensure_status_thread", lambda *_args: ("thread", None)
    )
    monkeypatch.setattr(
        pipeline, "discord_post", lambda *_args, **_kwargs: ("message", None)
    )
    monkeypatch.setattr(
        pipeline, "run_scan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("scan must be skipped")
        ),
    )
    monkeypatch.setattr(
        pipeline, "git_commit_push",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("transcript push must be skipped")
        ),
    )
    monkeypatch.setattr(
        pipeline, "llm_wiki_ingest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ingest must be skipped")
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "generate_debrief",
        lambda *_args: ({"transcript_path": "raw/transcripts/meeting.md"}, None),
    )
    monkeypatch.setattr(
        pipeline,
        "run_debrief_py",
        lambda *_args: (0, {"status": "posted", "thread_id": "debrief-thread"}),
    )
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({
            "type": "object",
            "required": ["transcript_path"],
            "properties": {"transcript_path": {"type": "string"}},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline, "DEBRIEF_SCHEMA", schema_path)

    result = pipeline.process_pending(pending)

    assert result["status"] == "done"
    assert not pending.exists()
    assert len(_git(
        tmp_path,
        "log",
        "--format=%H",
        "--grep=^wiki: ingest meeting$",
    ).stdout.splitlines()) == 1


def test_three_identical_retryable_failures_quarantine_once(
    monkeypatch, tmp_path
):
    pipeline = _load_pipeline(monkeypatch, tmp_path)
    pending_path = tmp_path / ".craig-pending" / "recording.json"
    pending = pipeline.migrate_pending({
        "craig_id": "recording",
        "pipeline_thread_id": "thread",
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
    })
    pipeline.save_pending(pending_path, pending)
    notifications = []
    monkeypatch.setattr(
        pipeline,
        "discord_post",
        lambda channel, content, **_kwargs: (
            notifications.append((channel, content)) or "message",
            None,
        ),
    )

    first = pipeline.record_failure(
        pending_path, pending, "scan", "network timeout contacting Drive"
    )
    pending = pipeline.load_pending(pending_path)
    second = pipeline.record_failure(
        pending_path, pending, "scan", "network timeout contacting Drive"
    )
    pending = pipeline.load_pending(pending_path)
    third = pipeline.record_failure(
        pending_path, pending, "scan", "network timeout contacting Drive"
    )

    assert first["status"] == second["status"] == "retryable-error"
    assert third["status"] == "quarantined"
    assert not pending_path.exists()
    failed = pipeline.load_pending(tmp_path / ".craig-failed" / "recording.json")
    assert failed["failures"]["consecutive"] == 3
    assert len(failed["failures"]["history"]) == 3
    assert [channel for channel, _ in notifications] == ["thread", "456"]


def test_completed_checkpoint_does_not_reset_repeated_later_failure(
    monkeypatch, tmp_path
):
    pipeline = _load_pipeline(monkeypatch, tmp_path)
    pending_path = tmp_path / ".craig-pending" / "recording.json"
    pending = pipeline.migrate_pending({
        "craig_id": "recording",
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
    })
    pipeline._complete_stage(
        pending,
        "debrief_generation",
        transcript_sha256="transcript-sha",
    )
    pipeline.save_pending(pending_path, pending)

    first = pipeline.record_failure(
        pending_path,
        pending,
        "debrief-post",
        "discord-create-thread: discord-400 code 160004",
    )
    pending = pipeline.load_pending(pending_path)
    pipeline._complete_stage(
        pending,
        "debrief_generation",
        transcript_sha256="transcript-sha",
    )
    second = pipeline.record_failure(
        pending_path,
        pending,
        "debrief-post",
        "discord-create-thread: discord-400 code 160004",
    )

    assert first["consecutive"] == 1
    assert second["consecutive"] == 2


def test_type_error_is_quarantined_immediately(monkeypatch, tmp_path):
    pipeline = _load_pipeline(monkeypatch, tmp_path)
    pending_path = tmp_path / ".craig-pending" / "recording.json"
    pending = pipeline.migrate_pending({
        "craig_id": "recording",
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
    })
    pipeline.save_pending(pending_path, pending)
    monkeypatch.setattr(
        pipeline, "discord_post", lambda *_args, **_kwargs: ("message", None)
    )

    result = pipeline.record_failure(
        pending_path,
        pending,
        "debrief-generation",
        TypeError("unsupported response_format"),
    )

    assert result["status"] == "quarantined"
    assert not pending_path.exists()


def test_expired_pending_stops_before_scan_ingest_or_debrief(
    monkeypatch, tmp_path
):
    pipeline = _load_pipeline(monkeypatch, tmp_path)
    pending_path = tmp_path / ".craig-pending" / "old.json"
    pipeline.save_pending(pending_path, {
        "craig_id": "old",
        "first_seen_at": (
            datetime.now(timezone.utc) - timedelta(hours=6, minutes=1)
        ).isoformat(),
    })
    monkeypatch.setattr(
        pipeline, "discord_post", lambda *_args, **_kwargs: ("message", None)
    )
    for name in ("run_scan", "llm_wiki_ingest", "generate_debrief"):
        monkeypatch.setattr(
            pipeline,
            name,
            lambda *_args, _name=name, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"{_name} must not run")
            ),
        )

    result = pipeline.process_pending(pending_path)

    assert result["status"] == "expired"
    assert (tmp_path / ".craig-failed" / "old.json").exists()


def test_process_lock_makes_concurrent_run_a_silent_success(
    monkeypatch, tmp_path, capsys
):
    pipeline = _load_pipeline(monkeypatch, tmp_path)
    pipeline.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with pipeline.LOCK_PATH.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(sys, "argv", ["pipeline.py"])
        returncode = pipeline.main()

    payload = json.loads(capsys.readouterr().out)
    assert returncode == 0
    assert payload["status"] == "already-running"
