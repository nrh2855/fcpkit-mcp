#!/usr/bin/env python3
"""Which Final Cut Pro is running, and what libraries/events/projects it has open (read only).

Several FCP copies can share the bundle id `com.apple.FinalCutApp` and the
executable name "Final Cut Pro" (the stock /Applications/Final Cut Pro.app, an
older version kept beside it, a SpliceKit-patched copy). `tell application
"Final Cut Pro"`, `open -a "Final Cut Pro"` and `pgrep -x` cannot tell them
apart, so:

- `running_fcp()` finds FCP processes by their executable *path*
  (`ps -axo pid=,comm=`, confirmed with libproc `proc_pidpath`). Two running
  instances at once is refused (ambiguous target).
- `list_libraries()` addresses Apple events to the running instance by its
  full .app path, only after checking that exactly that one instance runs.
  The script itself first asks `application "<path>" is running` (does not
  launch) and returns early otherwise, and its first output field is the
  responding app's `version`, which must equal the version of the app at that
  path (e.g. 12.3 in one copy vs 12.4 in another); a mismatch raises.
- Nothing here launches FCP or changes anything in it: the AppleScript only
  reads the ProEditor.sdef "library inspection" properties.

    fcp_apps() -> [{path, version, bundle_id, executable, splicekit}]
    running_fcp() -> {path, pid, version, splicekit, executable} | None
    list_libraries(app=None, timeout=20, durations=True)
        -> [{name, path, id, persistent_id, events: [{name, id, persistent_id,
             projects: [{name, id, persistent_id, duration_seconds?}]}]}]
    snapshot_diff(before, after) -> dict

CLI:
    fcpkit-libraries apps
    fcpkit-libraries running
    fcpkit-libraries list [--json] [--no-durations] [--timeout 20]
    fcpkit-libraries diff a.json b.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import plistlib
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

from . import config

STOCK_APP = config.STOCK_APP
SPLICEKIT_APP = config.SPLICEKIT_APP
BUNDLE_ID = 'com.apple.FinalCutApp'
EXECUTABLE = 'Final Cut Pro'
US, RS = '\x1f', '\x1e'  # field / record separators in the AppleScript output


class FCPError(RuntimeError):
    """FCP is not in a state this tool may talk to."""


class FCPNotRunning(FCPError):
    pass


class FCPAmbiguous(FCPError):
    pass


class FCPScriptError(FCPError):
    def __init__(self, message: str, hint: str = '', code: int | None = None):
        super().__init__(message + (f' — {hint}' if hint else ''))
        self.hint, self.code = hint, code


# ---------------------------------------------------------------- paths

def norm_path(path: str | os.PathLike) -> str:
    """Comparison key for library paths: absolute, NFC, case-folded, no trailing slash."""
    text = unicodedata.normalize('NFC', os.path.abspath(os.path.expanduser(str(path))))
    return text.rstrip('/').casefold() or '/'


def same_path(a, b) -> bool:
    return norm_path(a) == norm_path(b)


def norm_name(name: str) -> str:
    return unicodedata.normalize('NFC', name or '')


# ---------------------------------------------------------------- apps and processes

def _app_info(app: Path) -> dict | None:
    plist = app / 'Contents/Info.plist'
    try:
        with open(plist, 'rb') as fh:
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException):
        return None
    exe = info.get('CFBundleExecutable', EXECUTABLE)
    return {'path': str(app), 'version': info.get('CFBundleShortVersionString'),
            'build': info.get('CFBundleVersion'), 'bundle_id': info.get('CFBundleIdentifier'),
            'executable': str(app / 'Contents/MacOS' / exe), 'splicekit': 'SpliceKit' in app.name}


def fcp_apps(apps=None) -> list[dict]:
    """Every known FCP .app on disk with its version (reads Info.plist only).
    Known = config.fcp_apps(): FCPKIT_FCP_APP, FCPKIT_FCP_APPS, the stock app, SpliceKit if installed."""
    apps = config.fcp_apps() if apps is None else apps
    return [info for info in (_app_info(Path(a)) for a in apps) if info]


def _processes() -> list[tuple[int, str]]:
    out = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        pid, _, comm = line.strip().partition(' ')
        if pid.isdigit():
            rows.append((int(pid), comm.strip()))
    return rows


def _pid_path(pid: int) -> str | None:
    """Executable path of a pid via libproc (exact, not truncated)."""
    try:
        libproc = ctypes.CDLL('/usr/lib/libproc.dylib')
        buf = ctypes.create_string_buffer(4096)
        n = libproc.proc_pidpath(ctypes.c_int(pid), buf, ctypes.c_uint32(len(buf)))
    except OSError:
        return None
    return os.fsdecode(buf.raw[:n]) if n > 0 else None


def _app_of_executable(exe: str) -> str | None:
    marker = '.app/Contents/MacOS/'
    i = exe.rfind(marker)
    return exe[:i + 4] if i >= 0 else None


def running_fcp_all(processes=None, pid_path=_pid_path, apps=None) -> list[dict]:
    """Every running FCP process (any copy whose executable is .../Contents/MacOS/Final Cut Pro)."""
    rows = _processes() if processes is None else processes
    apps = config.fcp_apps() if apps is None else apps
    known = {norm_path(a): Path(a) for a in apps}
    found = []
    for pid, comm in rows:
        if os.path.basename(comm) != EXECUTABLE:
            continue
        exe = pid_path(pid) or comm
        if os.path.basename(exe) != EXECUTABLE:
            continue
        app = _app_of_executable(exe)
        if not app:
            continue
        info = _app_info(known.get(norm_path(app), Path(app))) or {'path': app, 'version': None,
                                                                   'splicekit': 'SpliceKit' in app}
        found.append({'path': info['path'], 'pid': pid, 'version': info.get('version'),
                      'splicekit': info.get('splicekit', False), 'executable': exe,
                      'known': norm_path(app) in known})
    return found


def running_fcp(processes=None, pid_path=_pid_path, apps=None) -> dict | None:
    """The one running FCP instance, or None. Two at once raises FCPAmbiguous."""
    found = running_fcp_all(processes, pid_path, apps)
    if len(found) > 1:
        raise FCPAmbiguous('more than one Final Cut Pro is running: '
                           + ', '.join(f"{f['path']} (pid {f['pid']})" for f in found)
                           + ' — Apple events by path/bundle id would be ambiguous; quit one')
    return found[0] if found else None


# ---------------------------------------------------------------- AppleScript

def _as_string(text: str) -> str:
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def applescript_list_libraries(app_path: str, timeout: int = 20, durations: bool = True) -> str:
    """Read-only listing script for the FCP at app_path. Output: records joined by ASCII 30,
    fields by ASCII 31: A|version, L|name|path|id|pid, E|name|id|pid, P|name|id|pid|value|timescale.

    Every id is read inside its own `try`: on FCP 12.3 `persistent ID` cannot be coerced to
    text (-1700, seen live), and one unreadable field must not lose the whole listing."""
    app = _as_string(str(app_path))
    dur = '''
						try
							set d to duration of (sequence of pj)
							set dv to (value of d) as text
							set dt to (timescale of d) as text
						end try''' if durations else ''
    return f'''set out to ""
set RS to (character id 30)
set US to (character id 31)
if not (application {app} is running) then return "ERR" & US & "not running"
tell application {app}
	with timeout of {int(timeout)} seconds
		set out to "A" & US & (version as text) & RS
		repeat with lib in (every library)
			set lp to ""
			try
				set lp to POSIX path of ((file of lib) as text)
			end try
			set {{li, lpid}} to {{"", ""}}
			try
				set li to (id of lib) as text
			end try
			try
				set lpid to (persistent ID of lib) as text
			end try
			set out to out & "L" & US & (name of lib) & US & lp & US & li & US & lpid & RS
			repeat with ev in (every event of lib)
				set {{ei, epid}} to {{"", ""}}
				try
					set ei to (id of ev) as text
				end try
				try
					set epid to (persistent ID of ev) as text
				end try
				set out to out & "E" & US & (name of ev) & US & ei & US & epid & RS
				repeat with pj in (every project of ev)
					set {{pi, ppid, dv, dt}} to {{"", "", "", ""}}
					try
						set pi to (id of pj) as text
					end try
					try
						set ppid to (persistent ID of pj) as text
					end try{dur}
					set out to out & "P" & US & (name of pj) & US & pi & US & ppid & US & dv & US & dt & RS
				end repeat
			end repeat
		end repeat
	end timeout
end tell
return out
'''


def parse_listing(text: str) -> tuple[str | None, list[dict]]:
    """(responding app version, libraries) from the script output."""
    text = text.rstrip('\n')
    if text.startswith('ERR' + US):
        raise FCPNotRunning('the script found FCP not running: ' + text.split(US, 1)[1])
    version, libs = None, []
    for rec in filter(None, text.split(RS)):
        f = rec.split(US)
        kind = f[0]
        if kind == 'A':
            version = f[1]
        elif kind == 'L':
            libs.append({'name': f[1], 'path': f[2].rstrip('/') if len(f[2]) > 1 else f[2], 'id': f[3],
                         'persistent_id': f[4], 'events': []})
        elif kind == 'E':
            if not libs:
                raise FCPScriptError(f'event record before any library: {rec!r}')
            libs[-1]['events'].append({'name': f[1], 'id': f[2], 'persistent_id': f[3], 'projects': []})
        elif kind == 'P':
            if not libs or not libs[-1]['events']:
                raise FCPScriptError(f'project record before any event: {rec!r}')
            pj = {'name': f[1], 'id': f[2], 'persistent_id': f[3]}
            if len(f) > 5 and f[4] and f[5]:
                try:
                    value, scale = float(f[4].replace(',', '.')), float(f[5].replace(',', '.'))
                    pj['duration_seconds'] = round(value / scale, 6) if scale else None
                    pj['duration_raw'] = [f[4], f[5]]
                except ValueError:
                    pj['duration_raw'] = [f[4], f[5]]
            libs[-1]['events'][-1]['projects'].append(pj)
        else:
            raise FCPScriptError(f'unknown record {rec!r}')
    return version, libs


HINTS = {
    -1743: 'this host lacks Automation permission for Final Cut Pro: System Settings › Privacy & Security › '
           'Automation › allow the terminal/app running this tool to control "Final Cut Pro"',
    -1712: 'Apple event timed out: usually a modal dialog/sheet is open in FCP (close it) or FCP is busy',
    -600: 'Final Cut Pro is not running (or quit while being asked)',
    -1728: 'FCP could not find an object (library closed while listing?) — retry',
    -1700: 'type coercion failed (FCP scripting changed?)',
}


def _error_code(stderr: str) -> int | None:
    import re
    m = re.search(r'\((-?\d+)\)\s*$', stderr.strip())
    return int(m.group(1)) if m else None


def run_osascript(script: str, timeout: float) -> str:
    try:
        r = subprocess.run(['/usr/bin/osascript', '-'], input=script, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired as err:
        raise FCPScriptError('osascript timed out', HINTS[-1712], -1712) from err
    if r.returncode:
        code = _error_code(r.stderr)
        raise FCPScriptError('osascript failed: ' + r.stderr.strip()[-400:], HINTS.get(code, ''), code)
    return r.stdout


def list_libraries(app: str | None = None, timeout: int = 20, durations: bool = True,
                   runner=run_osascript, running=None) -> list[dict]:
    """Open libraries of the running FCP. Never launches FCP: refuses unless exactly one
    instance runs and (if given) it is `app`."""
    inst = running if running is not None else running_fcp()
    if not inst:
        raise FCPNotRunning('Final Cut Pro is not running; not sending Apple events (they would launch it)')
    if app and not same_path(app, inst['path']):
        raise FCPError(f"asked for {app} but the running FCP is {inst['path']} (pid {inst['pid']})")
    script = applescript_list_libraries(inst['path'], timeout, durations)
    version, libs = parse_listing(runner(script, timeout + 10))
    if inst.get('version') and version and version != inst['version']:
        raise FCPError(f"Apple events reached FCP {version}, expected {inst['version']} at {inst['path']}")
    return libs


def snapshot(app: str | None = None, timeout: int = 20, durations: bool = True, **kw) -> dict:
    inst = kw.pop('running', None) or running_fcp()
    libs = list_libraries(app, timeout, durations, running=inst, **kw)
    return {'taken': datetime.now().isoformat(timespec='seconds'), 'app': inst, 'libraries': libs}


# ---------------------------------------------------------------- lookups and diff

def _libs(snap) -> list[dict]:
    return snap['libraries'] if isinstance(snap, dict) else list(snap or [])


def find_library(snap, path) -> dict | None:
    for lib in _libs(snap):
        if lib.get('path') and same_path(lib['path'], path):
            return lib
    return None


def find_events(lib: dict, name: str) -> list[dict]:
    return [ev for ev in lib.get('events', []) if norm_name(ev['name']) == norm_name(name)]


def _key(item: dict) -> str:
    return item.get('id') or item.get('persistent_id') or ('name:' + norm_name(item['name']))


def snapshot_diff(before, after) -> dict:
    """What appeared/disappeared between two listings (matched by persistent ID, else name)."""
    out = {k: [] for k in ('libraries_added', 'libraries_removed', 'events_added', 'events_removed',
                           'projects_added', 'projects_removed', 'projects_changed')}
    b = {norm_path(l['path']) if l.get('path') else 'name:' + l['name']: l for l in _libs(before)}
    a = {norm_path(l['path']) if l.get('path') else 'name:' + l['name']: l for l in _libs(after)}
    for k in a.keys() - b.keys():
        out['libraries_added'].append({'name': a[k]['name'], 'path': a[k].get('path')})
    for k in b.keys() - a.keys():
        out['libraries_removed'].append({'name': b[k]['name'], 'path': b[k].get('path')})
    for k, lib in a.items():
        old = b.get(k, {'events': []})
        bev = {_key(e): e for e in old['events']}
        aev = {_key(e): e for e in lib['events']}
        for ek, ev in aev.items():
            if ek not in bev:
                out['events_added'].append({'library': lib.get('path') or lib['name'], 'event': ev['name'],
                                            'persistent_id': ev.get('persistent_id')})
            bpj = {_key(p): p for p in bev.get(ek, {'projects': []})['projects']}
            for pk, pj in ((_key(p), p) for p in ev['projects']):
                row = {'library': lib.get('path') or lib['name'], 'event': ev['name'], **pj}
                if pk not in bpj:
                    out['projects_added'].append(row)
                elif (pj.get('name') != bpj[pk].get('name')
                      or ('duration_seconds' in pj and 'duration_seconds' in bpj[pk]
                          and pj['duration_seconds'] != bpj[pk]['duration_seconds'])):
                    out['projects_changed'].append({**row, 'before': bpj[pk]})
            for pk, pj in bpj.items():
                if pk not in {_key(p) for p in ev['projects']}:
                    out['projects_removed'].append({'library': lib.get('path') or lib['name'],
                                                    'event': ev['name'], **pj})
        for ek, ev in bev.items():
            if ek not in aev:
                out['events_removed'].append({'library': lib.get('path') or lib['name'], 'event': ev['name'],
                                              'persistent_id': ev.get('persistent_id')})
    out['changed'] = any(out[k] for k in out)
    return out


# ---------------------------------------------------------------- disk side (no Apple events)

def library_mtime(path) -> float | None:
    """Newest mtime of the library's own database files (library, event, project stores)."""
    import glob
    root = os.path.expanduser(str(path))
    if not os.path.isdir(root):
        return None
    files = [os.path.join(root, 'CurrentVersion.flexolibrary')]
    files += glob.glob(os.path.join(glob.escape(root), '*', 'CurrentVersion.fcpevent'))
    files += glob.glob(os.path.join(glob.escape(root), '*', '*', 'CurrentVersion.fcpevent'))
    times = [os.path.getmtime(f) for f in files if os.path.exists(f)]
    return max(times) if times else os.path.getmtime(root)


def disk_event_names(path) -> list[str] | None:
    """Event folder names inside a library bundle on disk (NFC), or None if not on disk."""
    root = os.path.expanduser(str(path))
    if not os.path.isdir(root):
        return None
    return sorted(norm_name(n) for n in os.listdir(root)
                  if not n.startswith('.') and os.path.isfile(os.path.join(root, n, 'CurrentVersion.fcpevent')))


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('apps')
    sub.add_parser('running')
    ls = sub.add_parser('list')
    ls.add_argument('--json', action='store_true')
    ls.add_argument('--no-durations', action='store_true')
    ls.add_argument('--timeout', type=int, default=20)
    df = sub.add_parser('diff')
    df.add_argument('a')
    df.add_argument('b')
    args = ap.parse_args(argv)
    if args.cmd == 'apps':
        print(json.dumps(fcp_apps(), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == 'running':
        print(json.dumps(running_fcp(), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == 'diff':
        d = snapshot_diff(json.load(open(args.a)), json.load(open(args.b)))
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 1 if d['changed'] else 0
    try:
        snap = snapshot(timeout=args.timeout, durations=not args.no_durations)
    except FCPError as err:
        print(f'error: {err}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=1))
        return 0
    app = snap['app']
    print(f"{app['path']}  pid {app['pid']}  FCP {app['version']}")
    for lib in snap['libraries']:
        print(f"{lib['name']}  {lib['path']}  [{lib['persistent_id']}]")
        for ev in lib['events']:
            print(f"  {ev['name']}  ({len(ev['projects'])} projects)")
            for pj in ev['projects']:
                dur = pj.get('duration_seconds')
                print(f"    {pj['name']}" + (f'  {dur:.3f}s' if isinstance(dur, float) else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
