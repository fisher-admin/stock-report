"""Local failure ledger; outbound notifications contain only a fixed status line."""
import fcntl
import hashlib
import json
import math
import re
import subprocess
import time
from pathlib import Path

from managed_process import run_managed
from production_release_gate import write_json_atomic

EXISTING_STOCK_TARGET = 'discord:1492058190492467230'
PUBLIC_FAILURE_NOTICE = '选股系统盘后研究异常，请查看本机运行报告。'


def delivery_status(payload):
    if not isinstance(payload, dict):
        return 'unknown'
    if payload.get('skipped') is True:
        return 'skipped'
    if payload.get('error'):
        return 'failed'
    if payload.get('success') is True and isinstance(payload.get('message_id'), str) and payload['message_id'].strip():
        return 'accepted'
    return 'unknown'


def notify_failure(*, root, run_id, trade_date, stage, error_kind, target, sender=run_managed, timeout_s=20):
    if target not in (None, EXISTING_STOCK_TARGET):
        raise ValueError('new notification target is not allowed')
    timeout_s = float(timeout_s)
    if not math.isfinite(timeout_s) or not 0 <= timeout_s <= 20:
        raise ValueError('notification timeout must be between zero and twenty seconds')
    for field in (run_id, trade_date, stage, error_kind):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', field):
            raise ValueError('invalid local event identity')
    identity = {'run_id': run_id, 'trade_date': trade_date, 'stage': stage, 'error_kind': error_kind}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (key + '.json')
    with (root / (key + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**identity, 'status': 'busy'}
        event = {**identity, 'status': 'pending', 'attempts': 0, 'created_at': time.time()}
        if path.exists():
            try:
                event = json.loads(path.read_text())
                if any(event.get(k) != v for k, v in identity.items()):
                    return {**identity, 'status': 'unknown'}
            except (OSError, ValueError, TypeError, AttributeError):
                return {**identity, 'status': 'unknown'}
        if event['status'] == 'sending':
            event['status'] = 'unknown'
            write_json_atomic(path, event)
        if event['status'] in ('accepted', 'unknown', 'skipped'):
            return event
        write_json_atomic(path, event)
        if not target or timeout_s == 0 or event['attempts'] >= 3 or time.time() - event.get('attempted_at', 0) < 60:
            return event
        event.update(status='sending', attempts=event['attempts'] + 1, attempted_at=time.time())
        write_json_atomic(path, event)
        # No run/date/stage, logs, paths, credentials or stock data leave this process.
        command = [str(Path.home() / '.local/bin/hermes'), 'send', '--to', target, '--json', PUBLIC_FAILURE_NOTICE]
        try:
            result = sender(command, timeout=timeout_s)
            payload = json.loads(result.stdout)
            status = delivery_status(payload)
            if result.returncode != 0 and status == 'accepted':
                status = 'unknown'
            event['status'] = status
            if status == 'accepted':
                event['message_id'] = payload['message_id']
        except (subprocess.TimeoutExpired, OSError, RuntimeError, ValueError, TypeError):
            event['status'] = 'unknown'
        event['finished_at'] = time.time()
        write_json_atomic(path, event)
        return event


def retry_failures(events, *, root, target, deadline, clock=time.monotonic,
                   sleep=time.sleep, sender=run_managed):
    """Retry only explicit failures, at most three attempts, before final cutoff.

    Unknown, skipped, busy, and interrupted sends never become retryable here.
    The caller must have saved its failed terminal summary before entering.
    """
    events = list(events)
    if not target:
        return events
    while clock() < deadline - 2:
        pending = [(i, event) for i, event in enumerate(events)
                   if event.get('status') == 'failed' and event.get('attempts', 3) < 3]
        if not pending:
            break
        delay = min(max(0, event.get('attempted_at', 0) + 60 - time.time())
                    for _, event in pending)
        if delay > 0:
            sleep(min(delay, 1, max(0, deadline - 2 - clock())))
            continue
        for index, event in pending:
            remaining = deadline - 2 - clock()
            if remaining <= 0:
                return events
            if event.get('attempted_at', 0) + 60 > time.time():
                continue
            identity = {key: event[key] for key in ('run_id', 'trade_date', 'stage', 'error_kind')}
            events[index] = notify_failure(root=root, target=target, sender=sender,
                                          timeout_s=min(20, remaining), **identity)
    return events
