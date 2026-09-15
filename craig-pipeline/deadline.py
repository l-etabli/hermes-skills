"""Hard deadlines for the Linux pipeline's single-threaded recording workers."""
from __future__ import annotations

from contextlib import suppress
import math
import multiprocessing
import os
import signal


def _worker(connection, function, args):
    # Isolate scan/ffmpeg/git/Hermes descendants from the supervising cron.
    os.setsid()
    try:
        try:
            connection.send(('result', function(*args)))
        except Exception as exc:
            connection.send(('error', exc))
        # Keep the group leader alive until the parent has collected the result
        # and terminated the group, including any orphaned subprocesses.
        connection.recv()
    finally:
        connection.close()


def run_with_deadline(function, *args, timeout_s: float):
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError('deadline must be a positive finite duration')
    context = multiprocessing.get_context('fork')
    receiver, sender = context.Pipe()
    worker = context.Process(target=_worker, args=(sender, function, args))
    worker.start()
    sender.close()
    try:
        if not receiver.poll(timeout_s):
            raise TimeoutError(f'recording deadline exceeded after {timeout_s:g}s')
        try:
            kind, value = receiver.recv()
        except EOFError as exc:
            raise RuntimeError('recording worker exited without a result') from exc
        if kind == 'error':
            raise value
        return value
    finally:
        # SIGKILL is deliberate: a hung tool may ignore SIGTERM. The worker
        # owns a separate session; never signal the supervisor's process group.
        worker.join(timeout=0)  # reap a crashed leader before signalling its group
        with suppress(ProcessLookupError):
            os.killpg(worker.pid, signal.SIGKILL)
        if worker.is_alive():
            worker.kill()  # also covers a timeout before setsid()
        worker.join()
        receiver.close()
        worker.close()
