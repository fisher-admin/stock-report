#!/usr/bin/env python3
"""Advisory publication lock for single-writer publishing.

Features:
1. Atomic POSIX file creation (O_CREAT | O_EXCL).
2. Owner authentication: generates a unique random lock_token on acquire.
3. Release authorization check: release() requires the matching lock_token
   and/or matching live PID + process start timestamp to prevent unauthorized
   deletion or accidental release from unrelated processes.
4. Shell PID binding: binds to parent shell PID ($$) across subcommands.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class PublishAlreadyRunning(Exception):
    pass


class UnauthorizedRelease(Exception):
    pass


_DARWIN_PROCESS_START_TOLERANCE_CS = 200


def _get_process_start_time(pid: int) -> int | None:
    try:
        from gateway.status import get_process_start_time
        st = get_process_start_time(pid)
        if st is not None:
            return st
    except Exception:
        pass
    try:
        import subprocess
        proc = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            raw = proc.stdout.strip()
            import time
            st_time = time.strptime(raw)
            return int(time.mktime(st_time) * 100)
    except Exception:
        pass
    return None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _is_owner_live(pid: int, started_at: int | None) -> bool:
    if not _pid_exists(pid):
        return False
    if started_at is None:
        return True
    current = _get_process_start_time(pid)
    if current is None:
        return True
    if sys.platform == "darwin":
        return abs(int(started_at) - int(current)) <= _DARWIN_PROCESS_START_TOLERANCE_CS
    return int(started_at) == int(current)


@dataclass
class PublishLock:
    lock_path: Path
    pid: int | None = None
    run_id: str = ""
    trade_date: str = ""
    token: str | None = None

    def acquire(self) -> str:
        self.lock_path = Path(self.lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        target_pid = self.pid if self.pid is not None else os.getpid()
        self.token = secrets.token_hex(16)
        started_at = _get_process_start_time(target_pid)
        payload = json.dumps({
            "pid": target_pid,
            "token": self.token,
            "started_at": started_at,
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "acquired_at": time.time(),
        }, ensure_ascii=False, indent=2)

        for _ in range(3):
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                return self.token
            except FileExistsError:
                try:
                    data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                    owner_pid = int(data.get("pid") or 0)
                    owner_started_at = data.get("started_at")
                    if _is_owner_live(owner_pid, owner_started_at):
                        raise PublishAlreadyRunning(f"Publisher already running: pid={owner_pid}, data={data}")
                except (json.JSONDecodeError, ValueError, OSError):
                    pass
                # Dead owner -> safely unlink and retry
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
        raise PublishAlreadyRunning(f"Could not acquire publish lock at {self.lock_path}")

    def release(self, token: str | None = None, pid: int | None = None) -> None:
        self.lock_path = Path(self.lock_path)
        if not self.lock_path.exists():
            return

        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupted lockfile can be removed
            try:
                self.lock_path.unlink()
            except OSError:
                pass
            return

        stored_token = str(data.get("token") or "")
        stored_pid = int(data.get("pid") or 0)
        stored_started_at = data.get("started_at")
        check_token = token or self.token
        check_pid = pid if pid is not None else self.pid

        # 1. If stored owner process is alive, unauthorized release MUST be rejected
        owner_is_live = _is_owner_live(stored_pid, stored_started_at)
        if owner_is_live:
            # Token match is accepted
            token_matches = bool(check_token and check_token == stored_token)
            # PID match from the owner process itself is accepted
            pid_matches = bool(check_pid and check_pid == stored_pid and _is_owner_live(check_pid, stored_started_at))
            if not (token_matches or pid_matches):
                raise UnauthorizedRelease(
                    f"Unauthorized release rejected: caller (token={check_token}, pid={check_pid}) does not match live lock owner (pid={stored_pid})"
                )

        try:
            self.lock_path.unlink()
        except OSError:
            pass


@contextlib.contextmanager
def publish_lock(lock_path: Path, pid: int | None = None, run_id: str = "", trade_date: str = "") -> Iterator[PublishLock]:
    lock = PublishLock(lock_path, pid=pid, run_id=run_id, trade_date=trade_date)
    token = lock.acquire()
    try:
        yield lock
    finally:
        lock.release(token=token)


def main() -> int:
    parser = argparse.ArgumentParser(description="CLI interface for single-writer publish lock")
    parser.add_argument("--acquire", action="store_true", help="Acquire lock")
    parser.add_argument("--release", action="store_true", help="Release lock")
    parser.add_argument("--lock-file", required=True, help="Path to lock file")
    parser.add_argument("--token", default=None, help="Secret lock owner token")
    parser.add_argument("--pid", type=int, default=None, help="Process PID to bind the lock to")
    parser.add_argument("--run-id", default="", help="Run ID")
    parser.add_argument("--trade-date", default="", help="Trade date")
    args = parser.parse_args()

    lock = PublishLock(Path(args.lock_file), pid=args.pid, run_id=args.run_id, trade_date=args.trade_date, token=args.token)
    if args.acquire:
        try:
            tok = lock.acquire()
            print(f"TOKEN={tok}")
            return 0
        except PublishAlreadyRunning as exc:
            sys.stderr.write(f"ERROR: {exc}\n")
            return 4
    elif args.release:
        try:
            lock.release(token=args.token, pid=args.pid)
            return 0
        except UnauthorizedRelease as exc:
            sys.stderr.write(f"ERROR: {exc}\n")
            return 5
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
