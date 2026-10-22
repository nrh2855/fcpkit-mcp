#!/usr/bin/env python3
"""Import an FCPXML into a chosen, already-open FCP library — guarded, verified, with a receipt.

FCP picks the target library from `<import-options><option key="library
location" …/>` (first child of <fcpxml>). Without it a flat .fcpxml imports
nothing silently and a .fcpxmld opens a blocking "Which library?" picker; with
a location that does not exist FCP silently CREATES a new library. So:

- `make_import_copy(xml, library_path, out=None)` (offline) writes a NEW
  sibling copy (`<stem>_import.fcpxml`, or a copied `<stem>_import.fcpxmld`
  with its Info.fcpxml edited) whose first child is
      <import-options>
        <option key="library location" value="file://<percent-encoded path>"/>
        <option key="copy assets" value="0"/>        (media stays where it is)
        <option key="suppress warnings" value="0"/>  (import warnings must be seen)
      </import-options>
  replacing any existing one. Lossless envelope I/O (fcpxml_io.py),
  DTD-validated, exclusive create, original untouched.
- `import_xml(...)` (live) runs every check below BEFORE any Apple event,
  then opens the copy with the running FCP (`open -a <full .app path>`), polls
  the library listing every 2 s until every project of the XML is in the
  target library under the expected (new) event, records FCP dialogs via an
  optional SpliceKit bridge (dialog.detect; never clicks) and writes a JSON
  receipt (status, verdict PASS/NOTCHECKED/FAIL, warnings, note).

Checks (refuse on any fail):
  lock      lock file (--lock / $FCPKIT_LOCK) absent; held while running, then renamed
            <lock>.released-<YYYYmmdd-HHMM> (kept as a record, never deleted)
  plan      --plan/--plan-id match plan.check_plan('import', {xml, xml_sha256, library, event, app})
  volumes   media under /Volumes/<name> → that volume is mounted (external drives disconnect)
  app       exactly one FCP running (never launched unless --allow-launch); it is the planned app
  library   target library is OPEN in FCP (path match: case-, slash-, NFC/NFD-insensitive),
            unless --create-library (then it must not exist on disk)
  quiet     no open library modified in the last --quiet-minutes (the user may be editing)
  event     no event with the XML's event name in the target library (FCP "Invalid argument";
            every fix goes into a NEW event); <event uid> in the XML → warning
  dialogs   no FCP dialog open before the import (bridge up)
  copy      the import copy does not exist yet

CLI:
  fcpkit-import X.fcpxmld --library L.fcpbundle --dry-run [--out COPY]
  fcpkit-import X.fcpxmld --library L.fcpbundle --plan plan.json --plan-id ID --receipt R.json
      [--quiet-minutes 10] [--timeout 120] [--create-library] [--allow-launch] [--app APP] [--lock PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from . import config, splicekit
from . import fcp_libraries as FL
from . import fcpxml_io as IO

ACTION = 'import'
OWNER = 'fcpkit.fcp_import_xml'
# FCP dialog text that means a problem; the second line matches FCP's Korean localisation
WARN_RE = re.compile(r'encountered|unexpected|invalid|missing|error|warning|could not|unable|not supported|'
                     r'문제|오류|경고|없습니다|찾을 수', re.I)


class ImportRefused(RuntimeError):
    def __init__(self, message: str, checks: list | None = None):
        super().__init__(message)
        self.checks = checks or []


# ---------------------------------------------------------------- offline XML side

def xml_file(xml) -> Path:
    p = Path(os.path.abspath(Path(xml).expanduser()))
    return p / 'Info.fcpxml' if p.suffix == '.fcpxmld' or p.is_dir() else p


def default_copy_path(xml) -> Path:
    p = Path(os.path.abspath(Path(xml).expanduser()))
    return p.with_name(p.stem + '_import' + p.suffix)


def library_url(path) -> str:
    """file:// URL of a library path, UTF-8 percent-encoded, path kept as given (no trailing slash)."""
    text = os.path.abspath(os.path.expanduser(str(path))).rstrip('/')
    return 'file://' + quote(text, safe='/')


def inject_import_options(tree: ET.ElementTree, url: str) -> bool:
    """Put a fresh <import-options> first under <fcpxml>; True if an old one was replaced."""
    root = tree.getroot()
    if root.tag != 'fcpxml':
        raise IO.FCPXMLEnvelopeError(f'root is <{root.tag}>, not <fcpxml>')
    indent = root.text if root.text and not root.text.strip() else '\n    '
    old = [el for el in root if el.tag == 'import-options']
    tail = old[0].tail if old and root[0] is old[0] and old[0].tail else indent
    for el in old:
        root.remove(el)
    inner = indent + '    '
    opts = ET.Element('import-options')
    opts.text = inner
    for key, value in (('library location', url), ('copy assets', '0'), ('suppress warnings', '0')):
        ET.SubElement(opts, 'option', {'key': key, 'value': value}).tail = inner
    opts[-1].tail = indent
    opts.tail = tail
    if root.text is None or root.text.strip():
        root.text = indent
    root.insert(0, opts)
    return bool(old)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(path: Path) -> str | None:
    from .ledger import digest_path
    return digest_path(path)


def make_import_copy(xml, library_path, out=None) -> dict:
    """Write the import copy (never touching `xml`); returns paths, hashes, DTD result."""
    src = Path(os.path.abspath(Path(xml).expanduser()))
    is_bundle = src.is_dir()
    info = xml_file(src)
    out = Path(os.path.abspath(Path(out).expanduser())) if out else default_copy_path(src)
    if out.exists() or out.is_symlink():
        raise FileExistsError(f'import copy already exists: {out} (never overwritten; pass another --out)')
    if is_bundle != (out.suffix == '.fcpxmld'):
        raise ValueError(f'copy {out.name} must keep the source kind ({src.suffix})')
    before = _digest(src)
    tree, envelope = IO.parse_fcpxml(info)
    url = library_url(library_path)
    replaced = inject_import_options(tree, url)
    rendered = IO.render_fcpxml(tree, envelope)
    if is_bundle:
        out.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(out)  # exclusive claim of the bundle folder
        try:
            for entry in src.iterdir():
                if entry.name == 'Info.fcpxml':
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.copytree(entry, out / entry.name, symlinks=True)
                else:
                    shutil.copy2(entry, out / entry.name, follow_symlinks=False)
            pub = IO.publish_fcpxml(tree, out / 'Info.fcpxml', rendered)
        except BaseException:
            shutil.rmtree(out, ignore_errors=True)  # only the folder this call created
            raise
        copy_info = out / 'Info.fcpxml'
    else:
        pub = IO.publish_fcpxml(tree, out, rendered)
        copy_info = out
    after = _digest(src)
    if before != after:
        raise RuntimeError(f'source changed while copying: {src}')
    return {'source': str(src), 'source_sha256': before, 'source_xml_sha256': _sha(info),
            'copy': str(out), 'copy_xml': str(copy_info), 'copy_sha256': _digest(out),
            'copy_xml_sha256': pub['output_sha256'], 'library_url': url,
            'replaced_import_options': replaced, 'dtd_validation': pub['dtd_validation']}


def _url_path(url: str) -> str | None:
    if not url or not url.startswith('file:'):
        return None
    return unquote(urlparse(url).path)


def media_volumes(media) -> dict[str, int]:
    """{'/Volumes/<name>': count} for media paths on mounted-volume roots."""
    out: dict[str, int] = {}
    for m in media:
        parts = Path(m).parts
        if len(parts) >= 3 and parts[1] == 'Volumes':
            vol = '/Volumes/' + parts[2]
            out[vol] = out.get(vol, 0) + 1
    return out


def xml_facts(xml) -> dict:
    """Events, projects, uids and media paths named by the XML (read only)."""
    tree, _ = IO.parse_fcpxml(xml_file(xml))
    root = tree.getroot()
    events = []
    for ev in root.iter('event'):
        events.append({'name': FL.norm_name(ev.get('name', '')), 'uid': ev.get('uid'),
                       'projects': [FL.norm_name(p.get('name', '')) for p in ev.iter('project')]})
    in_events = {id(p) for ev in root.iter('event') for p in ev.iter('project')}
    loose = [FL.norm_name(p.get('name', '')) for p in root.iter('project') if id(p) not in in_events]
    media = sorted({p for el in root.iter() if el.tag in ('media-rep', 'asset')
                    for p in [_url_path(el.get('src', ''))] if p})
    libs = [lib.get('location') for lib in root.iter('library') if lib.get('location')]
    old = [dict(o.attrib) for io_ in root.findall('import-options') for o in io_.findall('option')]
    return {'version': root.get('version'), 'events': events, 'loose_projects': loose, 'media': media,
            'library_elements': libs, 'import_options': old}


def expected_event(facts: dict) -> str:
    return ' | '.join(e['name'] for e in facts['events'])


def plan_params(xml, library, event: str, app) -> dict:
    """The exact params the plan must be made with (fcpkit-plan make --action import --xml X
    --param library=… --param event=… --param app=…)."""
    from . import plan as PL
    p = PL.xml_params(xml)
    p.update({'library': unicodedata.normalize('NFC', os.path.abspath(os.path.expanduser(str(library)))).rstrip('/'),
              'event': event, 'app': str(app)})
    return p


# ---------------------------------------------------------------- checks

def _row(rows, check, status, detail=''):
    rows.append({'check': check, 'status': status, 'detail': detail})
    return status


def check_event_free(snap, library_path, event_names) -> tuple[str, str]:
    """('fail'|'pass'|'todo', detail) — an event with this name already in the target library?"""
    lib = FL.find_library(snap, library_path) if snap is not None else None
    if lib is None:
        return 'todo', 'target library not in the FCP listing (checked live)'
    clash = [n for n in event_names if FL.find_events(lib, n)]
    if clash:
        return 'fail', (f'event {clash} already exists in {lib["name"]} — reusing an event name/uid makes FCP fail '
                        '("Invalid argument"); build the fix into a NEW event name')
    return 'pass', f'no event named {event_names} in {lib["name"]}'


def check_quiet(paths, quiet_minutes: float, now: float | None = None) -> tuple[str, str]:
    now = time.time() if now is None else now
    recent, unknown = [], []
    for p in paths:
        m = FL.library_mtime(p)
        if m is None:
            unknown.append(str(p))
        elif (now - m) / 60 < quiet_minutes:
            recent.append(f'{os.path.basename(str(p).rstrip("/"))} {(now - m) / 60:.1f} min ago')
    if recent:
        return 'fail', 'modified recently — the user may be editing: ' + '; '.join(recent)
    if unknown and len(unknown) == len(list(paths)):
        return 'todo', 'library not on disk here: ' + '; '.join(unknown)
    return 'pass', f'no library modified in the last {quiet_minutes:g} min' + (
        f' (not on disk: {unknown})' if unknown else '')


def offline_checks(xml, library, *, facts, lock_path, plan_path, plan_id, app, out, create_library,
                   quiet_minutes, dry_run, running=None) -> list[dict]:
    rows: list[dict] = []
    events = [e['name'] for e in facts['events']]
    if not events:
        _row(rows, 'event', 'warn', 'XML has no <event>; FCP chooses the event — receipt matches projects only')
    if len(events) != len(set(events)):
        _row(rows, 'event', 'fail', f'duplicate event names in the XML: {events}')
    uids = [e['name'] for e in facts['events'] if e['uid']]
    if uids:
        _row(rows, 'event-uid', 'warn', f'<event uid> present for {uids}: a reused uid merges into an existing '
                                        'event (Invalid argument); prefer no uid')
    for loc in facts['library_elements']:
        p = _url_path(loc) or loc
        same = FL.same_path(p, library)
        _row(rows, 'library-element', 'warn',
             f'XML also has <library location="{FL.norm_name(p)}"> — '
             + ('same library as the target' if same else
                'different from the target; import-options wins (verified live on FCP 12.3)'))
    if facts['import_options']:
        _row(rows, 'import-options', 'warn', f'existing import-options replaced: {facts["import_options"]}')
    projects = [p for e in facts['events'] for p in e['projects']] + facts['loose_projects']
    if not projects:
        _row(rows, 'projects', 'fail', 'XML has no <project>: nothing to verify after import')
    for vol, count in sorted(media_volumes(facts['media']).items()):
        mounted = os.path.ismount(vol)
        _row(rows, 'volume', 'pass' if mounted else 'fail',
             f'{count} media on {vol}; ' + ('mounted' if mounted else 'NOT mounted (ls /Volumes)'))
    missing = [m for m in facts['media'] if not os.path.exists(m)]
    if missing:
        _row(rows, 'media', 'warn', f'{len(missing)} media files not found, e.g. {missing[:3]}')
    lock = Path(lock_path)
    if lock.exists():
        _row(rows, 'lock', 'fail', f'{lock} is held: {lock.read_text(errors="replace")[:200]}')
    else:
        _row(rows, 'lock', 'pass', f'{lock} absent')
    copy = Path(out) if out else default_copy_path(xml)
    _row(rows, 'copy', 'fail' if copy.exists() else 'pass', f'{copy}' + (' already exists' if copy.exists() else ''))
    on_disk = os.path.isdir(os.path.expanduser(str(library)))
    if create_library:
        _row(rows, 'library', 'fail' if on_disk else 'pass',
             'exists on disk — --create-library is only for a new library' if on_disk
             else 'will be CREATED by FCP (--create-library)')
    else:
        _row(rows, 'library-disk', 'pass' if on_disk else ('todo' if dry_run else 'warn'),
             'on disk' if on_disk else f'{library} not on disk here (must be open in FCP)')
        if on_disk:
            names = FL.disk_event_names(library) or []
            clash = [n for n in events if n in names]
            _row(rows, 'event-disk', 'fail' if clash else 'pass',
                 f'event folder {clash} already in the library' if clash else 'no event folder with that name')
            _row(rows, 'quiet-target', *check_quiet([library], quiet_minutes))
    # plan
    if plan_path and plan_id:
        try:
            from . import plan as PL
            planned = json.loads(Path(plan_path).read_text())
            PL.check_plan(planned, ACTION, plan_params(xml, library, expected_event(facts), app), plan_id)
            _row(rows, 'plan', 'pass', f'plan {plan_id[:12]} matches')
        except Exception as err:  # PlanError, missing file, bad JSON
            _row(rows, 'plan', 'fail', f'{type(err).__name__}: {err}')
    else:
        _row(rows, 'plan', 'todo' if dry_run else 'fail', 'no --plan/--plan-id: make one with fcpkit-plan and get it approved')
    # app (process table only — no Apple event)
    try:
        inst = running if running is not None else FL.running_fcp()
    except FL.FCPAmbiguous as err:
        _row(rows, 'app', 'fail', str(err))
    else:
        if not inst:
            _row(rows, 'app', 'todo' if dry_run else 'fail', 'no Final Cut Pro running (never launched implicitly)')
        elif not FL.same_path(inst['path'], app):
            _row(rows, 'app', 'fail', f"running FCP is {inst['path']} (pid {inst['pid']}), planned app is {app}")
        else:
            _row(rows, 'app', 'pass', f"{inst['path']} pid {inst['pid']} FCP {inst['version']}")
    return rows


def failing(rows, statuses=('fail',)) -> list[dict]:
    return [r for r in rows if r['status'] in statuses]


# ---------------------------------------------------------------- lock

def take_lock(lock_path, what: str, events, stamp: str) -> dict:
    body = {'owner': OWNER, 'what': what, 'events': list(events), 'since': stamp, 'pid': os.getpid()}
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, 'w') as fh:
        json.dump(body, fh, ensure_ascii=False)
    return body


def release_lock(lock_path) -> str | None:
    lock = Path(lock_path)
    try:
        body = json.loads(lock.read_text())
    except (OSError, ValueError):
        return None
    if body.get('pid') != os.getpid() or body.get('owner') != OWNER:
        return None  # not ours: leave it
    dest = Path(f'{lock}.released-{datetime.now():%Y%m%d-%H%M}')
    if dest.exists():
        dest = Path(f'{lock}.released-{datetime.now():%Y%m%d-%H%M%S}')
    os.rename(lock, dest)
    return str(dest)


# ---------------------------------------------------------------- SpliceKit dialogs (read only)

def bridge_up(port: int | None = None) -> bool:
    return splicekit.bridge_up(port)


def detect_dialogs() -> list[dict]:
    r = splicekit.call('dialog.detect', {}, 10).get('result', {}) or {}
    return [d for d in r.get('dialogs', []) if d.get('class') != 'FFOSCOverlayWindow']


def dialog_text(d: dict) -> str:
    parts = [d.get('title') or ''] + [str(x) for x in d.get('labels', []) or []] + \
            [str(x) for x in d.get('textFields', []) or []]
    return ' | '.join(p for p in parts if p)


# ---------------------------------------------------------------- live import

def _expected_found(snap, library, facts, before_ids: set) -> list[dict] | None:
    """Rows for every expected project, or None while any is still missing."""
    lib = FL.find_library(snap, library)
    if lib is None:
        return None
    rows = []
    if facts['events']:
        for ev_fact in facts['events']:
            evs = [e for e in FL.find_events(lib, ev_fact['name']) if _ident(e) not in before_ids]
            if not evs:
                return None
            for name in ev_fact['projects']:
                hits = [p for e in evs for p in e['projects'] if FL.norm_name(p['name']) == name]
                if not hits:
                    return None
                rows += [{'event': evs[0]['name'], 'event_persistent_id': evs[0].get('persistent_id'), **h}
                         for h in hits[:1]]
    for name in facts['loose_projects']:
        hits = [(e, p) for e in lib['events'] for p in e['projects']
                if FL.norm_name(p['name']) == name and _ident(p) not in before_ids]
        if not hits:
            return None
        e, p = hits[0]
        rows.append({'event': e['name'], 'event_persistent_id': e.get('persistent_id'), **p})
    return rows


def _ident(item: dict) -> str | None:
    """FCP object identity. FCP 12.3 returns `persistent ID` empty (live test) but a UUID `id`;
    an empty value is no identity (it would match every object)."""
    return item.get('id') or item.get('persistent_id') or None


def _ids(snap) -> set:
    out = set()
    for lib in FL._libs(snap):
        for ev in lib['events']:
            out.add(_ident(ev))
            out.update(_ident(p) for p in ev['projects'])
    out.discard(None)
    return out


def import_xml(xml, library, *, plan_path=None, plan_id=None, receipt=None, out=None, quiet_minutes=10.0,
               timeout=120.0, settle=8.0, create_library=False, allow_launch=False,
               app=None, lock_path=None, dry_run=False,
               lister=None, opener=None, dialogs=None, running=None, poll=2.0) -> dict:
    """Dry run: offline checks + the copy + the live plan. Live: the guarded import (see module doc).

    `app` defaults to config.fcp_app() ($FCPKIT_FCP_APP), `lock_path` to config.lock_path()."""
    app = str(app or config.fcp_app())
    lock_path = Path(lock_path) if lock_path else config.lock_path()
    t0 = time.time()
    facts = xml_facts(xml)
    events = [e['name'] for e in facts['events']]
    projects = [p for e in facts['events'] for p in e['projects']] + facts['loose_projects']
    rows = offline_checks(xml, library, facts=facts, lock_path=lock_path, plan_path=plan_path, plan_id=plan_id,
                          app=app, out=out, create_library=create_library, quiet_minutes=quiet_minutes,
                          dry_run=dry_run, running=running)
    rec = {'tool': OWNER, 'xml': str(Path(xml).absolute()), 'library': str(library), 'event': expected_event(facts),
           'expected_projects': projects, 'app': app, 'dry_run': dry_run, 'checks': rows,
           'started': datetime.fromtimestamp(t0).isoformat(timespec='seconds')}
    if dry_run:
        copy = None
        if not any(r['check'] == 'copy' and r['status'] == 'fail' for r in rows):
            copy = make_import_copy(xml, library, out)
        rec.update({'status': 'dry-run', 'copy': copy, 'would_refuse': failing(rows),
                    'todo_before_live': failing(rows, ('todo',)),
                    'live_steps': live_steps(xml, library, facts, app, copy, lock_path, quiet_minutes, timeout),
                    'plan_command': plan_command(xml, library, expected_event(facts), app)})
        return _write_receipt(rec, receipt)
    bad = failing(rows)
    if bad and not (allow_launch and all(r['check'] == 'app' and 'no Final Cut Pro' in r['detail'] for r in bad)):
        rec.update({'status': 'refused', 'verdict': 'FAIL', 'note': '; '.join(f"{r['check']}: {r['detail']}" for r in bad)})
        _write_receipt(rec, receipt)
        raise ImportRefused(rec['note'], rows)

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    take_lock(lock_path, f'import {Path(xml).name} into {library}', events, stamp)
    rec['lock'] = {'path': str(lock_path), 'since': stamp}
    seen: dict[str, dict] = {}
    try:
        inst = running or FL.running_fcp()
        if not inst:
            subprocess.run(['/usr/bin/open', '-a', app], check=True)  # --allow-launch only (checked above)
            deadline = time.time() + 120
            while not inst and time.time() < deadline:
                time.sleep(2)
                inst = FL.running_fcp()
            if not inst:
                raise ImportRefused(f'launched {app} but no FCP process appeared')
        if not FL.same_path(inst['path'], app):
            raise ImportRefused(f"running FCP is {inst['path']}, planned {app}")
        rec['pid'] = inst['pid']
        lister = lister or (lambda durations=True: FL.snapshot(app, timeout=20, durations=durations, running=inst))
        watch = dialogs if dialogs is not None else (detect_dialogs if inst.get('splicekit') and bridge_up() else None)
        rec['dialogs_watched'] = watch is not None
        before = lister(durations=True)
        lib = FL.find_library(before, library)
        live = []
        if lib is None and not create_library:
            open_paths = [l.get('path') for l in FL._libs(before)]
            _row(live, 'library-open', 'fail', f'{library} is not open in FCP (open: {open_paths}); a location that '
                                                'is not open makes FCP create a NEW library')
        elif lib is not None and create_library:
            _row(live, 'library-open', 'fail', 'library is already open — drop --create-library')
        else:
            _row(live, 'library-open', 'pass', lib['path'] if lib else 'new library')
        _row(live, 'quiet', *check_quiet([l['path'] for l in FL._libs(before) if l.get('path')], quiet_minutes))
        if events and lib is not None:
            _row(live, 'event', *check_event_free(before, library, events))
        if watch:
            open_now = [dialog_text(d) for d in watch()]
            _row(live, 'dialogs', 'fail' if open_now else 'pass', f'dialog open: {open_now}' if open_now else 'none open')
        rows += live
        bad = failing(live, ('fail', 'todo'))
        if bad:
            raise ImportRefused('; '.join(f"{r['check']}: {r['detail']}" for r in bad), rows)
        target = lib['path'] if lib else library  # FCP's own spelling of the path
        copy = make_import_copy(xml, target, out)
        rec['copy'] = copy
        if ((running if running is not None else FL.running_fcp()) or {}).get('pid') != inst['pid']:
            raise ImportRefused('the FCP process changed before open')
        (opener or (lambda c: subprocess.run(['/usr/bin/open', '-a', inst['path'], c], check=True)))(copy['copy'])
        t_open, found, errors, found_at = time.time(), None, [], None
        before_ids = _ids(before)
        while time.time() - t_open < timeout:
            if watch:
                try:
                    for d in watch():
                        k = dialog_text(d)
                        if k not in seen:
                            seen[k] = {'first_seen_s': round(time.time() - t_open, 1), 'class': d.get('class'),
                                       'title': d.get('title'), 'labels': d.get('labels'),
                                       'buttons': [b if isinstance(b, str) else b.get('title') for b in d.get('buttons', [])],
                                       'warning': bool(WARN_RE.search(k))}
                except (OSError, ValueError) as err:
                    errors.append(f'dialog.detect: {err}')
            if found is None:
                try:
                    found = _expected_found(lister(durations=False), target, facts, before_ids)
                    found_at = time.time() if found is not None else None
                except FL.FCPError as err:  # busy/modal while importing — keep polling
                    errors.append(str(err)[:200])
            elif time.time() - found_at >= settle:
                break
            time.sleep(poll)
        after = lister(durations=True)
        diff = FL.snapshot_diff(before, after)
        final = _expected_found(after, target, facts, before_ids)
        pre_existing = sorted({p['name'] for l in [FL.find_library(before, target) or {'events': []}]
                               for e in l['events'] for p in e['projects'] if FL.norm_name(p['name']) in projects})
        warn = [v for v in seen.values() if v['warning']]
        if final is None:
            status = 'timeout'
        elif pre_existing:
            status = 'not verified'
        else:
            status = 'verified'
        note = []
        if pre_existing:
            note.append(f'project names already in the library before: {pre_existing}')
        if not watch:
            note.append('FCP dialogs not watched (no SpliceKit bridge) — import warnings not checked')
        if status == 'timeout':
            note.append(f'expected projects not seen within {timeout:g}s')
        rec.update({'status': status, 'projects': final or [], 'diff': diff, 'dialogs': list(seen.values()),
                    'warnings': warn, 'poll_errors': errors[-20:],
                    'elapsed_s': round(time.time() - t0, 1), 'import_s': round(time.time() - t_open, 1),
                    'verdict': 'PASS' if status == 'verified' and not warn and watch else
                               ('NOTCHECKED' if status == 'verified' and not warn else 'FAIL'),
                    'note': '; '.join(note)})
    except ImportRefused as err:
        rec.update({'status': 'refused', 'verdict': 'FAIL', 'note': str(err), 'checks': rows,
                    'dialogs': list(seen.values())})
        _write_receipt(rec, receipt)
        raise
    except Exception as err:
        rec.update({'status': 'error', 'verdict': 'FAIL', 'note': f'{type(err).__name__}: {err}',
                    'dialogs': list(seen.values())})
        _write_receipt(rec, receipt)
        raise
    finally:
        rec['lock_released'] = release_lock(lock_path)
    return _write_receipt(rec, receipt)


def _write_receipt(rec: dict, receipt) -> dict:
    if receipt:
        path = Path(receipt)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + '\n')
    return rec


def plan_command(xml, library, event, app) -> str:
    import shlex
    lib = unicodedata.normalize('NFC', os.path.abspath(os.path.expanduser(str(library)))).rstrip('/')
    out = default_copy_path(xml).with_suffix('.plan.json')
    return ' '.join(shlex.quote(x) for x in [
        'fcpkit-plan', 'make', '--action', ACTION, '--xml', str(Path(xml).absolute()),
        '--param', f'library={lib}', '--param', f'event={event}', '--param', f'app={app}', '--out', str(out)])


def live_steps(xml, library, facts, app, copy, lock_path, quiet_minutes, timeout) -> list[str]:
    ev = expected_event(facts) or '(none in XML)'
    return [
        f'check the lock is absent ({lock_path}), media volumes mounted, plan id matches '
        f'(xml sha256, library, event {ev!r}, app)',
        f'process table: exactly one FCP running and it is {app} (never launched without --allow-launch)',
        'take the lock {owner, what, events, since, pid}',
        f'AppleScript listing of the running app by path (read only): {library} must be OPEN; '
        f'no open library modified in the last {quiet_minutes:g} min; no event named {ev!r} in it',
        'if a SpliceKit bridge is up → dialog.detect: no dialog open before the import',
        'rewrite the import copy with FCP\'s own spelling of the library path'
        + (f' (dry-run copy: {copy["copy"]})' if copy else ''),
        f'open -a "{app}" <copy>',
        f'poll the listing every 2 s (≤{timeout:g} s) until projects {facts_projects(facts)} are in a NEW event '
        f'{ev!r}; record every dialog (never click); settle 8 s for the warnings window',
        'final listing with durations; diff before/after; receipt JSON (status, verdict, warnings, note)',
        'rename the lock → <lock>.released-<YYYYmmdd-HHMM>',
    ]


def facts_projects(facts) -> list[str]:
    return [p for e in facts['events'] for p in e['projects']] + facts['loose_projects']


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('xml', help='.fcpxml or .fcpxmld (never modified)')
    ap.add_argument('--library', required=True, help='target .fcpbundle path (must be open in FCP)')
    ap.add_argument('--plan', help='plan JSON written by fcpkit-plan make --action import')
    ap.add_argument('--plan-id', help='the plan id the user approved')
    ap.add_argument('--receipt', help='receipt JSON path')
    ap.add_argument('--out', help='import copy path (default <stem>_import.<ext> next to the XML)')
    ap.add_argument('--quiet-minutes', type=float, default=10)
    ap.add_argument('--timeout', type=float, default=120)
    ap.add_argument('--app', help='FCP .app expected to be running (default $FCPKIT_FCP_APP or '
                                  '/Applications/Final Cut Pro.app)')
    ap.add_argument('--create-library', action='store_true', help='allow a library location FCP will create')
    ap.add_argument('--allow-launch', action='store_true', help='launch --app if no FCP is running')
    ap.add_argument('--lock', help='cooperative lock file (default $FCPKIT_LOCK or '
                                   '~/Library/Application Support/fcpkit/FCP.lock)')
    ap.add_argument('--dry-run', action='store_true', help='checks without FCP + write the copy + print the live steps')
    a = ap.parse_args(argv)
    if not a.dry_run and not a.receipt:
        ap.error('--receipt is required for a live import')
    try:
        rec = import_xml(a.xml, a.library, plan_path=a.plan, plan_id=a.plan_id, receipt=a.receipt, out=a.out,
                         quiet_minutes=a.quiet_minutes, timeout=a.timeout, create_library=a.create_library,
                         allow_launch=a.allow_launch, app=a.app, lock_path=a.lock, dry_run=a.dry_run)
    except ImportRefused as err:
        print(f'REFUSED: {err}', file=sys.stderr)
        return 1
    except (FL.FCPError, IO.FCPXMLEnvelopeError, FileExistsError, OSError, ValueError) as err:
        print(f'error: {type(err).__name__}: {err}', file=sys.stderr)
        return 2
    for r in rec['checks']:
        print(f"  {r['status']:5} {r['check']:15} {r['detail']}")
    if a.dry_run:
        c = rec['copy']
        if c:
            print(f"copy: {c['copy']}\n  library location: {c['library_url']}\n  DTD {c['dtd_validation']['version']}: "
                  f"{c['dtd_validation']['status']}  sha256 {c['copy_xml_sha256'][:16]}  "
                  f"(replaced old import-options: {c['replaced_import_options']})")
        print('plan:', rec['plan_command'])
        print('live steps:')
        for i, s in enumerate(rec['live_steps'], 1):
            print(f'  {i}. {s}')
        if rec['would_refuse']:
            print('WOULD REFUSE NOW: ' + '; '.join(f"{r['check']}: {r['detail']}" for r in rec['would_refuse']))
        return 1 if rec['would_refuse'] else 0
    print(f"{rec['status']} / {rec['verdict']}: {len(rec.get('projects', []))} projects, "
          f"{len(rec.get('warnings', []))} warnings -> {a.receipt}" + (f"  ({rec['note']})" if rec.get('note') else ''))
    return 0 if rec['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
