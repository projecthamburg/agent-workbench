"""Private, bounded native-log receipts. Never export conversation or tool text.

First observation hashes existing history without interpreting it. Subsequent
receipts describe fixed byte ranges and allowlisted event counts only. A complete
previous-prefix hash is checked; replacement, truncation and mutation stop that
generation rather than silently overwriting its history.
"""
import argparse
import fcntl
import hashlib
import json
import os
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BLOCK = 1024 * 1024
TYPES = frozenset(('session_meta', 'turn_context', 'response_item', 'event_msg',
                  'user', 'assistant', 'system', 'progress', 'summary',
                  'USER_INPUT', 'PLANNER_RESPONSE', 'SYSTEM', 'TOOL_CALL',
                  'user_message', 'assistant_message', 'system_message',
                  'tool_call', 'step_start', 'step_end', 'error'))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def publish(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(canonical(value)); output.flush(); os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def digest_range(handle, start, end):
    handle.seek(start)
    digest = hashlib.sha256()
    left = end - start
    while left:
        block = handle.read(min(BLOCK, left))
        if not block: raise ValueError('Source shortened during snapshot')
        digest.update(block); left -= len(block)
    return digest.hexdigest()


def metadata(handle, start, end):
    """Count complete records; oversized records are counted, never accumulated."""
    handle.seek(start)
    counts = Counter()
    first_time = last_time = None
    left = end - start
    pending = bytearray()
    oversized = False
    complete_end = start
    position = start
    while left:
        block = handle.read(min(BLOCK, left))
        if not block: raise ValueError('Source shortened during metadata capture')
        left -= len(block)
        for part in block.splitlines(keepends=True):
            position += len(part)
            if len(pending) + len(part) > BLOCK:
                pending.clear(); oversized = True
            elif not oversized:
                pending.extend(part)
            if not part.endswith(b'\n'): continue
            complete_end = position
            if oversized:
                counts['OVERSIZED_RECORD'] += 1
            else:
                try:
                    item = json.loads(pending.decode('utf-8', errors='strict'))
                    kind = item.get('type', item.get('event')) if isinstance(item, dict) else None
                    counts[kind if isinstance(kind, str) and kind in TYPES else 'OTHER'] += 1
                    raw = item.get('timestamp', item.get('created_at')) if isinstance(item, dict) else None
                    if isinstance(raw, str) and len(raw) <= 40:
                        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                        if parsed.tzinfo is not None:
                            stamp = parsed.astimezone(timezone.utc).isoformat()
                            first_time = min(first_time, stamp) if first_time else stamp
                            last_time = max(last_time, stamp) if last_time else stamp
                except (ValueError, UnicodeError, TypeError):
                    counts['UNPARSEABLE_OR_INVALID_TIMESTAMP'] += 1
            pending.clear(); oversized = False
    return dict(counts), complete_end, first_time, last_time


def capture(source, session_id, previous=None):
    source = Path(source).resolve(strict=True)
    previous = previous or None
    with source.open('rb') as handle:
        before = os.fstat(handle.fileno())
        identity = {'path': str(source), 'session_id': session_id,
                    'device': before.st_dev, 'inode': before.st_ino}
        size = before.st_size
        start = 0
        if previous:
            bound = dict(previous)
            claimed = bound.pop('receipt_sha256')
            if hashlib.sha256(canonical(bound)).hexdigest() != claimed:
                raise ValueError('Previous receipt integrity failed')
            if previous['identity'] != identity: raise ValueError('Source rotation: preserve previous generation')
            start = previous['observed_size']
            if not 0 <= previous['parse_offset'] <= start:
                raise ValueError('Invalid previous parse boundary')
            if size < start: raise ValueError('Source truncation: preserve previous generation')
            if digest_range(handle, 0, start) != previous['prefix_sha256']:
                raise ValueError('Previously captured prefix mutated')
        prefix = digest_range(handle, 0, size)
        delta = digest_range(handle, start, size)
        if previous:
            counts, parse_end, first, last = metadata(handle, previous['parse_offset'], size)
        else:
            counts, first, last = {}, None, None
            # Baseline ends at a known record boundary without storing its text.
            handle.seek(max(0, size - BLOCK))
            tail = handle.read(min(size, BLOCK))
            newline = tail.rfind(b'\n')
            parse_end = size - len(tail) + newline + 1 if newline >= 0 else 0
        after = os.fstat(handle.fileno())
        current = source.stat()
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or current.st_ino != before.st_ino:
            raise ValueError('Source identity changed during capture')
        if after.st_size < size or digest_range(handle, 0, size) != prefix:
            raise ValueError('Fixed snapshot prefix changed during capture')
    record = {'schema_version': 1, 'identity': identity,
              'observed_at': datetime.now(timezone.utc).isoformat(),
              'observed_size': size, 'range_start': start, 'range_end': size,
              'range_sha256': delta, 'prefix_sha256': prefix,
              'parse_offset': parse_end, 'event_counts': counts,
              'first_event_utc': first, 'last_event_utc': last,
              'baseline_only': previous is None,
              'previous_receipt_sha256': previous.get('receipt_sha256') if previous else None,
              'limitations': 'Metadata only; event counts do not prove task completion or capture private reasoning. Private source identity is not a public artifact.'}
    record['receipt_sha256'] = hashlib.sha256(canonical(record)).hexdigest()
    return record


def _collect(registry, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    statuses = []
    for session in json.loads(Path(registry).read_text())['sessions']:
        path, session_id = session['native_path'], session['session_id']
        key = hashlib.sha256(canonical([session_id, path])).hexdigest()
        state_path = destination / (key + '.state.json')
        previous = json.loads(state_path.read_text()) if state_path.exists() else None
        try:
            receipt = capture(path, session_id, previous)
            publish(destination / 'receipts' / (receipt['receipt_sha256'] + '.json'), receipt)
            publish(state_path, receipt)
            statuses.append({'role': session['role'], 'status': 'CAPTURED',
                             'receipt_sha256': receipt['receipt_sha256'], 'observed_size': receipt['observed_size']})
        except (ValueError, OSError):
            statuses.append({'role': session['role'], 'status': 'HELD_SOURCE_CHANGE_OR_READ_FAILURE'})
    publish(destination / 'latest.json', {'at': datetime.now(timezone.utc).isoformat(), 'sessions': statuses})
    return statuses


def collect(registry, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(destination / 'collector.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _collect(registry, destination)
    finally:
        os.close(lock)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--interval', type=int)
    parser.add_argument('--until-utc')
    args = parser.parse_args()
    if args.interval is not None:
        if not 60 <= args.interval <= 3600 or not args.until_utc:
            parser.error('Periodic capture requires interval60..3600 and explicit until-utc')
        deadline = datetime.fromisoformat(args.until_utc.replace('Z', '+00:00'))
        if deadline.tzinfo is None: parser.error('Deadline requires timezone')
        while datetime.now(timezone.utc) < deadline:
            print(json.dumps(collect(args.registry, args.destination)), flush=True)
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0: time.sleep(min(args.interval, remaining))
    else:
        print(json.dumps(collect(args.registry, args.destination)))
