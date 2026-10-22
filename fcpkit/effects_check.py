#!/usr/bin/env python3
"""Check that every <effect> resource in an FCPXML can be found (read only).

Why: FCP drops or breaks clips silently when an effect cannot be resolved.
A Motion template uid without `src` makes FCP drop every title of that
template on import; a missing FxPlug or template turns into a red
"missing effect" clip only after import.

For each `<resources><effect>` it reports
  {id, name, uid, kind, origin, src, status, detail, used_by_count, used_by}.

uid forms and how they are checked:
- `~/Titles.localized/...moti` (also Effects/Generators/Transitions):
  user template under ~/Movies/Motion Templates.localized. `src` must exist;
  the uid file in ~/Movies is reported too, and whether it is the same file.
- `..../<UUID>/Motion Templates.localized/...`: template embedded in a
  library. Only `src` can locate it, so `src` must exist.
- `.../Titles.localized/...`: FCP built-in Motion content, looked up in the
  FCP bundles' MotionEffect.fxp Templates/PETemplates/METemplates folders.
- `FxPlug:<UUID>`: looked up in the ProPlugPlugInList of FCP's
  InternalFiltersXPC Filters.bundle Info.plist and of third-party
  pluginkits/fxplug bundles. A bare UUID (no `FxPlug:` prefix) is a defect.
- `FF...`, `AudioUnit: 0x...`, `*.audio.effectBundle`: FCP/system internals
  with no registry we can read -> INFO (not verified, not a failure).

Statuses: OK, MISSING, REVIEW (could not verify), INFO.
Exit code: 1 if any MISSING, else 2 if any REVIEW, else 0.
"""
from __future__ import annotations

import argparse
import glob
import json
import plistlib
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

from . import config
from .timeline import existing_path, url_to_path, xml_path

UUID_RE = re.compile(r'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$')
KIND_BY_EXT = {'.moti': 'title', '.moef': 'effect', '.motn': 'generator', '.motr': 'transition'}
TEMPLATE_ROOTS = ('Templates.localized', 'PETemplates.localized', 'METemplates.localized')
MOTION_RES = 'Contents/PlugIns/MediaProviders/MotionEffect.fxp/Contents/Resources'
LICENSED_HINTS = ('DesignStudio', 'motionVFX')


def nfc(s: str) -> str:
    return unicodedata.normalize('NFC', s)


# ------------------------------------------------------------------ registries
def _plugin_plists(bundle: Path) -> list[Path]:
    """Info.plists inside a bundle that may carry a ProPlugPlugInList."""
    pats = ['Contents/Info.plist',
            'Contents/PlugIns/*.pluginkit/Contents/Info.plist',
            'Contents/PlugIns/*.pluginkit/Contents/PlugIns/*/Contents/Info.plist',
            'Contents/PlugIns/*.fxplug/Contents/Info.plist',
            'Contents/PlugIns/*.appex/Contents/Info.plist']
    out = []
    for pat in pats:
        out += [Path(p) for p in glob.glob(glob.escape(str(bundle)) + '/' + pat)]
    return out


def load_fxplug_registry(fcp_apps=None, third_party=True) -> dict:
    """{'uuids': {UUID: {name, source, obsolete, builtin}}, 'scanned': [...], 'fcp_found': bool}."""
    fcp_apps = [Path(a) for a in (config.fcp_apps() if fcp_apps is None else fcp_apps)]
    reg = {'uuids': {}, 'scanned': [], 'errors': [], 'fcp_found': False}
    bundles: list[tuple[Path, bool]] = [(a, True) for a in fcp_apps if a.is_dir()]
    reg['fcp_found'] = bool(bundles)
    if third_party:
        home = str(Path.home())
        pats = ['/Applications/*.app', '/Applications/*/*.app', '/Applications/*/*/*.app',
                home + '/Applications/*.app', home + '/Applications/*/*.app',
                '/Library/Plug-Ins/FxPlug/*', home + '/Library/Plug-Ins/FxPlug/*']
        fcp_set = {str(b) for b, _ in bundles}
        for pat in pats:
            for p in sorted(glob.glob(pat)):
                if p not in fcp_set:
                    bundles.append((Path(p), False))
    for bundle, builtin in bundles:
        for plist in _plugin_plists(bundle):
            try:
                with open(plist, 'rb') as fh:
                    data = plistlib.load(fh)
            except Exception as exc:  # unreadable plist: note it, keep going
                reg['errors'].append(f'{plist}: {exc}')
                continue
            entries = data.get('ProPlugPlugInList') if isinstance(data, dict) else None
            if entries is None:
                continue
            reg['scanned'].append({'plist': str(plist), 'entries': len(entries), 'fcp': builtin})
            for e in entries:
                u = str(e.get('uuid', '')).upper()
                if u and u not in reg['uuids']:
                    reg['uuids'][u] = {'name': e.get('displayName'), 'source': str(plist),
                                       'obsolete': e.get('obsolete') == '1', 'builtin': builtin}
    reg['has_fcp_registry'] = any(s['fcp'] and s['entries'] for s in reg['scanned'])
    return reg


def builtin_template_roots(fcp_apps=None) -> list[Path]:
    fcp_apps = [Path(a) for a in (config.fcp_apps() if fcp_apps is None else fcp_apps)]
    return [a / MOTION_RES / r for a in fcp_apps for r in TEMPLATE_ROOTS if (a / MOTION_RES / r).is_dir()]


# --------------------------------------------------------------------- checks
def classify(uid: str) -> tuple[str, str]:
    """(kind, origin)."""
    if uid.startswith('FxPlug:'):
        return 'fxplug', 'fxplug'
    if UUID_RE.match(uid):
        return 'fxplug', 'bare-uuid'
    if uid.startswith('AudioUnit:'):
        return 'audio-unit', 'internal'
    if uid.endswith('.effectBundle'):
        return 'audio-effect-bundle', 'internal'
    ext = Path(uid).suffix.lower()
    kind = KIND_BY_EXT.get(ext)
    if uid.startswith('~/'):
        return kind or 'template?', 'user'
    if uid.startswith('..../'):
        return kind or 'template?', 'library'
    if uid.startswith('.../'):
        return kind or 'template?', 'builtin'
    return 'internal', 'internal'


def _same(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    try:
        return a.resolve() == b.resolve() or nfc(str(a)) == nfc(str(b))
    except OSError:
        return nfc(str(a)) == nfc(str(b))


def check_effect(el, ctx) -> dict:
    uid = el.get('uid') or ''
    src = el.get('src')
    kind, origin = classify(uid) if uid else ('unknown', 'none')
    row = {'id': el.get('id'), 'name': el.get('name'), 'uid': uid, 'kind': kind, 'origin': origin,
           'src': src, 'status': 'REVIEW', 'detail': ''}
    src_path = url_to_path(src) if src else None
    src_found = existing_path(src_path) if src_path else None
    if src:
        row['src_path'] = str(src_path)
        row['src_exists'] = src_found is not None
    if not uid:
        row['status'], row['detail'] = 'MISSING', 'effect has no uid'
        return row

    if origin in ('user', 'library'):
        rel = uid[2:] if origin == 'user' else uid.split('/Motion Templates.localized/', 1)[-1]
        home_file = ctx['templates_dir'] / rel if (origin == 'user' or '/Motion Templates.localized/' in uid) else None
        home_found = existing_path(home_file) if home_file else None
        if home_file is not None:
            row['uid_path'] = str(home_file)
            row['uid_exists'] = home_found is not None
        notes = []
        if src_found and home_found:
            row['src_matches_uid'] = _same(src_found, home_found)
            if not row['src_matches_uid']:
                notes.append('src and ~/Movies uid template are different files')
        if any(h in uid for h in LICENSED_HINTS):
            notes.append('licensed template: file existence only, licence not checked')
        if not src:
            row['status'] = 'MISSING'
            notes.insert(0, 'template uid without src (FCP drops every title of this template on import)'
                         + ('; uid template exists in ~/Movies' if home_found else ''))
        elif not src_found:
            row['status'] = 'MISSING'
            notes.insert(0, 'src file not found' + ('; uid template exists in ~/Movies' if home_found
                                                     else '; uid template not in ~/Movies either'))
        else:
            row['status'] = 'OK'
            if origin == 'user' and not home_found:
                notes.append('uid template not in ~/Movies (only src exists)')
        row['detail'] = '; '.join(notes)
        return row

    if origin == 'builtin':
        rel = uid[4:]
        roots = ctx['builtin_roots']
        hit = next((p for p in (existing_path(r / rel) for r in roots) if p), None)
        if hit:
            row['status'], row['detail'] = 'OK', f'built-in: {hit}'
        elif not roots:
            row['status'], row['detail'] = 'REVIEW', 'no FCP bundle found to look up built-in templates'
        else:
            row['status'], row['detail'] = 'MISSING', f'built-in template not found in {len(roots)} FCP template roots'
        if src and not src_found:
            row['status'], row['detail'] = 'MISSING', (row['detail'] + '; src file not found').lstrip('; ')
        return row

    if kind == 'fxplug':
        reg = ctx['registry']
        if origin == 'bare-uuid':
            known = reg['uuids'].get(uid.upper())
            row['status'] = 'MISSING'
            row['detail'] = 'bare UUID without "FxPlug:" prefix' + (
                f' (uuid is {known["name"]!r} in {known["source"]})' if known else '')
            return row
        u = uid.split(':', 1)[1].strip().upper()
        known = reg['uuids'].get(u)
        if known:
            row['status'] = 'OK'
            row['detail'] = (('FCP built-in' if known['builtin'] else 'third-party, installed on disk; load not verified')
                             + f': {known["name"]} ({known["source"]})' + ('; marked obsolete' if known['obsolete'] else ''))
        elif not reg.get('has_fcp_registry'):
            row['status'], row['detail'] = 'REVIEW', 'FxPlug registry not readable (no FCP Filters.bundle list found)'
        else:
            row['status'] = 'MISSING'
            row['detail'] = f'FxPlug uuid not in FCP registry or {len(reg["scanned"])} scanned plug-in lists'
        return row

    row['status'] = 'INFO'
    row['detail'] = 'FCP/system internal effect; no readable registry, not verified'
    if src and not src_found:
        row['status'], row['detail'] = 'MISSING', 'src file not found'
    return row


def count_uses(root, effect_ids: set[str]) -> dict[str, dict[str, int]]:
    """Elements anywhere in the document (timelines and compound media) referencing each effect."""
    uses: dict[str, dict[str, int]] = {i: {} for i in effect_ids}
    for node in root.iter():
        ref = node.get('ref')
        if ref in uses and node.tag != 'effect':
            uses[ref][node.tag] = uses[ref].get(node.tag, 0) + 1
    return uses


def check(path, registry=None, templates_dir=None, fcp_apps=None) -> dict:
    xml = xml_path(Path(path))
    root = ET.parse(xml).getroot()
    effects = root.findall('./resources/effect')
    ctx = {'registry': registry if registry is not None else load_fxplug_registry(fcp_apps),
           'templates_dir': Path(templates_dir) if templates_dir else config.motion_templates(),
           'builtin_roots': builtin_template_roots(fcp_apps)}
    uses = count_uses(root, {e.get('id') for e in effects})
    rows = []
    for el in effects:
        row = check_effect(el, ctx)
        row['used_by'] = uses.get(el.get('id'), {})
        row['used_by_count'] = sum(row['used_by'].values())
        rows.append(row)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    reg = ctx['registry']
    return {'xml': str(xml), 'effects': len(rows), 'counts': counts,
            'registry': {'fxplug_uuids': len(reg['uuids']), 'plists': reg['scanned'],
                         'errors': reg.get('errors', []), 'fcp_registry': reg.get('has_fcp_registry', False),
                         'builtin_template_roots': [str(r) for r in ctx['builtin_roots']],
                         'templates_dir': str(ctx['templates_dir'])},
            'rows': rows}


def exit_code(report: dict) -> int:
    c = report['counts']
    return 1 if c.get('MISSING') else 2 if c.get('REVIEW') else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('xml', help='.fcpxml file or .fcpxmld bundle')
    ap.add_argument('--out', help='write the JSON report here instead of stdout')
    ap.add_argument('--templates-dir', help='user Motion Templates folder (default $FCPKIT_MOTION_TEMPLATES or '
                                            '~/Movies/Motion Templates.localized)')
    args = ap.parse_args(argv)
    report = check(args.xml, templates_dir=args.templates_dir)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
        print(json.dumps({'counts': report['counts'], 'out': args.out}, ensure_ascii=False))
    else:
        print(text)
    return exit_code(report)


if __name__ == '__main__':
    sys.exit(main())
