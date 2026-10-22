#!/usr/bin/env python3
"""Wait for a fresh Final Cut "Export XML" (Cmd-E) by content, not mtime.

A candidate (`--pattern`, default `*.fcpxml*`, top level of `--dir`) is a
.fcpxml file or a .fcpxmld bundle; bundles are digested whole (every file,
sorted). The first run records a baseline digest per candidate (saved in
`--since-state`); a candidate is fresh when its digest differs from the
baseline (new or changed). It is accepted only once the same digest was
seen in two polls at least `--settle` (1 s) apart, so a half-written export
is never returned. When several are fresh, the newest by mtime wins (not
the last by path), and if that newest one is still changing we keep
waiting for it instead of returning an older settled one. The accepted digest becomes the new baseline
for that path, so the next run waits for the next export.

`--against BUILD.fcpxml` then diffs the export against your own build with
timeline_diff (what the editor changed), `--diff-out` keeps the full report.

`--once`: no waiting for an export; report what changed since the state
(still checking that each change has settled).

Output: one JSON object. Exit 0 found, 2 nothing (timeout / --once with no
settled change), 3 the diff failed.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .ledger import digest_path

STATE_VERSION = 'watch-export-v1'


def newest_mtime(path: Path) -> float:
    """Latest mtime of the path or anything inside it (bundles)."""
    best = path.stat().st_mtime
    if path.is_dir():
        for sub in path.rglob('*'):
            try:
                best = max(best, sub.stat().st_mtime)
            except OSError:
                pass
    return best


def scan(directory: Path, pattern: str) -> dict[str, dict]:
    """{abs path: {'digest', 'mtime'}} for each candidate; digest None if unreadable now."""
    out = {}
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return out
    for name in names:
        if name.startswith('.') or not fnmatch.fnmatch(name, pattern):
            continue
        path = directory / name
        try:
            out[str(path)] = {'digest': digest_path(path), 'mtime': newest_mtime(path)}
        except OSError:  # vanished or mid-write
            out[str(path)] = {'digest': None, 'mtime': 0.0}
    return out


def baseline_of(snapshot: dict[str, dict]) -> dict[str, str | None]:
    return {p: v['digest'] for p, v in snapshot.items()}


class Tracker:
    """Remembers since when each fresh candidate has shown its current digest."""

    def __init__(self, baseline: dict[str, str | None], settle: float = 1.0):
        self.baseline = dict(baseline)
        self.settle = settle
        self.seen: dict[str, tuple[str, float, int]] = {}  # path -> (digest, first_seen, polls)

    def observe(self, snapshot: dict[str, dict], now: float) -> list[dict]:
        """Fresh candidates with their settled flag, newest (mtime) first."""
        rows = []
        for path, info in snapshot.items():
            digest = info['digest']
            if digest is None or digest == self.baseline.get(path):
                self.seen.pop(path, None)
                continue
            prev = self.seen.get(path)
            if prev and prev[0] == digest:
                self.seen[path] = (digest, prev[1], prev[2] + 1)
            else:
                self.seen[path] = (digest, now, 1)  # new bytes: the clock restarts
            _, first, polls = self.seen[path]
            rows.append({'path': path, 'digest': digest, 'mtime': info['mtime'],
                         'new': path not in self.baseline,
                         'settled': polls >= 2 and now - first >= self.settle})
        rows.sort(key=lambda r: (-r['mtime'], r['path']))
        return rows


def wait_for_export(directory: Path, pattern: str, baseline: dict, timeout: float = 1800,
                    interval: float = 2.0, settle: float = 1.0, once: bool = False,
                    clock=time.monotonic, sleep=time.sleep) -> tuple[dict | None, list[dict]]:
    """(accepted candidate or None, last fresh rows). `once` = at most two polls."""
    tracker = Tracker(baseline, settle)
    start = clock()
    rows: list[dict] = []
    polls = 0
    while True:
        now = clock()
        rows = tracker.observe(scan(directory, pattern), now)
        polls += 1
        # The newest fresh candidate decides: if it is still being written,
        # wait for it rather than return an older, settled export.
        if rows and rows[0]['settled']:
            return rows[0], rows
        if once and (not rows or polls >= 2):
            return None, rows
        if not once and now - start >= timeout:
            return None, rows
        sleep(settle if once else interval)


def load_state(path: Path | None, directory: Path, pattern: str) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except ValueError:
        return None
    if state.get('version') != STATE_VERSION or state.get('dir') != str(directory) or state.get('pattern') != pattern:
        return None
    return state


def save_state(path: Path | None, directory: Path, pattern: str, baseline: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {'version': STATE_VERSION, 'dir': str(directory), 'pattern': pattern,
            'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'baseline': baseline}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    with os.fdopen(fd, 'w') as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dir', required=True, type=Path)
    ap.add_argument('--pattern', default='*.fcpxml*')
    ap.add_argument('--since-state', type=Path, help='baseline state file (read and advanced)')
    ap.add_argument('--timeout', type=float, default=1800)
    ap.add_argument('--interval', type=float, default=2.0)
    ap.add_argument('--settle', type=float, default=1.0, help='min seconds a digest must hold (>=1)')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--against', type=Path, help='your build XML: diff the export against it (fcpkit-diff)')
    ap.add_argument('--diff-out', type=Path, help='write the full timeline_diff report here')
    args = ap.parse_args(argv)
    directory = Path(os.path.abspath(args.dir.expanduser()))
    settle = max(1.0, args.settle)

    state = load_state(args.since_state, directory, args.pattern)
    if state is None:
        baseline = baseline_of(scan(directory, args.pattern))
        save_state(args.since_state, directory, args.pattern, baseline)
        if args.once:
            print(json.dumps({'found': None, 'baseline_recorded': len(baseline), 'dir': str(directory),
                              'state': str(args.since_state) if args.since_state else None},
                             ensure_ascii=False))
            return 2
    else:
        baseline = state['baseline']

    found, rows = wait_for_export(directory, args.pattern, baseline, args.timeout,
                                  args.interval, settle, once=args.once)
    result = {'found': found and found['path'], 'digest': found and found['digest'],
              'new': found and found['new'], 'fresh': rows, 'dir': str(directory)}
    if found is None:
        result['timeout'] = not args.once
        print(json.dumps(result, ensure_ascii=False))
        return 2
    baseline[found['path']] = found['digest']
    save_state(args.since_state, directory, args.pattern, baseline)
    code = 0
    if args.against:
        try:
            from . import timeline_diff
            rep = timeline_diff.diff(args.against, found['path'])
            if args.diff_out:
                args.diff_out.write_text(json.dumps(rep, ensure_ascii=False, indent=1))
            result['diff'] = {'against': str(args.against), 'summary': rep['summary'],
                              'duration': [rep['duration_a'], rep['duration_b']],
                              'report': str(args.diff_out) if args.diff_out else None}
        except Exception as err:
            result['diff'] = {'error': f'{type(err).__name__}: {err}'}
            code = 3
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
