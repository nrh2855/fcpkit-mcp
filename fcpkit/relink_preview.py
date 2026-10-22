#!/usr/bin/env python3
"""Preview where an FCPXML's media lives and what a relink would find (read only).

Why: media often lives on external drives that disconnect or mount under
another name, and non-ASCII (e.g. Korean) names appear in both NFC and NFD
form. Policy: never rewrite media paths in an XML in place; relinking is done
in FCP (File > Relink). This tool only reports, so the relink can be planned
and checked before touching FCP.

For each `<asset>` it reports the original-media path, whether it exists
now (NFC/NFD twin aware), whether it carries a `<bookmark>`, and its root
(`/Volumes/<name>`, `/Users/<name>` or the first path segment).

With `--from PREFIX --to PREFIX` it also computes the remapped path by
whole-segment prefix match (`/Media/A` matches `/Media/A/x.mov`, never
`/Media/AB/x.mov`; compared in NFC) and whether that target exists.

Exit code: 1 if any asset would stay unresolved (missing now and not
remapped to an existing file), else 2 if anything needs REVIEW (asset with
no file URL, prefix that matched nothing), else 0. Never writes XML.
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

from .timeline import existing_path, url_to_path, xml_path


def nfc(s: str) -> str:
    return unicodedata.normalize('NFC', s)


def form_of(written: Path, found: Path | None) -> str | None:
    if found is None:
        return None
    if str(found) == str(written):
        return 'as written'
    return 'NFC twin' if str(found) == nfc(str(written)) else 'NFD twin'


def root_of(path: Path) -> str:
    parts = Path(nfc(str(path))).parts
    if len(parts) >= 3 and parts[1] in ('Volumes', 'Users'):
        return '/'.join(parts[:3]).replace('//', '/')
    return '/' + parts[1] if len(parts) >= 2 else str(path)


def norm_prefix(p: str) -> str:
    if p.startswith('file://'):
        p = str(url_to_path(p))
    p = nfc(p)
    return p.rstrip('/') or '/'


def remap(path: Path, src_prefix: str, dst_prefix: str) -> Path | None:
    """Whole-segment prefix replacement; None when the prefix does not match."""
    s = nfc(str(path))
    if src_prefix == '/':
        rest = s
    elif s == src_prefix:
        rest = ''
    elif s.startswith(src_prefix + '/'):
        rest = s[len(src_prefix):]
    else:
        return None
    return Path(dst_prefix.rstrip('/') + rest) if rest else Path(dst_prefix)


def asset_media(asset, base: Path) -> tuple[str | None, Path | None, bool]:
    rep = asset.find("media-rep[@kind='original-media']")
    if rep is None:
        rep = asset.find('media-rep')
    src = rep.get('src') if rep is not None else asset.get('src')
    bookmark = rep is not None and rep.find('bookmark') is not None and bool((rep.find('bookmark').text or '').strip())
    path = url_to_path(src)
    if path is not None and not path.is_absolute():
        path = base / path          # relative src inside a .fcpxmld bundle
    return src, path, bookmark


def preview(xml, src_prefix: str | None = None, dst_prefix: str | None = None) -> dict:
    xml_file = xml_path(Path(xml))
    root = ET.parse(xml_file).getroot()
    assets = root.findall('./resources/asset')
    sp = norm_prefix(src_prefix) if src_prefix else None
    dp = norm_prefix(dst_prefix) if dst_prefix else None
    rows, groups, review = [], {}, []
    for a in assets:
        src, path, bookmark = asset_media(a, xml_file.parent)
        row = {'id': a.get('id'), 'name': a.get('name'), 'src': src, 'has_bookmark': bookmark}
        if path is None or (src and '://' in src and not src.startswith('file://')):
            row.update(path=None, exists=None, status='REVIEW', detail='no file URL for media')
            review.append(f'{a.get("id")}: no file URL')
            rows.append(row)
            groups.setdefault('(none)', {'assets': 0, 'exists': 0, 'missing': 0, 'review': 0})['review'] += 1
            groups['(none)']['assets'] += 1
            continue
        found = existing_path(path)
        row.update(path=nfc(str(path)), exists=found is not None, found_as=form_of(path, found),
                   root=root_of(path), status='OK' if found else 'MISSING')
        g = groups.setdefault(row['root'], {'assets': 0, 'exists': 0, 'missing': 0, 'review': 0})
        g['assets'] += 1
        g['exists' if found else 'missing'] += 1
        if sp is not None:
            target = remap(path, sp, dp)
            row['remap_matched'] = target is not None
            if target is not None:
                t_found = existing_path(target)
                row.update(target=nfc(str(target)), target_exists=t_found is not None,
                           target_found_as=form_of(target, t_found))
        rows.append(row)

    def unresolved(r):
        if r['status'] == 'REVIEW':
            return False
        if r.get('remap_matched'):
            return not r['target_exists']
        return not r['exists']

    summary = {'assets': len(rows),
               'exists': sum(1 for r in rows if r.get('exists')),
               'missing': sum(1 for r in rows if r['status'] == 'MISSING'),
               'with_bookmark': sum(1 for r in rows if r['has_bookmark']),
               'review': sum(1 for r in rows if r['status'] == 'REVIEW'),
               'by_root': groups}
    if sp is not None:
        matched = [r for r in rows if r.get('remap_matched')]
        summary['remap'] = {'from': sp, 'to': dp, 'matched': len(matched),
                            'target_exists': sum(1 for r in matched if r['target_exists']),
                            'target_missing': sum(1 for r in matched if not r['target_exists']),
                            'unmatched': len(rows) - len(matched)}
        if not matched:
            review.append(f'--from {sp!r} matched no asset')
    summary['unresolved'] = sum(1 for r in rows if unresolved(r))
    return {'xml': str(xml_file), 'summary': summary, 'review': review, 'rows': rows}


def exit_code(report: dict) -> int:
    return 1 if report['summary']['unresolved'] else 2 if report['review'] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('xml', help='.fcpxml file or .fcpxmld bundle')
    ap.add_argument('--from', dest='src_prefix', help='path prefix to replace (whole segments)')
    ap.add_argument('--to', dest='dst_prefix', help='replacement prefix')
    ap.add_argument('--out', help='write the JSON report here instead of stdout')
    args = ap.parse_args(argv)
    if (args.src_prefix is None) != (args.dst_prefix is None):
        ap.error('--from and --to go together')
    report = preview(args.xml, args.src_prefix, args.dst_prefix)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
        print(json.dumps({'summary': report['summary'], 'review': report['review'], 'out': args.out},
                         ensure_ascii=False))
    else:
        print(text)
    return exit_code(report)


if __name__ == '__main__':
    sys.exit(main())
