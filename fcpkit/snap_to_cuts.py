#!/usr/bin/env python3
"""Snap caption / zoom starts (and optionally ends) onto main-storyline cuts.

Rule (learned from an editor's own fixes to generated timelines): a caption
or zoom start goes on the cut when the cut is at most 0.5 s before it and
speech does not continue across the cut. In that editor's timelines about
44 % of caption starts sat on cuts, so this is a tidy-up, not a law.

Items are chosen by a regex on "<template name> | <clip name>" (NFC), e.g.
--select '^Caption Lower' or, for a Korean template, --select '^하단 자막'.

Nothing is forced. Every selected item lands in exactly one bucket:
- moved        start (end) moved onto the cut
- on_cut       already on a cut
- no_cut       no cut inside the window
- skipped      with the reason: another caption spans the cut (speech
               continues), same-lane overlap, anchor would leave its host clip,
               not enough media head, inside a connected storyline, too short

Shared boundary: when the previous caption ends exactly where this one starts
and that boundary is at most --shared-max frames (default 3) after the cut,
the offset is timing slop, not speech: both edges move to the cut (the
previous caption is shortened, row `also_trimmed`). A larger gap with an
adjacent caption stays skipped for a person to judge by ear.

Edits mimic dragging the head in FCP: offset and start move by the same
amount and the duration grows, so anchored children keep their place.
Without --out it only reports. With --out it writes a NEW file/bundle through
fcpxml_io (lossless envelope, DTD validated, source never overwritten).
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import unicodedata
from fractions import Fraction
from pathlib import Path

from .timeline import Timeline, fmt, t

NFC = lambda s: unicodedata.normalize('NFC', s or '')  # noqa: E731


def label(tl: Timeline, item) -> str:
    ref = tl.resources.get(item.el.get('ref'))
    return NFC(ref.get('name') if ref is not None else '') + ' | ' + NFC(item.name)


def text_of(el) -> str:
    return NFC(''.join(ts.text or '' for ts in el.iter('text-style'))).strip()


def _sec(value: str) -> Fraction:
    return Fraction(value).limit_denominator(10 ** 6)


class Snapper:
    def __init__(self, tl: Timeline, select: str, guard: str | None, lanes: set[int] | None,
                 before: Fraction, after: Fraction, end_window: Fraction | None, shared_max: int = 3):
        self.tl, self.before, self.after, self.end_window = tl, before, after, end_window
        self.shared_max = shared_max * tl.frame
        rx, gx = re.compile(select), re.compile(guard or select)
        anchored = [i for i in tl.items if not i.in_main and i.tag != 'transition']
        self.selected = [i for i in anchored if rx.search(label(tl, i)) and (not lanes or i.lane in lanes)]
        self.guards = [i for i in anchored if gx.search(label(tl, i))]
        self.cuts = tl.cut_points()
        self.cut_set = set(self.cuts)
        # absolute lane occupancy: items anchored directly to main-storyline clips
        self.lane_items = [i for i in anchored if i.spine is None and i.parent is not None and i.parent.in_main]

    def _nearest(self, at: Fraction, back: Fraction, fwd: Fraction):
        k = bisect.bisect_left(self.cuts, at)
        options = []
        if k > 0 and at - self.cuts[k - 1] <= back:
            options.append(self.cuts[k - 1])
        if k < len(self.cuts) and 0 < self.cuts[k] - at <= fwd:
            options.append(self.cuts[k])
        return min(options, key=lambda c: abs(c - at)) if options else None

    def _blocked(self, item, lo: Fraction, hi: Fraction, cut: Fraction, trims: list | None = None) -> str | None:
        """Reason the range [lo, hi) can't be taken, or None. Appends shared-boundary trims to `trims`."""
        f = self.tl.frame
        shared = []
        for g in self.guards:
            if g is item or not (g.abs_start < hi and g.abs_end > lo):
                continue
            adjacent = (trims is not None and g.abs_end == item.abs_start and g.abs_start < cut
                        and item.abs_start - cut <= self.shared_max and g.spine is None
                        and cut - g.abs_start >= 2 * f)
            if not adjacent:
                return f'speech continues across the cut ({text_of(g.el)[:20] or g.name})'
            shared.append(g)
        for o in self.lane_items:
            if o is not item and o not in shared and o.lane == item.lane and o.abs_start < hi and o.abs_end > lo:
                return f'lane {item.lane} overlap with {o.name[:30]}'
        if trims is not None:
            trims.extend(shared)
        return None

    def plan(self) -> list[dict]:
        tl, f = self.tl, self.tl.frame
        rows = []
        for it in self.selected:
            row = {'name': it.name, 'text': text_of(it.el)[:40], 'lane': it.lane, 'tag': it.tag,
                   'start': float(it.abs_start), 'end': float(it.abs_end), 'path': it.path}
            rows.append(row)
            new_start, new_end = it.abs_start, it.abs_end
            if it.spine is not None:
                row.update(bucket='skipped', reason='inside a connected storyline (move the storyline in FCP)')
                continue
            if it.abs_start in self.cut_set:
                row['bucket'] = 'on_cut'
            else:
                cut = self._nearest(it.abs_start, self.before, self.after)
                if cut is None:
                    row['bucket'] = 'no_cut'
                else:
                    lo, hi = min(cut, it.abs_start), max(cut, it.abs_start)
                    trims = []
                    why = self._blocked(it, lo, hi, cut, trims) if cut < it.abs_start else None
                    if not why and trims:
                        row['also_trimmed'] = [{'name': g.name, 'text': text_of(g.el)[:30], 'end': float(g.abs_end),
                                                'new_end': float(cut)} for g in trims]
                        row['_trims'] = [(g, g.abs_end - cut) for g in trims]
                    if why:
                        row.update(bucket='skipped', reason=why, cut=float(cut))
                    else:
                        new_start = cut
            if self.end_window is not None and row.get('bucket') != 'skipped':
                end_cut = None if it.abs_end in self.cut_set else self._nearest(it.abs_end, self.end_window, self.end_window)
                if end_cut is not None and end_cut > new_start:
                    why = self._blocked(it, it.abs_end, end_cut, end_cut) if end_cut > it.abs_end else None
                    if why:
                        row['end_skipped'] = why
                    else:
                        new_end = end_cut
            if new_start == it.abs_start and new_end == it.abs_end:
                continue
            delta = it.abs_start - new_start           # > 0: head moves earlier
            host = it.parent
            new_offset = t(it.el.get('offset')) - delta
            if host is not None and not (host.start <= new_offset < host.start + host.duration):
                row.update(bucket='skipped', reason='anchor would leave its host clip (re-anchor in FCP)')
                continue
            if delta > 0 and it.tag not in ('title', 'caption') and it.el.get('ref') in tl.resources \
                    and tl.resources[it.el.get('ref')].tag == 'asset':
                res = tl.available(it, it.start + f / 2, 'video' if it.tag != 'audio' else 'audio')
                if not res.unlimited and (res.lo is None or it.start - res.lo < delta):
                    row.update(bucket='skipped', reason='not enough media before the in-point')
                    continue
            keep_start = False
            if it.start - delta < 0:
                if it.tag not in ('title', 'caption'):
                    row.update(bucket='skipped', reason='local start would go negative')
                    continue
                # a generator has no media to run out of: keep its start, its animation starts at the new head
                keep_start = True
                row['start_kept'] = True
            if new_end - new_start < 2 * f:
                row.update(bucket='skipped', reason='would be shorter than 2 frames')
                continue
            row.update(bucket='moved' if new_start != it.abs_start else row.get('bucket', 'on_cut'),
                       new_start=float(new_start), new_end=float(new_end),
                       head_frames=float(delta / f), tail_frames=float((new_end - it.abs_end) / f),
                       _edit=(it, delta, new_end - new_start, keep_start))
            if new_end != it.abs_end:
                row['end_moved'] = True
        return rows

    @staticmethod
    def apply(rows):
        for r in rows:
            edit = r.pop('_edit', None)
            if edit is None:
                continue
            it, delta, duration, keep_start = edit
            it.el.set('offset', fmt(t(it.el.get('offset')) - delta))
            if not keep_start:
                it.el.set('start', fmt(t(it.el.get('start')) - delta))
            it.el.set('duration', fmt(duration))
            for g, cut_back in r.pop('_trims', []):
                g.el.set('duration', fmt(t(g.el.get('duration')) - cut_back))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('xml')
    ap.add_argument('--select', required=True, help='regex on "<template name> | <clip name>" (NFC)')
    ap.add_argument('--guard', help='regex of items whose span means speech continues (default: --select)')
    ap.add_argument('--lane', type=int, action='append', help='only these lanes (repeatable)')
    ap.add_argument('--before', default='0.5', help='snap back to a cut at most this many seconds earlier')
    ap.add_argument('--after', default='0', help='snap forward to a cut at most this many seconds later')
    ap.add_argument('--end-window', help='also snap ends to a cut within ± this many seconds')
    ap.add_argument('--shared-max', type=int, default=3,
                    help='frames: an adjacent caption boundary this close after a cut moves with it (0 = off)')
    ap.add_argument('--out', help='write a NEW .fcpxml/.fcpxmld with the moves applied')
    ap.add_argument('--report', help='write the JSON report here')
    ap.add_argument('--ledger', help='record the write in this LEDGER.jsonl (fcpkit-ledger)')
    args = ap.parse_args(argv)

    if args.out:
        from . import xmlio
        tree, envelope = xmlio.load(args.xml)
        tl = Timeline(args.xml, tree)
    else:
        tl = Timeline(args.xml)
    snap = Snapper(tl, args.select, args.guard, set(args.lane) if args.lane else None,
                   _sec(args.before), _sec(args.after), _sec(args.end_window) if args.end_window else None,
                   args.shared_max)
    rows = snap.plan()
    buckets = {}
    for r in rows:
        buckets[r.get('bucket', 'no_cut')] = buckets.get(r.get('bucket', 'no_cut'), 0) + 1
    receipt = None
    if args.out:
        Snapper.apply(rows)
        receipt = xmlio.save_new(tree, envelope, args.xml, args.out)
        if args.ledger:
            from . import ledger
            ledger.record(args.ledger, 'snap_to_cuts', 'write', vars(args), [args.xml], [args.out])
    for r in rows:
        r.pop('_edit', None)
        r.pop('_trims', None)
    report = {'xml': str(tl.xml), 'selected': len(rows), 'buckets': buckets, 'cuts': len(snap.cuts),
              'rows': rows, 'receipt': receipt}
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f'{len(rows)} selected, {len(snap.cuts)} cuts: {buckets}' + (f' -> {args.out}' if args.out else ' (dry run)'))
    reasons = {}
    for r in rows:
        if r.get('bucket') == 'skipped':
            key = re.sub(r'\(.*', '', r['reason']).strip()
            reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print('skipped:', reasons)


if __name__ == '__main__':
    main()
