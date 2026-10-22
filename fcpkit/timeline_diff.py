#!/usr/bin/env python3
"""What changed between two timelines (our build vs the user's FCP export).

Items are matched in stages, most certain first, and a stage only pairs
items whose key is unique on both sides (no guessing between duplicates):
1. exact      same tag, ref, lane, timeline range, source start and text
2. text       titles with the same template and the same text
3. source     same tag, ref and source in-point (a clip that moved/was trimmed)
4. placement  same tag, ref, lane and timeline start
5. overlap    main-storyline clips of the same source whose source ranges
              overlap most (cut edits move both edges)
Everything left is added / removed.

For every pair the report gives frame deltas of start/end/source, lane and
text changes, grouped by kind (main, caption, title, overlay, audio). Lessons
from user edits come from this list, not from eyeballing.

A title counts as a caption when its template name matches --caption-pattern
(default $FCPKIT_CAPTION_PATTERN or `(?i)caption|subtitle|자막`, "자막" being
Korean for caption), e.g. --caption-pattern '^Lower Third Caption'.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote

from . import config
from .timeline import Timeline

NFC = lambda s: unicodedata.normalize('NFC', s or '')  # noqa: E731


def _text(el) -> str:
    return NFC(''.join(ts.text or '' for ts in el.iter('text-style'))).strip()


class Side:
    def __init__(self, tl: Timeline, caption_pattern: str | None = None):
        self.tl = tl
        self.caption_re = re.compile(caption_pattern or config.caption_pattern())
        self.items = [i for i in tl.items if i.tag not in ('transition',)]
        self.ref_name, self.ref_key = {}, {}
        for i in self.items:
            r = tl.resources.get(i.el.get('ref'))
            self.ref_name[i.index] = NFC(r.get('name')) if r is not None else ''
            self.ref_key[i.index] = self._identity(r)

    @staticmethod
    def _identity(r):
        """Resources are renumbered between exports and template uids change form
        (~/… vs …/<library>/Motion Templates…): identify media by file name,
        everything else by NFC name."""
        if r is None:
            return None
        if r.tag == 'asset':
            rep = r.find('media-rep')
            src = rep.get('src') if rep is not None else r.get('src')
            if src:
                return 'asset:' + NFC(unquote(src).rsplit('/', 1)[-1])
        return f'{r.tag}:' + NFC(r.get('name'))

    def kind(self, i) -> str:
        if i.in_main:
            return 'main'
        name = self.ref_name[i.index]
        if i.tag == 'title':
            return 'caption' if self.caption_re.search(name) else 'title'
        if i.tag in ('audio',) or i.lane < 0:
            return 'audio'
        return 'overlay'


def _keys(side: Side, i, stage: str):
    rk = side.ref_key[i.index]
    if stage == 'exact':
        return (i.tag, rk, i.lane, i.abs_start, i.abs_end, i.start, _text(i.el))
    if stage == 'text':
        return (i.tag, rk, _text(i.el)) if i.tag == 'title' and _text(i.el) else None
    if stage == 'source':
        return (i.tag, rk, i.start) if i.tag != 'title' and rk else None
    if stage == 'placement':
        return (i.tag, rk, i.lane, i.abs_start) if rk else None
    return None


def match(a: Side, b: Side) -> tuple[list, list, list]:
    left = {i.index: i for i in a.items}
    right = {i.index: i for i in b.items}
    pairs = []
    for stage in ('exact', 'text', 'source', 'placement'):
        ka, kb = defaultdict(list), defaultdict(list)
        for i in left.values():
            k = _keys(a, i, stage)
            if k is not None:
                ka[k].append(i)
        for i in right.values():
            k = _keys(b, i, stage)
            if k is not None:
                kb[k].append(i)
        for k in ka.keys() & kb.keys():
            if len(ka[k]) == len(kb[k]) == 1:
                x, y = ka[k][0], kb[k][0]
                pairs.append((x, y, stage))
                del left[x.index], right[y.index]
    # main storyline: same source, most overlapping source range, greedy and unique
    cand = []
    for x in left.values():
        if not x.in_main:
            continue
        for y in right.values():
            if y.in_main and a.ref_key[x.index] == b.ref_key[y.index] and a.ref_key[x.index]:
                ov = min(x.start + x.duration, y.start + y.duration) - max(x.start, y.start)
                if ov > 0:
                    cand.append((ov, x.index, y.index))
    for ov, xi, yi in sorted(cand, reverse=True):
        if xi in left and yi in right:
            pairs.append((left.pop(xi), right.pop(yi), 'overlap'))
    return pairs, list(left.values()), list(right.values())


def diff(path_a, path_b, caption_pattern: str | None = None) -> dict:
    A, B = Side(Timeline(path_a), caption_pattern), Side(Timeline(path_b), caption_pattern)
    f = A.tl.frame
    pairs, removed, added = match(A, B)
    fr = lambda v: float(v / f)  # noqa: E731
    changes, counts = [], Counter()
    for x, y, stage in pairs:
        d = {'kind': A.kind(x), 'stage': stage, 'tag': x.tag, 'name': NFC(x.name)[:40],
             'start': float(x.abs_start), 'd_start_f': fr(y.abs_start - x.abs_start),
             'd_end_f': fr(y.abs_end - x.abs_end), 'd_src_f': fr(y.start - x.start)}
        if x.lane != y.lane:
            d['lane'] = [x.lane, y.lane]
        tx, ty = _text(x.el), _text(y.el)
        if tx != ty:
            d['text'] = [tx, ty]
        if any(d[k] for k in ('d_start_f', 'd_end_f', 'd_src_f')) or 'lane' in d or 'text' in d:
            changes.append(d)
            counts[(d['kind'], 'changed')] += 1
        else:
            counts[(d['kind'], 'same')] += 1
    for side, items, label in ((A, removed, 'removed'), (B, added, 'added')):
        for i in items:
            counts[(side.kind(i), label)] += 1
            changes.append({'kind': side.kind(i), 'stage': label, 'tag': i.tag, 'name': NFC(i.name)[:40],
                            'start': float(i.abs_start), 'end': float(i.abs_end), 'lane': i.lane,
                            'text': _text(i.el)[:60]})
    changes.sort(key=lambda c: c['start'])
    summary = defaultdict(dict)
    for (kind, what), n in counts.items():
        summary[kind][what] = n
    return {'a': str(A.tl.xml), 'b': str(B.tl.xml), 'frame': str(f),
            'duration_a': float(A.tl.duration), 'duration_b': float(B.tl.duration),
            'summary': dict(summary), 'changes': changes}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('a', help='baseline (our build)')
    ap.add_argument('b', help='changed (the user export)')
    ap.add_argument('--out')
    ap.add_argument('--kind', action='append', help='print only these kinds')
    ap.add_argument('--caption-pattern', help='regex on title template names that marks captions')
    args = ap.parse_args(argv)
    rep = diff(args.a, args.b, args.caption_pattern)
    if args.out:
        Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    print(f"duration {rep['duration_a']:.2f}s -> {rep['duration_b']:.2f}s")
    for kind, c in sorted(rep['summary'].items()):
        print(f'  {kind:8} {c}')
    if args.kind:
        for c in rep['changes']:
            if c['kind'] in args.kind:
                print('   ', json.dumps(c, ensure_ascii=False))


if __name__ == '__main__':
    main()
