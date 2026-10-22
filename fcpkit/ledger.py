#!/usr/bin/env python3
"""Append-only write ledger with a safe undo (stdlib only).

Every tool that writes a file (an FCPXML build, a plan, a render) records
one JSON line in a ledger file (e.g. `<project>/LEDGER.jsonl`): tool, action, args, and the
sha256 of each input and output. A .fcpxmld bundle (or any folder) is
hashed whole, every file in sorted order, not just Info.fcpxml.

Undo never deletes: it moves each output to `<parent>/undone/<stamp>-<name>`
and refuses when an output is missing or its bytes differ from the recorded
sha256 (someone edited it since; the user decides). Deletes stay with the
user. A torn line (crash mid-write) is skipped when reading and the next
append starts on a fresh line.

    record(ledger, tool, action, args, inputs, outputs) -> entry
    entries(ledger) -> [entry]
    undo(ledger, entry_id) -> report      (raises UndoRefused)
    verify(ledger) -> [status per entry]

CLI: fcpkit-ledger list|verify|undo|record --ledger <project>/LEDGER.jsonl [--id ...]
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SKIP_NAMES = {'.DS_Store'}  # Finder litter inside a bundle is not content


class UndoRefused(RuntimeError):
    def __init__(self, entry_id: str, problems: list[dict]):
        super().__init__(f'undo of {entry_id} refused: {problems}')
        self.entry_id = entry_id
        self.problems = problems


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def digest_path(path: str | Path) -> str | None:
    """sha256 of a file, or of a folder/bundle (relative path + file sha, sorted).

    None when the path does not exist. Symlinks inside a bundle are hashed by
    their target text, not followed.
    """
    path = Path(path).expanduser()
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_dir():
        return _file_sha(path)
    h = hashlib.sha256(b'bundle-v1\n')
    for sub in sorted(path.rglob('*'), key=lambda p: p.relative_to(path).as_posix()):
        if sub.name in SKIP_NAMES:
            continue
        rel = sub.relative_to(path).as_posix()
        if sub.is_symlink():
            h.update(f'L {rel}\0{os.readlink(sub)}\n'.encode())
        elif sub.is_file():
            h.update(f'F {rel}\0{_file_sha(sub)}\n'.encode())
        elif sub.is_dir():
            h.update(f'D {rel}\n'.encode())
    return h.hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec='seconds').replace('+00:00', 'Z')


def _abs(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _append(ledger_path: Path, entry: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n'
    with ledger_path.open('a+b') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0, os.SEEK_END)
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b'\n':
                line = '\n' + line  # isolate a torn previous line
        f.write(line.encode())
        f.flush()
        os.fsync(f.fileno())


def record(ledger_path, tool: str, action: str, args: dict, inputs: list, outputs: list) -> dict:
    """Append one write record; returns the entry (with its `id`)."""
    when = _now()
    entry = {
        'id': when.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8],
        'ts': _iso(when),
        'tool': tool,
        'action': action,
        'args': args,
        'inputs': [{'path': str(_abs(p)), 'sha256': digest_path(p)} for p in inputs],
        'outputs': [{'path': str(_abs(p)), 'sha256': digest_path(p)} for p in outputs],
    }
    _append(Path(ledger_path), entry)
    return entry


def entries(ledger_path, skipped: list | None = None) -> list[dict]:
    """All readable entries in order; torn/garbled lines go to `skipped` (line numbers)."""
    path = Path(ledger_path)
    if not path.exists():
        return []
    out = []
    for n, raw in enumerate(path.read_bytes().split(b'\n'), 1):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
            if not isinstance(entry, dict) or 'id' not in entry:
                raise ValueError('not an entry')
        except ValueError:
            if skipped is not None:
                skipped.append(n)
            continue
        out.append(entry)
    return out


def _undone_ids(all_entries: list[dict]) -> set[str]:
    return {e['args'].get('target') for e in all_entries if e.get('action') == 'undo'}


def undo(ledger_path, entry_id: str) -> dict:
    """Move the entry's outputs into `undone/`; refuse if any output is missing or edited."""
    all_entries = entries(ledger_path)
    entry = next((e for e in all_entries if e['id'] == entry_id), None)
    if entry is None:
        raise UndoRefused(entry_id, [{'problem': 'no such entry'}])
    if entry.get('action') == 'undo':
        raise UndoRefused(entry_id, [{'problem': 'an undo entry cannot be undone; move the files back by hand'}])
    if entry_id in _undone_ids(all_entries):
        raise UndoRefused(entry_id, [{'problem': 'already undone'}])
    if not entry['outputs']:
        raise UndoRefused(entry_id, [{'problem': 'entry has no outputs'}])

    problems = []
    for out in entry['outputs']:
        current = digest_path(out['path'])
        if current is None:
            problems.append({'path': out['path'], 'problem': 'missing'})
        elif current != out['sha256']:
            problems.append({'path': out['path'], 'problem': 'changed since recorded',
                             'recorded': out['sha256'], 'current': current})
    if problems:
        raise UndoRefused(entry_id, problems)

    stamp = _now().strftime('%Y%m%dT%H%M%SZ')
    moves = []
    try:
        for out in entry['outputs']:
            src = Path(out['path'])
            dest_dir = src.parent / 'undone'
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / f'{stamp}-{src.name}'
            k = 1
            while dest.exists() or dest.is_symlink():
                k += 1
                dest = dest_dir / f'{stamp}-{k}-{src.name}'
            os.rename(src, dest)  # same parent filesystem; never a copy+delete
            moves.append({'from': str(src), 'to': str(dest), 'sha256': out['sha256']})
    except OSError as err:
        for m in reversed(moves):  # put back what already moved
            os.rename(m['to'], m['from'])
        raise UndoRefused(entry_id, [{'problem': f'move failed, rolled back: {err}'}]) from err

    undo_entry = record(ledger_path, 'ledger', 'undo', {'target': entry_id, 'moves': moves},
                        [], [m['to'] for m in moves])
    return {'undone': entry_id, 'moves': moves, 'undo_entry': undo_entry['id']}


def verify(ledger_path) -> list[dict]:
    """Per entry: does each recorded output still match? (ok / changed / missing / undone)."""
    all_entries = entries(ledger_path)
    undone = _undone_ids(all_entries)
    report = []
    for e in all_entries:
        outs = []
        for out in e['outputs']:
            current = digest_path(out['path'])
            if e['id'] in undone:
                status = 'undone'
            elif current is None:
                status = 'missing'
            elif current == out['sha256']:
                status = 'ok'
            else:
                status = 'changed'
            outs.append({'path': out['path'], 'status': status})
        report.append({'id': e['id'], 'ts': e['ts'], 'tool': e['tool'], 'action': e['action'],
                       'ok': all(o['status'] in ('ok', 'undone') for o in outs), 'outputs': outs})
    return report


def _kv(items: list[str]) -> dict:
    out = {}
    for item in items:
        key, sep, value = item.partition('=')
        if not sep:
            raise SystemExit(f'--arg needs key=value, got {item!r}')
        out[key] = value
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('command', choices=['list', 'verify', 'undo', 'record'])
    ap.add_argument('--ledger', required=True, type=Path)
    ap.add_argument('--id', help='entry id (undo)')
    ap.add_argument('--tool', help='record: tool name')
    ap.add_argument('--action', help='record: action name')
    ap.add_argument('--arg', action='append', default=[], help='record: key=value')
    ap.add_argument('--input', action='append', default=[], help='record: input path')
    ap.add_argument('--output', action='append', default=[], help='record: output path')
    args = ap.parse_args(argv)

    def emit(value):
        json.dump(value, sys.stdout, ensure_ascii=False, indent=1)
        print()

    if args.command == 'list':
        skipped: list[int] = []
        rows = entries(args.ledger, skipped)
        emit({'entries': rows, 'skipped_lines': skipped})
        return 0
    if args.command == 'verify':
        rows = verify(args.ledger)
        emit({'all_ok': all(r['ok'] for r in rows), 'entries': rows})
        return 0 if all(r['ok'] for r in rows) else 1
    if args.command == 'undo':
        if not args.id:
            ap.error('undo needs --id')
        try:
            emit(undo(args.ledger, args.id))
            return 0
        except UndoRefused as err:
            emit({'refused': err.entry_id, 'problems': err.problems})
            return 1
    if not (args.tool and args.action):
        ap.error('record needs --tool and --action')
    emit(record(args.ledger, args.tool, args.action, _kv(args.arg), args.input, args.output))
    return 0


if __name__ == '__main__':
    sys.exit(main())
