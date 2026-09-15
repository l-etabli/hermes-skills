from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


def _load():
    spec = importlib.util.spec_from_file_location('deadline', Path(__file__).with_name('deadline.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deadline_stops_worker_and_its_subprocess(tmp_path):
    deadline = _load()
    heartbeat = tmp_path / 'heartbeat'

    def stuck():
        code = 'import pathlib,time\np=pathlib.Path(' + repr(str(heartbeat)) + ')\nwhile True:\n p.write_text(str(time.monotonic()))\n time.sleep(0.02)'
        subprocess.Popen([sys.executable, '-c', code])
        while True:
            time.sleep(0.05)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match='deadline'):
        deadline.run_with_deadline(stuck, timeout_s=0.5)
    assert time.monotonic() - started < 3
    assert heartbeat.exists(), 'descendant must have started before the timeout'
    last = heartbeat.read_text()
    time.sleep(0.1)
    assert heartbeat.read_text() == last, 'descendant must stop with the worker'


def test_deadline_returns_result_and_propagates_errors():
    deadline = _load()
    assert deadline.run_with_deadline(lambda: {'status': 'done'}, timeout_s=2) == {'status': 'done'}

    def failing():
        raise ValueError('bad input')

    with pytest.raises(ValueError, match='bad input'):
        deadline.run_with_deadline(failing, timeout_s=2)


def test_deadline_detects_worker_crash():
    deadline = _load()
    with pytest.raises(RuntimeError, match='without a result'):
        deadline.run_with_deadline(lambda: os._exit(3), timeout_s=2)
