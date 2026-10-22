#!/usr/bin/env python3
"""Apply an edited timeline brief back to the FCPXML ("recompile" the changed lines only).

Workflow:
    timeline_brief.py X.fcpxmld --ids --out X.brief.md     # every element line ends with #n
    (edit X.brief.md: change times, lengths, lanes, caption text, volumes; delete lines)
    brief_apply.py X.fcpxmld X.brief.md --out X_fixed.fcpxmld

Only the lines that changed are applied, to the exact element each #n stands
for; everything else in the XML stays as it was. What can change:

| line                         | allowed                                      |
|------------------------------|----------------------------------------------|
| anchored item (caption, title, overlay, sound, music) | start time, length, lane, caption text, `vol NdB`, delete the line |
| main-storyline cut (▌)       | `vol NdB` only (time/length would ripple)    |
| items in a connected storyline, inside compounds, transitions, ⊂/· lines | nothing |

Anything else — a new line, an edited alias/name/value, a keyframed volume,
text split across styles, a length past the media — is REFUSED with the
reason, and nothing is written unless every change can be applied (or
--partial). Times snap to the nearest frame. Moving an item past its host
clip re-anchors it to the main-storyline cut at the new time, inserting it
where the FCP DTD allows.

Safety: the brief must carry the sha256 of this exact XML; the output is a
NEW file written through fcpxml_io (lossless envelope, DTD validated, source
never overwritten). After applying, every element of the timeline is compared
before/after: only the edited elements may differ, and each must have exactly
the requested values — otherwise the result is not written.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from fractions import Fraction
from pathlib import Path

from . import xmlio
from .timeline import TIMED, Timeline, fmt, t
from .timeline_brief import HEADER, Brief, build_brief, file_sha256, parse_tc

MAIN_RE = re.compile(r'^▌(?P<time>\S+) (?P<body>.*) \((?P<dur>[\d.]+)s\) #(?P<id>\d+)$')
ITEM_RE = re.compile(r'^\s+(?P<lane>[+-]\d+) (?P<time>\S+) (?P<body>.*) (?P<dur>[\d.]+)s #(?P<id>\d+)$')
ANY_ID = re.compile(r'#(\d+)\s*$')
VOL_RE = re.compile(r'(?<![\w.])vol (-?\d+(?:\.\d+)?)dB(?![\w:])')
TEXT_RE = re.compile(r'^(T\d+) "(.*)"(?= |$)')
ANCHOR = {'audio', 'video', 'clip', 'title', 'caption', 'mc-clip', 'ref-clip', 'sync-clip', 'asset-clip',
          'audition', 'spine', 'live-drawing'}


class Refused(Exception):
    pass


# ----------------------------------------------------------------- DTD order
def dtd_rank(version: str):
    """{element: {child: rank}} from the installed FCP DTD (parameter entities expanded)."""
    from . import fcpxml_io
    names = ['Final Cut Pro Creator Studio.app', 'Final Cut Pro.app']
    dtd = None
    for app in names:
        p = Path('/Applications') / app / 'Contents/Frameworks/Interchange.framework/Versions/A/Resources' \
            / f'FCPXMLv{version.replace(".", "_")}.dtd'
        if p.is_file():
            dtd = p.read_text()
            break
    if dtd is None:
        raise Refused(f'FCPXML {version} DTD not found (needed to place re-anchored items)')
    ents = dict(re.findall(r'<!ENTITY\s+%\s+([\w-]+)\s+"([^"]*)"\s*>', dtd))

    def expand(s, depth=0):
        return s if depth > 10 else re.sub(r'%([\w-]+);', lambda m: expand(ents.get(m.group(1), ''), depth + 1), s)
    ranks = {}
    for name, model in re.findall(r'<!ELEMENT\s+([\w-]+)\s+(\(.*?\))\s*>', dtd, re.S):
        order = {}
        for i, tok in enumerate(re.findall(r'[A-Za-z][\w-]*', expand(model))):
            order.setdefault(tok, i)
        # anchored items are one repeatable choice group: give them one rank
        group = [order[n] for n in ANCHOR if n in order]
        if group:
            for n in ANCHOR:
                if n in order:
                    order[n] = min(group)
        ranks[name] = order
    return ranks


def insert_child(parent, child, ranks):
    order = ranks.get(parent.tag, {})
    r = order.get(child.tag)
    if r is None:
        raise Refused(f'DTD has no place for <{child.tag}> in <{parent.tag}>')
    kids = list(parent)
    pos = 0
    for i, k in enumerate(kids):
        if isinstance(k.tag, str) and order.get(k.tag, 10 ** 6) <= r:
            pos = i + 1
    parent.insert(pos, child)


# ------------------------------------------------------------------ parsing
def parse_line(line: str):
    m = MAIN_RE.match(line)
    if m:
        return dict(m.groupdict(), kind='main')
    m = ITEM_RE.match(line)
    if m:
        return dict(m.groupdict(), kind='item')
    return None


def strip_fields(body: str, is_title: bool):
    """(body without the editable fields, text or None, vol or None)."""
    text = None
    if is_title:
        m = TEXT_RE.match(body)
        if m:
            text = m.group(2)
            body = m.group(1) + ' "…"' + body[m.end():]
    vols = VOL_RE.findall(body)
    vol = vols[0] if len(vols) == 1 else None
    body = VOL_RE.sub('vol …', body)
    return body, text, vol


def snap(value: Fraction, frame: Fraction) -> Fraction:
    q = value / frame
    n = q.numerator // q.denominator
    if q - n >= Fraction(1, 2):
        n += 1
    return n * frame


def display_map(raw: str):
    """(text as the brief shows it, index of each shown character in raw).

    Same rule as Brief.text: whitespace collapses to one space, a line break between
    non-empty lines shows as ' / ', leading/trailing whitespace is dropped."""
    out, where, i, n = [], [], 0, len(raw)
    while i < n:
        if raw[i].isspace():
            j = i
            while j < n and raw[j].isspace():
                j += 1
            if out and j < n:
                sep = ' / ' if '\n' in raw[i:j] else ' '
                out.append(sep)
                where += [i] * len(sep)
            i = j
        else:
            out.append(raw[i])
            where.append(i)
            i += 1
    return ''.join(out), where


# ----------------------------------------------------------------- applying
class Applier:
    def __init__(self, xml, brief_text: str):
        self.xml = xml
        head = brief_text.splitlines()[0] if brief_text else ''
        if not head.startswith(HEADER):
            raise Refused('the brief has no fcpkit-brief header: make it with timeline_brief.py --ids')
        meta = dict(re.findall(r'(\w+)=(\S+)', head))
        if meta.get('sha256') != file_sha256(xml):
            raise Refused('the brief was made from a different version of this XML (sha256 mismatch)')
        self.tree, self.envelope = xmlio.load(xml)
        lim = lambda k: Fraction(meta[k]) if k in meta else None  # noqa: E731
        self.orig_text, self.brief = build_brief(xml, start=lim('from'), end=lim('to'),
                                                 detail=int(meta.get('detail', 2)), lang=meta.get('lang', 'ko'),
                                                 ids=True, open_overlays=meta.get('overlays', 'open') == 'open',
                                                 tree=self.tree)
        self.tl: Timeline = self.brief.tl
        self.frame = self.tl.frame
        self.rows = {r['id']: r for r in self.brief.rows}
        self.edited = brief_text
        self.version = self.tl.root.get('version', '1.11')
        self._ranks = None
        self.applied, self.refused, self.expect, self.warnings = [], [], {}, []

    def ranks(self):
        if self._ranks is None:
            self._ranks = dtd_rank(self.version)
        return self._ranks

    def lines_by_id(self, text):
        out, extra = {}, []
        for ln in text.splitlines():
            m = ANY_ID.search(ln)
            if m:
                out[int(m.group(1))] = ln
            elif parse_line(ln + ' #0') and not ln.startswith(('#', '|', '<')):
                extra.append(ln)
        return out, extra

    def run(self):
        before, _ = self.lines_by_id(self.orig_text)
        after, added = self.lines_by_id(self.edited)
        for ln in added:
            self.refused.append({'line': ln, 'reason': 'new lines cannot be created from the brief (no #id)'})
        for n in sorted(set(after) - set(before)):
            self.refused.append({'id': n, 'reason': f'#{n} does not exist in this XML'})
        for n in sorted(before):
            row = self.rows[n]
            if n not in after:
                self.attempt(row, 'delete', None, None)
            elif after[n].rstrip() != before[n].rstrip():
                self.attempt(row, 'change', parse_line(before[n]), parse_line(after[n]), after[n])
        return self

    def attempt(self, row, what, old, new, raw=None):
        it = row['_item']
        try:
            if what == 'delete':
                self.delete(row, it)
            else:
                if new is None:
                    raise Refused('line format not recognised (keep "lane time body length #id")')
                self.change(row, it, old, new)
        except Refused as err:
            self.refused.append({'id': row['id'], 'line': raw or '', 'reason': str(err)})

    # --- edits
    def editable(self, row, it):
        if not row['editable']:
            raise Refused('this line is not editable (inside a compound, a transition or a compound header)')

    def delete(self, row, it):
        self.editable(row, it)
        if it.in_main:
            raise Refused('deleting a main-storyline cut would ripple the timeline: do it in FCP')
        if it.spine is not None:
            raise Refused('item is inside a connected storyline: deleting it would shift the storyline')
        parent = it.parent.el
        children = [c for c in it.el if isinstance(c.tag, str) and c.tag in TIMED]
        parent.remove(it.el)
        self.expect[id(it.el)] = 'deleted'
        self.applied.append({'id': row['id'], 'edit': 'delete', 'what': row['name'] or row['tag'],
                             'with_children': len(children)})

    def change(self, row, it, old, new):
        self.editable(row, it)
        if old['kind'] != new['kind']:
            raise Refused('a main-storyline line and an anchored line cannot turn into each other')
        is_title = it.tag in ('title', 'caption')
        ob, otext, ovol = strip_fields(old['body'], is_title)
        nb, ntext, nvol = strip_fields(new['body'], is_title)
        if ob != nb:
            raise Refused('only time, length, lane, caption text and "vol NdB" can be edited on a line')
        changes = {}
        if new['time'] != old['time']:
            changes['start'] = snap(parse_tc(new['time']), self.frame)
        if new['dur'] != old['dur']:
            changes['duration'] = snap(Fraction(new['dur']), self.frame)
        if new.get('lane') != old.get('lane'):
            changes['lane'] = int(new['lane'])
        if ntext != otext:
            changes['text'] = ntext
        if nvol != ovol:
            if nvol is None:
                raise Refused('the volume can be changed, not removed')
            changes['vol'] = nvol
        if not changes:
            return
        if it.in_main and set(changes) - {'vol'}:
            raise Refused('a main-storyline cut accepts only "vol NdB" (time/length/lane would ripple)')
        if it.spine is not None and set(changes) & {'start', 'duration', 'lane'}:
            raise Refused('item is inside a connected storyline: move the storyline in FCP')
        if 'text' in changes:
            self.set_text(it, changes['text'])
        if 'vol' in changes:
            self.set_vol(it, changes['vol'])
        if 'duration' in changes:
            self.set_duration(it, changes['duration'])
        if 'start' in changes:
            self.move(it, changes['start'])
        if 'lane' in changes:
            if changes['lane'] == 0:
                raise Refused('lane 0 is the main storyline')
            it.el.set('lane', str(changes['lane']))
        if set(changes) & {'start', 'duration', 'lane'}:
            self.warn_overlap(it, changes)
        self.expect[id(it.el)] = {k: v for k, v in changes.items()}
        self.applied.append({'id': row['id'], 'edit': 'change', 'what': row['name'] or row['tag'],
                             **{k: (str(v) if isinstance(v, Fraction) else v) for k, v in changes.items()}})

    def warn_overlap(self, it, changes):
        """Warn (not refuse) when the edited item now overlaps another item on the same lane."""
        start = changes.get('start', it.abs_start)
        end = start + changes.get('duration', it.duration)
        lane = changes.get('lane', it.lane)
        for o in self.tl.items:
            if o is it or o.in_main or o.lane != lane or o.spine is not None or o.tag == 'transition':
                continue
            if o.abs_start < end and o.abs_end > start:
                self.warnings.append(f'#{it.index} now overlaps "{(Brief.text(o.el) or o.name)[:30]}" on lane {lane} '
                                     f'by {float((min(end, o.abs_end) - max(start, o.abs_start)) / self.frame):.0f} f '
                                     '(FCP will stack one of them on another lane)')

    def set_text(self, it, text):
        """Change only the characters that changed, inside the one styled run that holds them."""
        if it.tag != 'title':
            raise Refused('text can be edited on titles only')
        runs = [ts for ts in it.el.iter('text-style') if ts.text is not None and ts.get('ref') is not None] \
            or [ts for ts in it.el.iter('text-style') if ts.text]
        raw = ''.join(r.text for r in runs)
        shown, where = display_map(raw)
        if shown != Brief.text(it.el):
            raise Refused('this title text cannot be mapped to its styled runs: edit it in FCP')
        p = 0
        while p < min(len(shown), len(text)) and shown[p] == text[p]:
            p += 1
        q = 0
        while q < min(len(shown), len(text)) - p and shown[-1 - q] == text[-1 - q]:
            q += 1
        a = where[p] if p < len(shown) else len(raw)            # raw span [a, b) is replaced
        b = where[len(shown) - q - 1] + 1 if len(shown) - q > p else a
        new_part = text[p:len(text) - q].replace(' / ', '\n')
        bounds, pos = [], 0
        for r in runs:
            bounds.append((pos, pos + len(r.text), r))
            pos += len(r.text)
        inside = [(lo, hi, r) for lo, hi, r in bounds if lo <= a and b <= hi and (a < hi or (a == hi == b))]
        if not inside:
            raise Refused(f'the change spans {sum(1 for lo, hi, _ in bounds if lo < b and hi > a)} differently '
                          'styled parts of this title: edit it in FCP')
        lo, hi, run = inside[0]
        run.text = run.text[:a - lo] + new_part + run.text[b - lo:]

    def set_vol(self, it, vol):
        adj = it.el.find('adjust-volume')
        if adj is not None and adj.find('.//keyframe') is not None:
            raise Refused('this volume is keyframed: edit the keyframes in FCP')
        if adj is None:
            import xml.etree.ElementTree as ET
            adj = ET.Element('adjust-volume')
            insert_child(it.el, adj, self.ranks())
        adj.set('amount', f'{vol}dB')

    def set_duration(self, it, duration):
        if duration < 2 * self.frame:
            raise Refused('length below 2 frames')
        if duration > it.duration and it.tag not in ('title', 'caption'):
            res = self.tl.available(it, it.start + it.duration - self.frame / 2,
                                    'audio' if it.tag == 'audio' or it.lane < 0 else 'video')
            if not res.unlimited and (res.hi is None or res.hi < it.start + duration):
                raise Refused('not enough media after the out-point for that length')
        it.el.set('duration', fmt(duration))

    def move(self, it, target):
        """Move an anchored item to start at `target` (timeline seconds), keeping its source range."""
        host = it.parent
        if host is None:
            raise Refused('item has no host clip')
        delta = target - it.abs_start
        new_offset = t(it.el.get('offset')) + delta
        if host.in_main and host.start <= new_offset < host.start + host.duration:
            it.el.set('offset', fmt(new_offset))
            return
        if not host.in_main:
            raise Refused('item is anchored to another anchored item: move its host instead')
        # re-anchor to the main-storyline cut on screen at the new time
        mains = [m for m in self.tl.main_items() if m.tag not in ('transition',)]
        dest = next((m for m in mains if m.abs_start <= target < m.abs_end), None)
        if dest is None:
            raise Refused('no main-storyline clip at the new time')
        it.parent.el.remove(it.el)
        it.el.set('offset', fmt(dest.start + (target - dest.abs_start)))
        insert_child(dest.el, it.el, self.ranks())

    # --- verification
    def snapshot(self):
        tl = Timeline(self.xml, self.tree)
        return tl, {id(i.el): (i.abs_start, i.abs_end, i.lane, Brief.text(i.el) if i.tag == 'title' else None,
                               (i.el.find('adjust-volume').get('amount') if i.el.find('adjust-volume') is not None
                                else None)) for i in tl.items}

    def verify(self, before):
        tl, after = self.snapshot()
        problems = []
        for key, old in before.items():
            exp = self.expect.get(key)
            if exp == 'deleted':
                if key in after:
                    problems.append('a deleted element is still in the timeline')
                continue
            if key not in after:
                problems.append(f'element disappeared: {old}')
                continue
            new = after[key]
            if exp is None:
                if new != old:
                    problems.append(f'unedited element changed: {old} -> {new}')
                continue
            want = list(old)
            if 'start' in exp:
                length = exp.get('duration', old[1] - old[0])
                want[0], want[1] = exp['start'], exp['start'] + length
            elif 'duration' in exp:
                want[1] = old[0] + exp['duration']
            if 'lane' in exp:
                want[2] = exp['lane']
            if 'text' in exp:
                want[3] = exp['text']
            if 'vol' in exp:
                want[4] = f'{exp["vol"]}dB'
            if tuple(want) != new:
                problems.append(f'edited element has {new}, expected {tuple(want)}')
        return problems


def apply(xml, brief_path, out=None, partial=False, dry_run=False) -> dict:
    ap = Applier(xml, Path(brief_path).read_text(encoding='utf-8'))
    _, before = ap.snapshot()
    ap.run()
    report = {'xml': str(xml), 'brief': str(brief_path), 'applied': ap.applied, 'refused': ap.refused,
              'warnings': ap.warnings}
    if ap.refused and not partial:
        report['status'] = 'REFUSED'
        report['note'] = 'nothing written: fix or remove the refused edits (or pass --partial)'
        return report
    problems = ap.verify(before)
    report['verify'] = problems or 'only the edited elements changed, each to the requested values'
    if problems:
        report['status'] = 'FAIL'
        return report
    if dry_run or not ap.applied:
        report['status'] = 'DRY-RUN' if dry_run else 'NO-CHANGE'
        return report
    if not out:
        raise SystemExit('--out is required to write the result')
    report['receipt'] = xmlio.save_new(ap.tree, ap.envelope, xml, out)
    report['status'] = 'WRITTEN'
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('xml')
    p.add_argument('brief', help='the edited brief (made with timeline_brief.py --ids from this XML)')
    p.add_argument('--out', help='new .fcpxml / .fcpxmld to write')
    p.add_argument('--partial', action='store_true', help='apply what can be applied even if some edits are refused')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--report', help='write the JSON report here')
    p.add_argument('--ledger', help='record the write in this LEDGER.jsonl')
    args = p.parse_args(argv)
    try:
        rep = apply(args.xml, args.brief, args.out, args.partial, args.dry_run)
    except Refused as err:
        rep = {'status': 'REFUSED', 'note': str(err), 'applied': [], 'refused': []}
    if args.report:
        Path(args.report).write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    if rep.get('status') == 'WRITTEN' and args.ledger:
        from . import ledger
        ledger.record(args.ledger, 'brief_apply', 'write', vars(args), [args.xml, args.brief], [args.out])
    print(f"{rep['status']}: {len(rep['applied'])} applied, {len(rep['refused'])} refused"
          + (f" -> {args.out}" if rep['status'] == 'WRITTEN' else '') + (f" ({rep['note']})" if rep.get('note') else ''))
    for r in rep['refused']:
        print(f"  refused #{r.get('id', '-')}: {r['reason']}")
    for w in rep.get('warnings', []):
        print('  warning:', w)
    if isinstance(rep.get('verify'), list):
        for v in rep['verify']:
            print('  verify:', v)
    sys.exit(0 if rep['status'] in ('WRITTEN', 'DRY-RUN', 'NO-CHANGE') else 1)


if __name__ == '__main__':
    main()
