"""Bounded POSIX process groups with a parent-death lifeline.

The guardian owns a new group; jobs must not daemonize or create new sessions.
The parent alone owns the pipe's write end. Even SIGKILL closes it, so the
guardian can kill its group when the outer scheduler kills the parent group.
No PID-name matching or retries are performed here.
Caller must exclusively reap the guardian and must not fork while waiting.
Escaping sessions, simultaneous supervisor deaths and output-disk exhaustion
are unsupported. Output tail size is not a temporary-file disk quota.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _guardian(lifeline: int, result_path: str, command: list[str]) -> None:
    if os.getpgrp() != os.getpid():
        raise RuntimeError('guardian does not own its process group')
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True)
        reported = False
        while True:
            readable, _, _ = select.select([lifeline], [], [], .05)
            if readable and os.read(lifeline, 1) == b'':
                break
            code = child.poll()
            if code is not None and not reported:
                temporary = Path(result_path + '.tmp')
                temporary.write_text(json.dumps({'returncode': code}))
                os.replace(temporary, result_path)
                reported = True
                # Remain alive to pin the group until the caller kills it.
                # A result is READY, not proof that descendants have stopped.
    finally:
        # Includes descendants left behind by an exited child. The parent's
        # wait reaps the guardian only after this group-wide kill was issued.
        os.killpg(os.getpgrp(), signal.SIGKILL)


def _observe_exit(pid, timeout, ready_path=None):
    deadline = time.monotonic() + timeout
    while True:
        if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None:
            return 'exited'
        if ready_path is not None and ready_path.is_file():
            return 'ready'
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(.01, max(0, deadline - time.monotonic())))


def run_managed(command, *, timeout, env=None, cwd=None, capture_output=True, text=True):
    if os.name != 'posix' or not capture_output or not text:
        raise ValueError('managed runner requires POSIX and captured text output')
    if not all(hasattr(os, name) for name in ('waitid', 'P_PID', 'WEXITED', 'WNOWAIT', 'WNOHANG')):
        raise ValueError('managed runner requires non-reaping waitid; use the locked stock Python')
    if timeout <= 0:
        raise subprocess.TimeoutExpired(command, timeout)
    reader, writer = os.pipe()
    guardian = None
    cleaned = False
    def cleanup():
        nonlocal cleaned
        if guardian is None or cleaned:
            return
        # WNOWAIT pins the leader PID even if it has died. Never kill a PGID
        # after wait/poll has reaped it, including an externally killed guardian.
        try:
            os.waitid(os.P_PID, guardian.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError as exc:
            cleaned = True
            raise RuntimeError('guardian identity lost; cleanup unconfirmed; do not retry') from exc
        try:
            try:
                os.killpg(guardian.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            # Reap once even on cleanup failure; never issue another PGID kill
            # after this point. Failure propagates and prohibits safe retry.
            cleaned = True
            guardian.wait(timeout=2)
    try:
        with tempfile.TemporaryDirectory(prefix='stock-process-') as tmp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            result_path = Path(tmp) / 'result.json'
            guardian = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(reader), str(result_path), *command],
                stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, cwd=cwd,
                pass_fds=(reader,), start_new_session=True,
            )
            os.close(reader)
            reader = -1
            expired = False
            try:
                observed = _observe_exit(guardian.pid, timeout, result_path)
                expired = not observed
            finally:
                cleanup()
            def tail(handle):
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 1024 * 1024))
                return handle.read().decode('utf-8', errors='replace')
            stdout, stderr = tail(out), tail(err)
            if expired:
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            if observed != 'ready':
                raise RuntimeError('guardian exited unexpectedly; do not retry')
            if not result_path.is_file() or result_path.stat().st_size > 4096:
                raise RuntimeError('process guardian ended without a completion receipt')
            result = json.loads(result_path.read_text())
            if not isinstance(result, dict) or type(result.get('returncode')) is not int or not -255 <= result['returncode'] <= 255:
                raise RuntimeError('invalid process completion receipt')
            return subprocess.CompletedProcess(command, result['returncode'], stdout, stderr)
    finally:
        if reader >= 0:
            os.close(reader)
        try:
            cleanup()
        finally:
            if writer >= 0:
                os.close(writer)


if __name__ == '__main__':
    _guardian(int(sys.argv[1]), sys.argv[2], sys.argv[3:])
