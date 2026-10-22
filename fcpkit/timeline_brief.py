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
  transitions, markers — lane, time, length and the key values, keyframes
  with their times and values;
- compound clips on the main storyline are opened in place; compounds used
  as overlays list what they contain, indented under them;
- per-minute headings, lane legend and density stats (of the shown range);
- a coverage line: every timed element is counted, and anything not shown is
  named (nothing is dropped silently).

`--ids` adds `#n` to every element line and a header with the XML's sha256:
that brief can be edited and applied back with brief_apply.py.
`--json` writes the rows with exact rational times and XML paths.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote

from .timeline import TIMED, Timeline, t, xml_path

NFC = lambda s: unicodedata.normalize('NFC', s or '')  # noqa: E731
SKIP_PARAM = re.compile(r'^\s*[\[{]|^[A-Za-z0-9+/=]{40,}$')  # JSON programs, blobs
HEADER = '<!-- fcpkit-brief v2'
KF_SHOWN = 4

LABELS = {
    'en': {'legend': 'time = timeline mm:ss.cc · ▌= main-storyline cut (source @source time, [angle]) · indented = items '
                     'that start during that cut (lane +above/-below) · ⇄ transition · ◆ marker · ⊂ compound '
                     '(· = inside it) · vol/scale/fx = values · kf name: time=value → …',
           'defaults': 'main-storyline defaults (omitted on the lines)', 'sources': 'Sources',
           'templates': 'Templates & effects', 'lanes': 'Lanes', 'lane_head': '| lane | items | most common |',
           'density': 'Density (per minute)', 'cuts': 'cuts', 'titles': 'titles/captions', 'below': 'lanes below (sound)',
           'transitions': 'transitions', 'shown': 'timed items shown', 'missing': 'not shown', 'all': '(all)',
           'markers': 'markers', 'range': 'range', 'range_items': 'timed items shown', 'nohost': 'no main-storyline host',
           'opened': 'compound opened:', 'compound': 'compound, items inside:',
           'closed': 'inside compounds not opened', 'of_range': 'of the range'},
    'ko': {'legend': '시각 = 타임라인 mm:ss.cc · ▌= 본선 컷(원본 @원본시각, [앵글]) · 들여쓴 줄 = 그 컷 동안 시작하는 항목 '
                     '(레인 +위/-아래) · ⇄ 트랜지션 · ◆ 마커 · ⊂ 컴파운드(· = 그 안) · vol/scale/fx = 붙은 값 · '
                     'kf 이름: 시각=값 → …',
           'defaults': '본선 기본값(줄에서 생략)', 'sources': '원본', 'templates': '템플릿·효과', 'lanes': '레인',
           'lane_head': '| lane | 항목 | 많은 것 |', 'density': '밀도 (분당)', 'cuts': '컷', 'titles': '타이틀/자막',
           'below': '아래 레인(소리)', 'transitions': '트랜지션', 'shown': '표시한 시간 항목', 'missing': '빠진 것',
           'all': '(전부)', 'markers': '마커', 'range': '구간', 'range_items': '시간 항목 표시', 'nohost': '본선 없음',
           'opened': '컴파운드 펼침:', 'compound': '컴파운드, 안의 항목', 'closed': '펼치지 않은 컴파운드 안의 항목',
           'of_range': '구간 기준'},
}


def tc(seconds: Fraction | float) -> str:
    s = float(seconds)
    sign = '-' if s < 0 else ''
    s = abs(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f'{sign}{int(h)}:{int(m):02d}:{sec:05.2f}' if h else f'{sign}{int(m):02d}:{sec:05.2f}'


def parse_tc(text: str) -> Fraction:
    """Inverse of tc(): 'mm:ss.cc' or 'h:mm:ss.cc' (also plain seconds) -> Fraction seconds."""
    parts = text.strip().split(':')
    value = Fraction(0)
    for p in parts:
        value = value * 60 + Fraction(p)
    return value


def dur(seconds: Fraction) -> str:
    return f'{float(seconds):.2f}s'


def file_sha256(path) -> str:
    return hashlib.sha256(xml_path(Path(path)).read_bytes()).hexdigest()


class Brief:
    def __init__(self, tl: Timeline, detail: int = 2, lang: str = 'en', ids: bool = False,
                 open_overlays: bool = True):
        self.tl, self.detail, self.ids, self.open_overlays = tl, detail, ids, open_overlays
        self.L = LABELS[lang]
        self.media_alias: dict[str, str] = {}
        self.media_info: dict[str, str] = {}
        self.fx_alias: dict[str, str] = {}
        self.fx_use = Counter()
        self.media_use = Counter()
        self.shown: set[int] = set()
        self.last_main_name = None
        self.closed_inner = 0
        self.next_index = len(tl.items)
        self.all_items = list(tl.items)
        self.main_defaults: set[str] = set()
        self.rows: list[dict] = []

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

    def keyframes(self, params, it, label: str) -> list[str]:
        """'kf <label>.<param>: 00:07.01=1 1 → 00:08.21=1.3 1.3' for every animated param."""
        if self.detail < 2 or it is None:
            return []
        out = []
        for p in params:
            anim = p.find('keyframeAnimation')
            if anim is None:
                continue
            kfs = anim.findall('keyframe')
            pts = [f'{tc(it.to_abs(t(k.get("time"))))}={k.get("value")}' for k in kfs[:KF_SHOWN]]
            more = f' (+{len(kfs) - KF_SHOWN})' if len(kfs) > KF_SHOWN else ''
            name = NFC(p.get('name'))
            out.append(f'kf {label}{"." + name if name and name != label else ""}: ' + ' → '.join(pts) + more)
        return out

    def values(self, el, it=None) -> list[str]:
        out = []
        if el.get('enabled') == '0':
            out.append('OFF')
        for adj in el:
            if not isinstance(adj.tag, str):
                continue
            if adj.tag == 'adjust-volume':
                fades = [f.tag for f in adj.iter() if f.tag in ('fadeIn', 'fadeOut')]
                if adj.get('amount'):
                    out.append(f'vol {adj.get("amount")}')
                out += self.keyframes(adj.iter('param'), it, 'vol')
                if fades:
                    out.append('+'.join(fades))
            elif adj.tag == 'adjust-transform':
                bits = [f'{k} {adj.get(k)}' for k in ('scale', 'position', 'rotation', 'anchor') if adj.get(k)]
                if bits:
                    out.append(' '.join(bits))
                out += self.keyframes(adj.iter('param'), it, 'transform')
            elif adj.tag in ('adjust-blend', 'adjust-crop', 'adjust-conform', 'adjust-stabilization'):
                a = adj.get('amount') or adj.get('mode') or adj.get('type') or ''
                out.append(f'{adj.tag[7:]} {a}'.strip())
                out += self.keyframes(adj.iter('param'), it, adj.tag[7:])
            elif adj.tag in ('filter-video', 'filter-audio'):
                label = self.fx(adj.get('ref'), adj.get('name', ''))
                if self.detail >= 3:
                    ps = [f'{NFC(p.get("name"))}={p.get("value")}' for p in adj.findall('param')
                          if p.get('value') and not SKIP_PARAM.search(p.get('value')) and len(p.get('value')) <= 24]
                    label += ('(' + ', '.join(ps[:3]) + ')') if ps else ''
                out.append(('afx ' if adj.tag == 'filter-audio' else 'fx ') + label)
                out += self.keyframes(adj.iter('param'), it, label.split('(')[0])
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
        # parameters set directly on a title/generator can be animated too
        if el.tag in ('title', 'video') and self.detail >= 2:
            out += self.keyframes(el.findall('param'), it, 'param')
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
        vals = self.values(el, it)
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
                seq = self.compound_sequence(it)
                if seq is not None and not getattr(it, 'expanded', 0):
                    n = sum(1 for x in seq.iter() if x.tag in TIMED and x.tag != 'transition')
                    if not self.open_overlays:
                        self.closed_inner += n  # opened ones are counted item by item instead
                    body += f' [{self.L["compound"]} {n}]'
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

    def tag_id(self, it, editable: bool = True) -> str:
        """Record a row; in --ids mode return ' #n'. n is the element's timeline index, the same
        whatever range is shown, so a range brief can be applied too."""
        n = it.index
        self.rows.append({'id': n, 'start': str(it.abs_start), 'end': str(it.abs_end), 'lane': it.lane,
                          'tag': it.tag, 'name': NFC(it.name),
                          'text': self.text(it.el) if it.tag in ('title', 'caption') else None,
                          'path': it.path, 'main': it.in_main, 'editable': editable, '_item': it})
        return f' #{n}' if self.ids else ''

    # ------------------------------------------------------------- compounds
    def compound_sequence(self, it):
        r = self.tl.resources.get(it.el.get('ref')) if it.tag == 'ref-clip' else None
        return r.find('sequence') if r is not None and r.tag == 'media' else None

    def compound_items(self, it) -> list:
        """Timed items inside a compound clip, mapped to this timeline's time and clipped to what
        the compound shows. Inner main-storyline items keep in_main=True."""
        seq = self.compound_sequence(it)
        sub = Timeline.__new__(Timeline)
        sub.resources, sub.items, sub.by_el = self.tl.resources, [], {}
        r = self.tl.resources.get(it.el.get('ref'))
        sub._walk(seq.find('spine'), it.abs_start, it.start, f'{it.path}>{NFC(r.get("name"))}', None, 0, True)
        inner = [i for i in sub.items if i.abs_end > it.abs_start and i.abs_start < it.abs_end]
        for i in inner:
            i.index = self.next_index
            i.inside = it
            self.next_index += 1
        return inner

    def flatten(self, items, depth=0):
        """Main storyline with compound clips opened: (main list, inner non-main items)."""
        out, extra = [], []
        for m in items:
            out.append(m)
            if m.tag == 'ref-clip' and depth < 4 and self.compound_sequence(m) is not None:
                inner = self.compound_items(m)
                m.expanded = sum(1 for i in inner if i.in_main and i.tag not in ('transition', 'gap'))
                sub_main, sub_extra = self.flatten([i for i in inner if i.in_main], depth + 1)
                out += sub_main
                extra += [i for i in inner if not i.in_main] + sub_extra + [i for i in sub_main]
        return out, extra

    def overlay_lines(self, it, indent: str, depth: int = 0) -> list[str]:
        """Contents of an overlay compound, indented under its line (read only: no ids)."""
        seq = self.compound_sequence(it)
        if not self.open_overlays or seq is None:
            return []
        if depth > 3:
            self.closed_inner += sum(1 for x in seq.iter() if x.tag in TIMED and x.tag != 'transition')
            return []
        it.opened = 1
        lines = []
        inner = self.compound_items(it)
        self.all_items += inner
        for c in sorted(inner, key=lambda i: (i.abs_start, i.lane)):
            mark = '▌' if c.in_main else f'{c.lane:+d} '
            lines.append(f'{indent}· {mark}{tc(c.abs_start)} {self.describe(c)} {dur(c.duration)}')
            self.shown.add(c.index)
            self.tag_id(c, editable=False)
            lines += self.overlay_lines(c, indent + '  ', depth + 1)
        return lines

    def build(self, start: Fraction | None = None, end: Fraction | None = None, minute_heads: bool = True):
        tl = self.tl
        self.next_index = len(tl.items)
        main, extra = self.flatten(tl.main_items())
        self.all_items = tl.items + [i for i in extra if i.index >= len(tl.items) and not i.in_main] \
            + [m for m in main if m.index >= len(tl.items)]
        # group by the main-storyline cut on screen when the item starts (not by its anchor:
        # a connected storyline anchored at 0:00 can hold items far later in the video)
        slots = [m for m in main if m.tag != 'transition' and not getattr(m, 'expanded', 0)]
        starts = [m.abs_start for m in slots]
        under = defaultdict(list)
        for it in list(self.all_items):
            if it.in_main:
                continue
            k = bisect.bisect_right(starts, it.abs_start) - 1
            under[slots[k].index if k >= 0 else -1].append(it)
        # values shared by most main-storyline cuts are stated once in the header
        clips = [m for m in slots if m.tag != 'gap']
        common = Counter(v for m in clips for v in set(self.values(m.el)))
        self.main_defaults = {v for v, n in common.items() if clips and n >= 0.6 * len(clips)}
        self.fx_use.clear()
        # markers on any element, placed by timeline time
        marks = defaultdict(list)
        for it in list(self.all_items):
            for mk in [x for x in it.el if x.tag in ('marker', 'chapter-marker')]:
                at = it.to_abs(t(mk.get('start')))
                k = bisect.bisect_right(starts, at) - 1
                marks[slots[k].index if k >= 0 else -1].append((at, mk))
        self.marker_count = 0
        lines = []
        minute = None
        lo = start if start is not None else Fraction(-1)
        hi = end if end is not None else tl.duration + 1
        self.in_range: list = []
        for m in main:
            if m.abs_end <= lo or m.abs_start >= hi:
                continue
            if minute_heads and int(m.abs_start // 60) != minute:
                minute = int(m.abs_start // 60)
                lines.append(f'\n## {minute:02d}:00')
            if getattr(m, 'expanded', 0):
                lines.append(f'⊂{tc(m.abs_start)} {self.describe(m)} ({dur(m.duration)}) — {self.L["opened"]} '
                             f'{m.expanded} {self.L["cuts"]}{self.tag_id(m, editable=False)}')
                self.shown.add(m.index)
                continue
            if m.tag == 'transition':
                lines.append(f'{tc(m.abs_start)} {self.describe(m)} {dur(m.duration)}{self.tag_id(m, False)}')
            else:
                editable = not hasattr(m, 'inside')
                lines.append(f'▌{tc(m.abs_start)} {self.describe(m)} ({dur(m.duration)}){self.tag_id(m, editable)}')
            self.shown.add(m.index)
            self.in_range.append(m)
            entries = [(c.abs_start, 0, c.lane, c) for c in under.get(m.index, [])]
            entries += [(at, 1, 0, mk) for at, mk in marks.get(m.index, [])]
            for at, kind, lane, obj in sorted(entries, key=lambda e: (e[0], e[1], e[2])):
                if kind == 1:
                    self.marker_count += 1
                    lines.append(f'  ◆ {tc(at)} {"chapter " if obj.tag == "chapter-marker" else ""}{NFC(obj.get("value"))}')
                    continue
                editable = not hasattr(obj, 'inside')
                lines.append(f'  {obj.lane:+d} {tc(obj.abs_start)} {self.describe(obj)} {dur(obj.duration)}'
                             f'{self.tag_id(obj, editable)}')
                self.shown.add(obj.index)
                self.in_range.append(obj)
                lines += self.overlay_lines(obj, '    ')
        if start is None and end is None:
            for c in under.get(-1, []):
                lines.append(f'  ? {tc(c.abs_start)} {self.describe(c)} ({self.L["nohost"]}){self.tag_id(c, False)}')
                self.shown.add(c.index)
        return lines

    # ------------------------------------------------------------------ stats
    def stats(self, items, seconds: Fraction, ranged: bool) -> list[str]:
        L = self.L
        mins = max(float(seconds) / 60, 1e-9)
        lanes = defaultdict(Counter)
        for it in items:
            if it.in_main or it.tag == 'transition':
                continue
            r = self.tl.resources.get(it.el.get('ref'))
            key = (NFC(r.get('name')) if r is not None else '?') if it.tag == 'title' else \
                (it.tag if it.lane >= 0 else 'audio')
            lanes[it.lane][key] += 1
        scope = f' ({L["of_range"]})' if ranged else ''
        out = [f'\n## {L["lanes"]}{scope}', L['lane_head'], '|---|---|---|']
        for lane in sorted(lanes, reverse=True):
            c = lanes[lane]
            out.append(f'| {lane:+d} | {sum(c.values())} | ' + ', '.join(f'{k} {n}' for k, n in c.most_common(3)) + ' |')
        cuts = len([i for i in items if i.in_main and i.tag not in ('transition', 'gap')
                    and not getattr(i, 'expanded', 0)])
        titles = [i for i in items if i.tag == 'title']
        sfx = [i for i in items if not i.in_main and i.lane < 0]
        n_tr = len([i for i in items if i.tag == 'transition'])
        out += [f'\n## {L["density"]}{scope}',
                f'{L["cuts"]} {cuts / mins:.1f} · {L["titles"]} {len(titles) / mins:.1f} · {L["below"]} {len(sfx) / mins:.1f} '
                f'· {L["transitions"]} {n_tr / mins:.1f}']
        return out


def build_brief(path, start=None, end=None, detail=2, minute_heads=True, lang='en', ids=False,
                open_overlays=True, tree=None) -> tuple[str, Brief]:
    tl = Timeline(path, tree)
    b = Brief(tl, detail, lang, ids, open_overlays)
    L = b.L
    body = b.build(start, end, minute_heads)
    fps = 1 / tl.frame
    head = []
    if ids:
        span = (f' from={start}' if start is not None else '') + (f' to={end}' if end is not None else '')
        head.append(f'{HEADER} sha256={file_sha256(path)} detail={detail} lang={lang} '
                    f'overlays={"open" if open_overlays else "closed"}{span} -->')
    head += [f'# {NFC(tl.project.get("name"))} — {tc(tl.duration)}, {float(fps):.3f} fps', L['legend']]
    if b.main_defaults:
        head.append(f'{L["defaults"]}: ' + ' · '.join(sorted(b.main_defaults)))
    head.append(f'\n## {L["sources"]}')
    head += [f'{a} = {info} ×{b.media_use[a]}' for a, info in b.media_info.items()]
    head.append(f'\n## {L["templates"]}')
    head += [f'{alias} = {name} ×{b.fx_use[name]}' for name, alias in b.fx_alias.items()]
    ranged = start is not None or end is not None
    if ranged:
        lo, hi = start or Fraction(0), end or tl.duration
        stats = b.stats(b.in_range, hi - lo, True)
        cover = f'\n{L["range"]} {tc(lo)}–{tc(hi)}: {L["range_items"]} {len(b.shown)}'
    else:
        stats = b.stats(b.all_items, tl.duration, False)
        total = len(b.all_items)
        missing = Counter(i.tag for i in b.all_items if i.index not in b.shown)
        cover = (f'\n{L["shown"]} {len(b.shown)}/{total}'
                 + (f' — {L["missing"]}: {dict(missing)}' if missing else f' {L["all"]}')
                 + f', {L["markers"]} {b.marker_count}'
                 + (f'; {L["closed"]} {b.closed_inner}' if b.closed_inner else ''))
    text = '\n'.join(head + body + stats + [cover]) + '\n'
    return text, b


def render(path, start=None, end=None, detail=2, minute_heads=True, lang='en', ids=False,
           open_overlays=True) -> tuple[str, list[dict]]:
    text, b = build_brief(path, start, end, detail, minute_heads, lang, ids, open_overlays)
    return text, [{k: v for k, v in r.items() if k != '_item'} for r in b.rows]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('xml')
    ap.add_argument('--from', dest='start', type=float, help='start second')
    ap.add_argument('--to', dest='end', type=float, help='end second')
    ap.add_argument('--detail', type=int, default=2, choices=(1, 2, 3),
                    help='1 = no values, 2 = key values + keyframes (default), 3 = + effect parameters')
    ap.add_argument('--lang', default='en', choices=sorted(LABELS), help='labels language')
    ap.add_argument('--ids', action='store_true', help='add #n to element lines (for brief_apply.py)')
    ap.add_argument('--closed-overlays', action='store_true', help='do not list what overlay compounds contain')
    ap.add_argument('--out', help='write the brief here (default stdout)')
    ap.add_argument('--json', help='write rows with exact times and XML paths here')
    args = ap.parse_args(argv)
    lim = lambda v: Fraction(v).limit_denominator(10 ** 6) if v is not None else None  # noqa: E731
    text, rows = render(args.xml, lim(args.start), lim(args.end), args.detail, lang=args.lang, ids=args.ids,
                        open_overlays=not args.closed_overlays)
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
        print(f'{args.out}: {len(text.encode())} bytes, {text.count(chr(10))} lines')
    else:
        sys.stdout.write(text)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding='utf-8')


if __name__ == '__main__':
    main()
