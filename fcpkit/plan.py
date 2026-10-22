#!/usr/bin/env python3
"""Approval bound to one exact job: a plan id the live step must match.

Before a live step (import into FCP, relink, overwrite-free export) we write
a plan: the action, its parameters (typically the XML's sha256, the target
library path, the event name, the app path) and a validity window. The plan
id is the sha256 of a canonical JSON of all of that. The user approves the
id shown in the summary; the live step calls `check_plan` with the
parameters it is about to use, which recomputes the id and refuses when
anything differs (a rebuilt XML, another library, another event name), when
the plan file was edited, or when the window expired.

The window is inside the hash, so editing `expires_at` in the file breaks
the id. The XML digest is bundle-aware (every file of a .fcpxmld).

    make_plan(action, params, ttl_minutes=120) -> plan
    check_plan(plan, action, params, plan_id=None) -> None  (raises PlanError)

CLI:
    fcpkit-plan make  --action import --xml X --param key=value ... --out plan.json
    fcpkit-plan check --plan plan.json --xml X --param key=value ... [--id ID]   (exit 1 on refusal)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ledger import digest_path

PLAN_VERSION = 'fcpkit-plan-v1'


class PlanError(ValueError):
    """Plan refused: mismatch, tampering or expiry."""


class PlanExpired(PlanError):
    pass


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _canonical(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _plan_id(action: str, params: dict, created_at: str, expires_at: str) -> str:
    body = {'version': PLAN_VERSION, 'action': action, 'params': params,
            'created_at': created_at, 'expires_at': expires_at}
    return hashlib.sha256(_canonical(body)).hexdigest()


def xml_params(xml) -> dict:
    """Parameters that pin an FCPXML file/bundle: absolute path + whole-content sha256."""
    path = Path(os.path.abspath(Path(xml).expanduser()))
    sha = digest_path(path)
    if sha is None:
        raise FileNotFoundError(path)
    return {'xml': str(path), 'xml_sha256': sha}


def make_plan(action: str, params: dict, ttl_minutes: float = 120, now: datetime | None = None) -> dict:
    """A plan dict with `plan_id`; params must be JSON-serialisable."""
    created = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    created_at, expires_at = _iso(created), _iso(created + timedelta(minutes=ttl_minutes))
    params = json.loads(_canonical(params))  # normalise (tuples -> lists etc.)
    return {'version': PLAN_VERSION, 'plan_id': _plan_id(action, params, created_at, expires_at),
            'action': action, 'params': params, 'created_at': created_at, 'expires_at': expires_at}


def check_plan(plan: dict, action: str, params: dict, plan_id: str | None = None,
               now: datetime | None = None) -> None:
    """Raise PlanError unless (action, params) is exactly the approved, unexpired plan."""
    try:
        stored = _plan_id(plan['action'], plan['params'], plan['created_at'], plan['expires_at'])
    except (KeyError, TypeError) as err:
        raise PlanError(f'malformed plan: {err}') from err
    if plan.get('version') != PLAN_VERSION:
        raise PlanError(f'unknown plan version {plan.get("version")!r}')
    if stored != plan.get('plan_id'):
        raise PlanError('plan file was edited: its id does not match its contents')
    if plan_id is not None and plan_id != plan['plan_id']:
        raise PlanError(f'approved id {plan_id} is not this plan ({plan["plan_id"]})')
    params = json.loads(_canonical(params))
    live = _plan_id(action, params, plan['created_at'], plan['expires_at'])
    if live != plan['plan_id']:
        diff = []
        if action != plan['action']:
            diff.append(f'action: planned {plan["action"]!r}, now {action!r}')
        for key in sorted(set(plan['params']) | set(params)):
            if plan['params'].get(key) != params.get(key):
                diff.append(f'{key}: planned {plan["params"].get(key)!r}, now {params.get(key)!r}')
        raise PlanError('live step differs from the approved plan: ' + '; '.join(diff))
    current = now or datetime.now(timezone.utc)
    if current >= _parse_iso(plan['expires_at']):
        raise PlanExpired(f'plan expired at {plan["expires_at"]}; make a new one and get it approved')
    if current < _parse_iso(plan['created_at']) - timedelta(minutes=5):
        raise PlanError('plan is dated in the future; clock or file is wrong')


def summary(plan: dict) -> str:
    lines = [f'Plan {plan["plan_id"][:12]}  (full id {plan["plan_id"]})',
             f'  action:  {plan["action"]}']
    for key in sorted(plan['params']):
        lines.append(f'  {key}: {plan["params"][key]}')
    lines.append(f'  valid:   {plan["created_at"]} .. {plan["expires_at"]}')
    return '\n'.join(lines)


def _params(args) -> dict:
    params = {}
    for item in args.param:
        key, sep, value = item.partition('=')
        if not sep or not key:
            raise SystemExit(f'--param needs key=value, got {item!r}')
        params[key] = value
    if args.xml:
        clash = {'xml', 'xml_sha256'} & set(params)
        if clash:
            raise SystemExit(f'--param {sorted(clash)} is set by --xml')
        params.update(xml_params(args.xml))
    return params


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='command', required=True)
    mk = sub.add_parser('make', help='write a plan and print its id + summary')
    mk.add_argument('--action', required=True)
    mk.add_argument('--ttl-minutes', type=float, default=120)
    mk.add_argument('--out', required=True, type=Path)
    ck = sub.add_parser('check', help='exit 1 unless the live parameters match the plan')
    ck.add_argument('--plan', required=True, type=Path)
    ck.add_argument('--action', help='defaults to the plan action')
    ck.add_argument('--id', help='the id the user approved')
    for p in (mk, ck):
        p.add_argument('--xml', help='.fcpxml or .fcpxmld; adds xml + xml_sha256')
        p.add_argument('--param', action='append', default=[], help='key=value (repeat)')
    args = ap.parse_args(argv)

    if args.command == 'make':
        plan = make_plan(args.action, _params(args), args.ttl_minutes)
        if args.out.exists():
            raise SystemExit(f'{args.out} exists; plans are never overwritten')
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + '\n')
        print(json.dumps({'plan_id': plan['plan_id'], 'out': str(args.out)}, ensure_ascii=False))
        print(summary(plan))
        return 0

    plan = json.loads(args.plan.read_text())
    try:
        check_plan(plan, args.action or plan.get('action'), _params(args), args.id)
    except PlanError as err:
        print(json.dumps({'ok': False, 'plan_id': plan.get('plan_id'), 'error': str(err),
                          'expired': isinstance(err, PlanExpired)}, ensure_ascii=False))
        return 1
    print(json.dumps({'ok': True, 'plan_id': plan['plan_id'], 'action': plan['action']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
