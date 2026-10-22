#!/usr/bin/env python3
"""A readable brief of an FCPXML timeline — "decompiled" for people and AI.

A long FCPXML is mostly machine detail (ids, rational times, text styles,
effect blobs, bookmarks). Reading it raw costs time and things get missed.
This prints the editorial content instead:

- symbol table: every source file / multicam / compound gets a short alias
  (M1, M2 …) and every template / effect a T-alias, so long names appear once;
- one line per main-storyline cut (time, source + in-point, length, angle,
  volume, scale, effects), and under it everything that starts during that
  cut: captions and titles with their text, overlays, sound effects, music,
  transitions, markers — lane, time, length and the key values;
- per-minute headings, lane legend and density stats;
- a coverage line: every timed element of the XML is counted, and anything
  not shown is named (nothing is dropped silently).

`--json` writes the same rows with exact rational times and the XML path of
each element, so a reader can go from a brief line back to the XML.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote

from .timeline import Timeline, t

NFC = lambda s: unicodedata.normalize('NFC', s or '')  # noqa: E731
LABELS = {
    'en': {'legend': 'time = timeline mm:ss.cc · ▌= main-storyline cut (source @source time, [angle]) · indented = items '
                     'that start during that cut (lane +above/-below) · ⇄ transition · ◆ marker · vol/scale/fx = values · '
                     'kf = keyframes',
           'defaults': 'main-storyline defaults (omitted on the lines)', 'sources': 'Sources',
           'templates': 'Templates & effects', 'lanes': 'Lanes', 'lane_head': '| lane | items | most common |',
           'density': 'Density (per minute)', 'cuts': 'cuts', 'titles': 'titles/captions', 'below': 'lanes below (sound)',
           'transitions': 'transitions', 'shown': 'timed items shown', 'missing': 'not shown', 'all': '(all)',
           'markers': 'markers', 'range': 'range', 'range_items': 'timed items shown', 'nohost': 'no main-storyline host'},
    'ko': {'legend': '시각 = 타임라인 mm:ss.cc · ▌= 본선 컷(원본 @원본시각, [앵글]) · 들여쓴 줄 = 그 컷 동안 시작하는 항목 '
                     '(레인 +위/-아래) · ⇄ 트랜지션 · ◆ 마커 · vol/scale/fx = 붙은 값 · kf = 키프레임',
           'defaults': '본선 기본값(줄에서 생략)', 'sources': '원본', 'templates': '템플릿·효과', 'lanes': '레인',
           'lane_head': '| lane | 항목 | 많은 것 |', 'density': '밀도 (분당)', 'cuts': '컷', 'titles': '타이틀/자막',
           'below': '아래 레인(소리)', 'transitions': '트랜지션', 'shown': '표시한 시간 항목', 'missing': '빠진 것',
           'all': '(전부)', 'markers': '마커', 'range': '구간', 'range_items': '시간 항목 표시', 'nohost': '본선 없음'},
}
SKIP_PARAM = re.compile(r'^\s*[\[{]|^[A-Za-z0-9+/=]{40,}$')  # JSON programs, blobs


def tc(seconds: Fraction | float) -> str:
    s = float(seconds)
    sign = '-' if s < 0 else ''
    s = abs(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f'{sign}{int(h)}:{int(m):02d}:{sec:05.2f}' if h else f'{sign}{int(m):02d}:{sec:05.2f}'


def dur(seconds: Fraction) -> str:
    return f'{float(seconds):.2f}s'


class Brief:
    def __init__(self, tl: Timeline, detail: int = 2, lang: str = 'en'):
        self.tl, self.detail = tl, detail
        self.L = LABELS[lang]
        self.media_alias: dict[str, str] = {}
        self.media_info: dict[str, str] = {}
        self.fx_alias: dict[str, str] = {}
        self.fx_use = Counter()
        self.media_use = Counter()
        self.shown: set[int] = set()
        self.last_main_name = None
        self.main_defaults: set[str] = set()

    # ---------------------------------------------------------------- symbols
    def media(self, ref: str | None) -> str:
        if not ref:
            return '?'
        if ref not in self.media_alias:
            r = self.tl.resources.get(ref)
            self.media_alias[ref] = f'M{len(self.media_alias) + 1}'
            if r is None:
                info = f'unresolved ref {ref}'
            elif r.tag == 'asset':
                rep = r.find('media-rep')
                src = rep.get('src') if rep is not None else r.get('src')
                file = NFC(unquote(src).rsplit('/', 1)[-1]) if src else NFC(r.get('name'))
                kind = 'still' if t(r.get('duration')) == 0 else ('audio' if r.get('hasVideo') != '1' else 'video')
                length = '' if kind == 'still' else f', {float(t(r.get("duration"))):.1f}s'
                info = f'{file} ({kind}{length})'
            elif r.tag == 'media':
                mc = r.find('multicam')
                if mc is not None:
                    angles = [NFC(a.get('name')) for a in mc.findall('mc-angle')]
                    info = f'multicam "{NFC(r.get("name"))}" angles: ' + ', '.join(angles)
                else:
                    info = f'compound "{NFC(r.get("name"))}"'
            elif r.tag == 'effect':
                info = f'generator "{NFC(r.get("name"))}"'
            else:
                info = f'{r.tag} "{NFC(r.get("name"))}"'
            self.media_info[self.media_alias[ref]] = info
        self.media_use[self.media_alias[ref]] += 1
        return self.media_alias[ref]

    def fx(self, ref: str | None, fallback: str = '') -> str:
        r = self.tl.resources.get(ref) if ref else None
        name = NFC(r.get('name')) if r is not None else NFC(fallback) or '?'
        if name not in self.fx_alias:
            self.fx_alias[name] = f'T{len(self.fx_alias) + 1}'
        self.fx_use[name] += 1
        return self.fx_alias[name]

    # ----------------------------------------------------------------- values
    def angle(self, el) -> str:
        r = self.tl.resources.get(el.get('ref'))
        mc = r.find('multicam') if r is not None else None
        if mc is None:
            return ''
        names = {a.get('angleID'): NFC(a.get('name')) for a in mc.findall('mc-angle')}
        parts = []
        for s in el.findall('mc-source'):
            n = names.get(s.get('angleID'), s.get('angleID'))
            parts.append(n if s.get('srcEnable', 'all') == 'all' else f'{s.get("srcEnable")}:{n}')
        return '[' + ' / '.join(parts) + ']' if parts else ''

    def values(self, el) -> list[str]:
        out = []
        if el.get('enabled') == '0':
            out.append('OFF')
        for adj in el:
            if not isinstance(adj.tag, str):
                continue
            kf = ' kf' if adj.find('.//keyframe') is not None else ''
            if adj.tag == 'adjust-volume':
                fades = [f.tag for f in adj.iter() if f.tag in ('fadeIn', 'fadeOut')]
                amount = adj.get('amount')
                if amount or kf:
                    out.append(f'vol {amount or "0dB"}{kf}')
                if fades:
                    out.append('+'.join(fades))
            elif adj.tag == 'adjust-transform':
                bits = [f'{k} {adj.get(k)}' for k in ('scale', 'position', 'rotation', 'anchor') if adj.get(k)]
                if bits or kf:
                    out.append(' '.join(bits) + kf)
            elif adj.tag in ('adjust-blend', 'adjust-crop', 'adjust-conform', 'adjust-stabilization'):
                a = adj.get('amount') or adj.get('mode') or adj.get('type') or ''
                out.append(f'{adj.tag[7:]} {a}{kf}'.strip())
            elif adj.tag in ('filter-video', 'filter-audio'):
                label = self.fx(adj.get('ref'), adj.get('name', ''))
                if self.detail >= 3:
                    ps = [f'{NFC(p.get("name"))}={p.get("value")}' for p in adj.findall('param')
                          if p.get('value') and not SKIP_PARAM.search(p.get('value')) and len(p.get('value')) <= 24]
                    label += ('(' + ', '.join(ps[:3]) + ')') if ps else ''
                out.append(('afx ' if adj.tag == 'filter-audio' else 'fx ') + label
                           + (' kf' if adj.find('.//keyframe') is not None else ''))
            elif adj.tag == 'timeMap':
                pts = adj.findall('timept')
                if len(pts) == 2:
                    a, b = pts
                    span_t = t(b.get('time')) - t(a.get('time'))
                    speed = (t(b.get('value')) - t(a.get('value'))) / span_t if span_t else 0
                    out.append(f'speed x{float(speed):.2f}')
                else:
                    out.append(f'retime {len(pts)} pts')
            elif adj.tag == 'conform-rate' and adj.get('srcFrameRate'):
                out.append(f'conform {adj.get("srcFrameRate")}')
        if self.detail < 2:
            return [o for o in out if o == 'OFF']
        return out

    @staticmethod
    def text(el) -> str:
        raw = NFC(''.join(ts.text or '' for ts in el.iter('text-style')))
        lines = [' '.join(part.split()) for part in raw.splitlines()]
        return ' / '.join(x for x in lines if x)

    # ------------------------------------------------------------------ lines
    def describe(self, it) -> str:
        el, tag = it.el, it.tag
        vals = self.values(el)
        if tag == 'title':
            alias = self.fx(el.get('ref'))
            txt = self.text(el)
            body = f'{alias} "{txt}"' if txt else alias
        elif tag == 'transition':
            f = el.find('filter-video')
            body = '⇄ ' + (self.fx(f.get('ref'), f.get('name', '')) if f is not None else NFC(it.name))
            vals = []
        elif tag == 'gap':
            body = 'GAP'
        elif tag == 'caption':
            body = f'caption "{self.text(el)}"'
        else:
            r = self.tl.resources.get(el.get('ref'))
            if r is not None and r.tag == 'effect':
                body = 'gen ' + self.fx(el.get('ref'))
            else:
                body = self.media(el.get('ref'))
                still = r is not None and r.tag == 'asset' and t(r.get('duration')) == 0
                if it.start and r is not None and r.tag in ('asset', 'media') and not still:
                    body += f' @{tc(it.start)}'
                body += self.angle(el)
            name = NFC(it.name)
            ref_name = NFC(r.get('name')) if r is not None else ''
            if name and name != ref_name and not name.startswith(ref_name + ' '):
                if not it.in_main or name != self.last_main_name:
                    body += f' "{name[:40]}"'
                if it.in_main:
                    self.last_main_name = name
            if el.get('audioRole') and (tag == 'audio' or it.lane < 0):
                body += f' ({el.get("audioRole").split(".")[0]})'
        if it.in_main:
            vals = [v for v in vals if v not in self.main_defaults]
        return ' '.join([body] + vals)

    def build(self, start: Fraction | None = None, end: Fraction | None = None, minute_heads: bool = True):
        tl = self.tl
        main = [i for i in tl.main_items()]
        # group by the main-storyline cut that is on screen when the item starts (not by its anchor:
        # a connected storyline anchored at 0:00 can hold items far later in the video)
        import bisect
        slots = [m for m in main if m.tag != 'transition']
        starts = [m.abs_start for m in slots]
        under = defaultdict(list)
        for it in tl.items:
            if it.in_main:
                continue
            k = bisect.bisect_right(starts, it.abs_start) - 1
            under[slots[k].index if k >= 0 else -1].append(it)
        # values shared by most main-storyline cuts are stated once in the header
        clips = [m for m in main if m.tag not in ('transition', 'gap')]
        common = Counter(v for m in clips for v in set(self.values(m.el)))
        self.main_defaults = {v for v, n in common.items() if clips and n >= 0.6 * len(clips)}
        self.fx_use.clear()
        # markers on any element (main clips, titles, overlays), placed by timeline time
        marks = defaultdict(list)
        for it in tl.items:
            for mk in [x for x in it.el if x.tag in ('marker', 'chapter-marker')]:
                at = it.to_abs(t(mk.get('start')))
                k = bisect.bisect_right(starts, at) - 1
                marks[slots[k].index if k >= 0 else -1].append((at, mk))
        self.marker_count = sum(len(v) for v in marks.values())
        lines, rows = [], []
        minute = None
        lo = start if start is not None else Fraction(-1)
        hi = end if end is not None else tl.duration + 1
        for m in main:
            if m.abs_end <= lo or m.abs_start >= hi:
                continue
            if minute_heads and int(m.abs_start // 60) != minute:
                minute = int(m.abs_start // 60)
                lines.append(f'\n## {minute:02d}:00')
            if m.tag == 'transition':
                lines.append(f'{tc(m.abs_start)} {self.describe(m)} {dur(m.duration)}')
            else:
                lines.append(f'▌{tc(m.abs_start)} {self.describe(m)} ({dur(m.duration)})')
            self.shown.add(m.index)
            rows.append(self.row(m, 0))
            entries = [(c.abs_start, 0, c.lane, c) for c in under.get(m.index, [])]
            entries += [(at, 1, 0, mk) for at, mk in marks.get(m.index, [])]
            for at, kind, lane, obj in sorted(entries, key=lambda e: (e[0], e[1], e[2])):
                if kind == 1:
                    lines.append(f'  ◆ {tc(at)} {"chapter " if obj.tag == "chapter-marker" else ""}{NFC(obj.get("value"))}')
                    continue
                lines.append(f'  {obj.lane:+d} {tc(obj.abs_start)} {self.describe(obj)} {dur(obj.duration)}')
                self.shown.add(obj.index)
                rows.append(self.row(obj, 1))
        for c in under.get(-1, []):
            lines.append(f'  ? {tc(c.abs_start)} {self.describe(c)} ({self.L["nohost"]})')
            self.shown.add(c.index)
        return lines, rows

    def row(self, it, depth) -> dict:
        return {'start': str(it.abs_start), 'end': str(it.abs_end), 'lane': it.lane, 'tag': it.tag,
                'name': NFC(it.name), 'text': self.text(it.el) if it.tag in ('title', 'caption') else None,
                'depth': depth, 'path': it.path}

    # ------------------------------------------------------------------ stats
    def stats(self) -> list[str]:
        tl = self.tl
        mins = max(float(tl.duration) / 60, 1e-9)
        lanes = defaultdict(Counter)
        for it in tl.items:
            if it.in_main or it.tag == 'transition':
                continue
            key = self.fx_name(it) if it.tag == 'title' else (it.tag if it.lane >= 0 else 'audio')
            lanes[it.lane][key] += 1
        out = [f'\n## {self.L["lanes"]}', self.L['lane_head'], '|---|---|---|']
        for lane in sorted(lanes, reverse=True):
            c = lanes[lane]
            out.append(f'| {lane:+d} | {sum(c.values())} | ' + ', '.join(f'{k} {n}' for k, n in c.most_common(3)) + ' |')
        cuts = len([i for i in tl.main_items() if i.tag not in ('transition', 'gap')])
        titles = [i for i in tl.items if i.tag == 'title']
        sfx = [i for i in tl.items if not i.in_main and i.lane < 0]
        L = self.L
        n_tr = len([i for i in tl.items if i.tag == 'transition'])
        out += [f'\n## {L["density"]}',
                f'{L["cuts"]} {cuts / mins:.1f} · {L["titles"]} {len(titles) / mins:.1f} · {L["below"]} {len(sfx) / mins:.1f} '
                f'· {L["transitions"]} {n_tr / mins:.1f}']
        return out

    def fx_name(self, it) -> str:
        r = self.tl.resources.get(it.el.get('ref'))
        return NFC(r.get('name')) if r is not None else '?'


def render(path, start=None, end=None, detail=2, minute_heads=True, lang='en') -> tuple[str, list[dict]]:
    tl = Timeline(path)
    b = Brief(tl, detail, lang)
    L = b.L
    fps = 1 / tl.frame
    body, rows = b.build(start, end, minute_heads)
    head = [f'# {NFC(tl.project.get("name"))} — {tc(tl.duration)}, {float(fps):.3f} fps', L['legend']]
    if b.main_defaults:
        head.append(f'{L["defaults"]}: ' + ' · '.join(sorted(b.main_defaults)))
    head.append(f'\n## {L["sources"]}')
    head += [f'{a} = {info} ×{b.media_use[a]}' for a, info in b.media_info.items()]
    head.append(f'\n## {L["templates"]}')
    head += [f'{alias} = {name} ×{b.fx_use[name]}' for name, alias in b.fx_alias.items()]
    total = len(tl.items)
    if start is None and end is None:
        missing = Counter(tl.items[i].tag for i in range(total) if i not in b.shown)
        cover = (f'\n{L["shown"]} {len(b.shown)}/{total}' + (f' — {L["missing"]}: {dict(missing)}' if missing else f' {L["all"]}')
                 + f', {L["markers"]} {b.marker_count}')
    else:
        cover = f'\n{L["range"]} {tc(start or 0)}–{tc(end or tl.duration)}: {L["range_items"]} {len(b.shown)}'
    text = '\n'.join(head + body + b.stats() + [cover]) + '\n'
    return text, rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('xml')
    ap.add_argument('--from', dest='start', type=float, help='start second')
    ap.add_argument('--to', dest='end', type=float, help='end second')
    ap.add_argument('--detail', type=int, default=2, choices=(1, 2, 3),
                    help='1 = no values, 2 = key values (default), 3 = + effect parameters')
    ap.add_argument('--lang', default='en', choices=sorted(LABELS), help='labels language')
    ap.add_argument('--out', help='write the brief here (default stdout)')
    ap.add_argument('--json', help='write rows with exact times and XML paths here')
    args = ap.parse_args(argv)
    lim = lambda v: Fraction(v).limit_denominator(10 ** 6) if v is not None else None  # noqa: E731
    text, rows = render(args.xml, lim(args.start), lim(args.end), args.detail, lang=args.lang)
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
        print(f'{args.out}: {len(text.encode())} bytes, {text.count(chr(10))} lines')
    else:
        sys.stdout.write(text)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding='utf-8')


if __name__ == '__main__':
    main()
