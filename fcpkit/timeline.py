#!/usr/bin/env python3
"""Exact FCPXML timeline walker and source-media resolver (read only).

Every time is a Fraction of seconds. Absolute times are timeline times
(0 = the sequence start, independent of tcStart).

- `Timeline(path)` reads a .fcpxml file or a .fcpxmld bundle.
- `timeline.items` lists every timed element (clips, titles, gaps,
  transitions) with its absolute start/end, lane and container.
- `timeline.resolve(item, t_local, kind)` says which media file and file
  time a clip shows/plays at local time t (asset-clip, video, audio, clip,
  sync-clip, mc-clip angles, ref-clip compounds, linear timeMaps).
- `timeline.available(item, t_local, kind)` gives the local range around t
  that has real media (for transition handles). None = unlimited (titles,
  generators, stills); an empty answer means no material.

Unknown or unsupported mappings are reported in `Resolution.notes` and
`exact=False`; callers treat them as REVIEW, never as PASS.
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import fcpxml_io
from . import retime

TIMED = {'asset-clip', 'clip', 'mc-clip', 'ref-clip', 'sync-clip', 'video',
         'audio', 'title', 'caption', 'gap', 'transition'}
MEDIA_CLIPS = {'asset-clip', 'clip', 'mc-clip', 'ref-clip', 'sync-clip', 'video', 'audio'}
INF = None


def t(value: str | None, default: str = '0s') -> Fraction:
    raw = default if value is None else value
    if not raw.endswith('s'):
        raise ValueError(f'bad FCPXML time {raw!r}')
    return Fraction(raw[:-1])


def fmt(value: Fraction) -> str:
    return f'{value.numerator}/{value.denominator}s' if value.denominator != 1 else f'{value.numerator}s'


def frames(value: Fraction, frame: Fraction) -> Fraction:
    return value / frame


def round_frames(value: Fraction, frame: Fraction) -> int:
    """Nearest whole frame, halves away from zero (Python round() is banker's)."""
    q = value / frame
    sign = -1 if q < 0 else 1
    q = abs(q)
    whole = q.numerator // q.denominator
    return sign * (whole + (1 if q - whole >= Fraction(1, 2) else 0))


def url_to_path(src: str | None) -> Path | None:
    if not src:
        return None
    if src.startswith('file://'):
        return Path(unquote(urlparse(src).path))
    return Path(src)


def existing_path(path: Path | None) -> Path | None:
    """The path as written, or its NFC/NFD twin (Korean names differ by form)."""
    if path is None:
        return None
    for form in (None, 'NFC', 'NFD'):
        candidate = Path(unicodedata.normalize(form, str(path))) if form else path
        if candidate.exists():
            return candidate
    return None


def xml_path(path: Path) -> Path:
    path = Path(path).expanduser()
    if path.is_dir():
        inner = path / 'Info.fcpxml'
        if not inner.is_file():
            raise FileNotFoundError(f'{path} has no Info.fcpxml')
        return inner
    return path


@dataclass
class Item:
    el: ET.Element
    tag: str
    path: str
    parent: 'Item | None'          # host clip for anchored items / spine children of a nested spine
    spine: ET.Element | None       # spine the element sits in (None for anchored items)
    lane: int                      # 0 = main storyline; nested spine children inherit the spine lane
    abs_start: Fraction
    duration: Fraction
    start: Fraction                # local start (source in-point in the element's own time)
    in_main: bool                  # direct child of the sequence's main spine
    index: int = 0                 # order in self.items

    @property
    def abs_end(self) -> Fraction:
        return self.abs_start + self.duration

    @property
    def name(self) -> str:
        return self.el.get('name') or ''

    def to_local(self, absolute: Fraction) -> Fraction:
        return self.start + (absolute - self.abs_start)

    def to_abs(self, local: Fraction) -> Fraction:
        return self.abs_start + (local - self.start)


@dataclass
class Resolution:
    file: Path | None
    file_time: Fraction | None
    asset_id: str | None
    exact: bool = True
    notes: list[str] = field(default_factory=list)
    # local range (in the queried element's time) around t that has media; INF = unlimited
    lo: Fraction | None = None
    hi: Fraction | None = None
    unlimited: bool = False
    no_material: bool = False


class Timeline:
    def __init__(self, path: str | Path, tree: ET.ElementTree | None = None):
        """`tree`: an already parsed tree of the same file (e.g. from fcpxml_io, to edit and save it)."""
        self.source = Path(path).expanduser()
        self.xml = xml_path(self.source)
        self.tree = tree if tree is not None else fcpxml_io.safe_parse(self.xml)
        self.root = self.tree.getroot()
        self.resources = {n.get('id'): n for n in self.root.findall('./resources/*') if isinstance(n.tag, str)}
        projects = self.root.findall('.//project')
        if len(projects) != 1:
            raise ValueError(f'{self.xml}: expected exactly one project, found {len(projects)}')
        self.project = projects[0]
        self.sequence = self.project.find('sequence')
        self.main = self.sequence.find('spine')
        fmt_el = self.resources.get(self.sequence.get('format'))
        if fmt_el is None or not fmt_el.get('frameDuration'):
            raise ValueError('sequence format/frameDuration missing')
        self.frame = t(fmt_el.get('frameDuration'))
        self.duration = t(self.sequence.get('duration'))
        self.tc_start = t(self.sequence.get('tcStart'))
        self.items: list[Item] = []
        self.by_el: dict[int, Item] = {}
        self._walk(self.main, Fraction(0), Fraction(0), '/spine', None, 0, True)
        for i, item in enumerate(self.items):
            item.index = i

    # ----------------------------------------------------------------- walking
    def _walk(self, container, base, container_start, path, host, lane, is_main):
        counts: dict[str, int] = {}
        for node in container:
            if not isinstance(node.tag, str):
                continue
            counts[node.tag] = counts.get(node.tag, 0) + 1
            key = f'{path}/{node.tag}[{counts[node.tag]}]'
            if node.tag == 'spine':
                spine_lane = int(node.get('lane', '0'))
                self._walk(node, base + t(node.get('offset')) - container_start,
                           t(node.get('start')), key, host, spine_lane, False)
                continue
            if node.tag not in TIMED:
                continue
            in_spine = container.tag == 'spine'
            own_lane = node.get('lane')
            item_lane = int(own_lane) if own_lane is not None else (lane if in_spine else 0)
            absolute = base + t(node.get('offset')) - container_start
            item = Item(el=node, tag=node.tag, path=key, parent=host,
                        spine=container if in_spine else None, lane=item_lane,
                        abs_start=absolute, duration=t(node.get('duration')),
                        start=t(node.get('start')), in_main=is_main and in_spine and own_lane is None)
            self.items.append(item)
            self.by_el[id(node)] = item
            if node.tag in MEDIA_CLIPS or node.tag in ('title', 'gap'):
                # anchored children and connected storylines live in this element's local time
                self._walk(node, absolute, item.start, key, item, 0, False)

    def main_items(self) -> list[Item]:
        return [i for i in self.items if i.in_main]

    def neighbours(self, item: Item) -> tuple[ET.Element | None, ET.Element | None]:
        """Previous / next timed sibling in the same spine (transitions skipped)."""
        if item.spine is None:
            return None, None
        kids = [c for c in item.spine if isinstance(c.tag, str) and c.tag in TIMED and c.get('lane') is None]
        i = kids.index(item.el)
        prev = next((c for c in reversed(kids[:i]) if c.tag != 'transition'), None)
        nxt = next((c for c in kids[i + 1:] if c.tag != 'transition'), None)
        return prev, nxt

    def item_of(self, el: ET.Element | None) -> Item | None:
        return None if el is None else self.by_el.get(id(el))

    def cut_points(self) -> list[Fraction]:
        """Absolute times where one main-storyline element ends and the next begins."""
        main = [i for i in self.main_items() if i.tag != 'transition']
        return sorted({i.abs_start for i in main[1:]})

    # -------------------------------------------------------------- resolving
    def resolve(self, item: Item | ET.Element, local: Fraction, kind: str = 'video') -> Resolution:
        el = item.el if isinstance(item, Item) else item
        return self._resolve(el, local, kind, depth=0)

    def available(self, item: Item, local: Fraction, kind: str = 'video') -> Resolution:
        return self._resolve(item.el, local, kind, depth=0)

    def _asset_resolution(self, asset, local, notes, exact=True) -> Resolution:
        rep = asset.find("media-rep[@kind='original-media']")
        if rep is None:
            rep = asset.find('media-rep')
        src = url_to_path(rep.get('src') if rep is not None else asset.get('src'))
        a_start, a_dur = t(asset.get('start')), t(asset.get('duration'))
        still = a_dur == 0 or (asset.get('hasVideo') == '1' and asset.get('hasAudio') != '1'
                               and (src and src.suffix.lower() in {'.png', '.jpg', '.jpeg', '.psd', '.tif', '.tiff', '.heic'}))
        res = Resolution(file=src, file_time=local - a_start, asset_id=asset.get('id'), exact=exact, notes=notes)
        if still:
            res.unlimited, res.file_time = True, Fraction(0)
        else:
            res.lo, res.hi = a_start, a_start + a_dur
            if not (a_start <= local <= a_start + a_dur):
                res.no_material = True
                res.notes.append('outside asset media')
        return res

    def _resolve(self, el, local, kind, depth) -> Resolution:
        if depth > 6:
            return Resolution(None, None, None, exact=False, notes=['nesting too deep'])
        tag = el.tag
        notes: list[str] = []
        tm = el.find('timeMap')
        media_local = local
        exact = True
        if tm is not None:
            mapping = retime.TimeMap.from_element(tm)
            if not mapping.supported:
                return Resolution(None, None, None, exact=False, notes=['timeMap: ' + '; '.join(mapping.reasons)])
            if mapping.assumed_linear:
                notes.append('timeMap smooth2 two-point treated as linear')
                exact = False
            media_local = mapping.media_time(local)
        if tag in ('title', 'caption'):
            return Resolution(None, None, None, notes=['generator'], unlimited=True)
        if tag == 'gap':
            return Resolution(None, None, None, notes=['gap'], no_material=True)
        if tag == 'video' and el.get('ref') in self.resources and self.resources[el.get('ref')].tag == 'effect':
            return Resolution(None, None, None, notes=['generator'], unlimited=True)
        if tag in ('asset-clip', 'video', 'audio'):
            ref = self.resources.get(el.get('ref'))
            if ref is None:
                return Resolution(None, None, None, exact=False, notes=['unresolved ref'])
            if ref.tag == 'asset':
                if kind == 'audio' and tag == 'video':
                    # a clip's sound is an anchored <audio> under its <video> (it may carry a sync offset)
                    for a in el.findall('audio'):
                        a_off, a_dur = t(a.get('offset')), t(a.get('duration'))
                        if a_off <= media_local < a_off + a_dur:
                            res = self._resolve(a, t(a.get('start')) + (media_local - a_off), kind, depth + 1)
                            if res.lo is not None and not res.unlimited:
                                res.lo = a_off + (res.lo - t(a.get('start')))
                                res.hi = a_off + (res.hi - t(a.get('start')))
                            res.notes = notes + res.notes
                            return self._back_to_local(res, tm, local)
                if kind == 'video' and ref.get('hasVideo') != '1':
                    return Resolution(None, None, ref.get('id'), notes=['no video'], no_material=True)
                if kind == 'audio' and ref.get('hasAudio') != '1':
                    return Resolution(None, None, ref.get('id'), notes=['no audio'], no_material=True)
                res = self._asset_resolution(ref, media_local, notes, exact)
                return self._back_to_local(res, tm, local)
            if ref.tag == 'media':
                return self._media(ref, media_local, kind, depth, notes, exact, tm, local)
            return Resolution(None, None, None, exact=False, notes=[f'ref to {ref.tag}'])
        if tag in ('clip', 'sync-clip'):
            child = self._covering_child(el, media_local, kind)
            if child is None:
                return Resolution(None, None, None, exact=exact, notes=notes + ['no material in container'], no_material=True)
            c_el, c_local, c_lo, c_hi = child
            res = self._resolve(c_el, c_local, kind, depth + 1)
            res = self._shift(res, c_el, c_lo, c_hi)
            res.notes = notes + res.notes
            res.exact = res.exact and exact
            return self._back_to_local(res, tm, local)
        if tag in ('mc-clip', 'ref-clip'):
            ref = self.resources.get(el.get('ref'))
            if ref is None or ref.tag != 'media':
                return Resolution(None, None, None, exact=False, notes=['unresolved media ref'])
            angle = None
            if tag == 'mc-clip':
                angle = self._active_angle(el, kind)
            return self._media(ref, media_local, kind, depth, notes, exact, tm, local, angle)
        return Resolution(None, None, None, exact=False, notes=[f'unsupported {tag}'])

    def _active_angle(self, el, kind):
        for src in el.findall('mc-source'):
            enable = src.get('srcEnable', 'all')
            if enable == 'all' or enable == kind:
                return src.get('angleID')
        return None

    def _media(self, media, local, kind, depth, notes, exact, tm, query_local, angle=None) -> Resolution:
        multicam = media.find('multicam')
        if multicam is not None:
            angles = multicam.findall('mc-angle')
            chosen = next((a for a in angles if a.get('angleID') == angle), None)
            if chosen is None:
                return Resolution(None, None, None, exact=False,
                                  notes=notes + [f'mc angle {angle!r} not found for {kind}'])
            child = self._covering_child(chosen, local, kind, primary_only=False)
            if child is None:
                return Resolution(None, None, None, exact=exact,
                                  notes=notes + [f'angle {chosen.get("name")!r}: no material'], no_material=True)
            c_el, c_local, c_lo, c_hi = child
            res = self._resolve(c_el, c_local, kind, depth + 1)
            res = self._shift(res, c_el, c_lo, c_hi)
            res.notes = notes + [f'angle {chosen.get("name")}'] + res.notes
            res.exact = res.exact and exact
            mc_start = t(multicam.get('tcStart'))
            mc_end = mc_start + max((t(c.get('offset')) + t(c.get('duration'))
                                     for c in chosen if isinstance(c.tag, str) and c.get('duration')), default=Fraction(0))
            res.lo = max(res.lo, mc_start) if res.lo is not None else res.lo
            res.hi = min(res.hi, mc_end) if res.hi is not None else res.hi
            return self._back_to_local(res, tm, query_local)
        sequence = media.find('sequence')
        if sequence is not None:
            spine = sequence.find('spine')
            child = self._covering_child(spine, local, kind)
            seq_lo = t(sequence.get('tcStart'))
            seq_hi = seq_lo + t(sequence.get('duration'))
            if child is None:
                return Resolution(None, None, None, exact=exact, notes=notes + ['compound: no material'],
                                  no_material=True, lo=seq_lo, hi=seq_hi)
            c_el, c_local, c_lo, c_hi = child
            res = self._resolve(c_el, c_local, kind, depth + 1)
            # a compound's own frame range limits handles; inner gaps make black, which FCP still plays
            res.lo, res.hi, res.unlimited, res.no_material = seq_lo, seq_hi, False, res.no_material
            res.notes = notes + ['compound ' + (media.get('name') or '')] + res.notes
            res.exact = res.exact and exact
            return self._back_to_local(res, tm, query_local)
        return Resolution(None, None, None, exact=False, notes=notes + ['media without multicam/sequence'])

    def _covering_child(self, container, local, kind, primary_only=True):
        """Child of a spine/clip/angle that covers container-local time `local`.

        Returns (element, child-local time, container-local lo, container-local hi)."""
        best = None
        base = t(container.get('start')) if container.tag == 'spine' else Fraction(0)
        for c in container:
            if not isinstance(c.tag, str) or c.tag not in TIMED or c.tag == 'transition':
                continue
            if primary_only and c.get('lane') is not None:
                continue
            if kind == 'video' and c.tag == 'audio':
                continue
            off, dur = t(c.get('offset')), t(c.get('duration'))
            lo, hi = off - base, off - base + dur
            if lo <= local < hi or (local == hi and best is None and dur > 0 and c is list(container)[-1]):
                best = (c, t(c.get('start')) + (local - lo), lo, hi)
                if lo <= local < hi:
                    break
        if best is not None and best[0].tag == 'gap':
            return None
        return best

    def _shift(self, res: Resolution, child_el, c_lo, c_hi) -> Resolution:
        """Map a child's available range into the container's time.

        Media continues past the child's edge only inside the child element
        itself; the container shows the neighbour outside [c_lo, c_hi), so the
        handle is bounded by the child's own available media mapped back."""
        if res.unlimited or res.lo is None:
            return res
        c_start = t(child_el.get('start'))
        res.lo = c_lo + (res.lo - c_start)
        res.hi = c_lo + (res.hi - c_start)
        return res

    def _back_to_local(self, res: Resolution, tm, query_local) -> Resolution:
        if tm is None or res.unlimited or res.lo is None:
            return res
        mapping = retime.TimeMap.from_element(tm)
        try:
            res.lo, res.hi = mapping.local_time(res.lo), mapping.local_time(res.hi)
        except ValueError as exc:
            res.exact = False
            res.notes.append(f'timeMap inverse: {exc}')
            res.lo = res.hi = None
        return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('xml')
    ap.add_argument('--main-only', action='store_true')
    ap.add_argument('--resolve', type=float, help='resolve the main-storyline video at this timeline second')
    args = ap.parse_args(argv)
    tl = Timeline(args.xml)
    if args.resolve is not None:
        at = Fraction(args.resolve).limit_denominator(1000000)
        for it in tl.main_items():
            if it.tag != 'transition' and it.abs_start <= at < it.abs_end:
                for kind in ('video', 'audio'):
                    r = tl.resolve(it, it.to_local(at), kind)
                    print(kind, it.tag, it.name, r.file, float(r.file_time) if r.file_time is not None else None,
                          'lo', r.lo and float(r.lo), 'hi', r.hi and float(r.hi), r.notes)
        return
    rows = []
    for it in tl.items:
        if args.main_only and not it.in_main:
            continue
        rows.append({'tag': it.tag, 'name': it.name, 'lane': it.lane, 'start': float(it.abs_start),
                     'end': float(it.abs_end), 'main': it.in_main, 'path': it.path})
    json.dump({'project': tl.project.get('name'), 'frame': fmt(tl.frame), 'items': rows},
              sys.stdout, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
