"""FCPXML <timeMap> (speed change) mapping, exact and honest.

A timeMap is a list of <timept time="local" value="media" interp="..."/>.
`time` is the clip's own (local) time, `value` the media time it shows.

Supported exactly: linear segments. FCP writes interp="smooth2" when the
attribute is absent or for eased ramps; a two-point smooth2 map (what FCP
writes for a constant speed change) is treated as linear but flagged
`assumed_linear` so callers mark the result inexact. Anything else is
unsupported and listed in `reasons` — never silently treated as linear.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction


def _t(value: str) -> Fraction:
    if not value.endswith('s'):
        raise ValueError(f'bad time {value!r}')
    return Fraction(value[:-1])


@dataclass
class TimeMap:
    points: list[tuple[Fraction, Fraction, str]]
    reasons: list[str] = field(default_factory=list)
    assumed_linear: bool = False

    @property
    def supported(self) -> bool:
        return not self.reasons

    @classmethod
    def from_element(cls, el) -> 'TimeMap':
        pts, reasons = [], []
        for p in el.findall('timept'):
            try:
                pts.append((_t(p.get('time', '0s')), _t(p.get('value', '0s')), p.get('interp', 'smooth2')))
            except ValueError as exc:
                reasons.append(str(exc))
        tm = cls(points=pts, reasons=reasons)
        if len(pts) < 2:
            tm.reasons.append(f'{len(pts)} timept (need 2+)')
            return tm
        if any(b[0] <= a[0] for a, b in zip(pts, pts[1:])):
            tm.reasons.append('timept times not increasing')
        if any(b[1] < a[1] for a, b in zip(pts, pts[1:])):
            tm.reasons.append('reverse or backwards segment')
        if any(b[1] == a[1] for a, b in zip(pts, pts[1:])):
            tm.reasons.append('hold (freeze) segment')
        interps = {p[2] for p in pts[:-1]}
        if interps - {'linear'}:
            if len(pts) == 2 and interps <= {'smooth2', 'linear'}:
                tm.assumed_linear = True
            else:
                tm.reasons.append('non-linear interpolation: ' + ','.join(sorted(interps - {'linear'})))
        return tm

    def _segment(self, x: Fraction, key: int):
        pts = self.points
        for a, b in zip(pts, pts[1:]):
            if a[key] <= x <= b[key]:
                return a, b
        # extrapolate with the end segments (FCP holds the edge speed)
        return (pts[0], pts[1]) if x < pts[0][key] else (pts[-2], pts[-1])

    def media_time(self, local: Fraction) -> Fraction:
        if not self.supported:
            raise ValueError('; '.join(self.reasons))
        a, b = self._segment(local, 0)
        return a[1] + (local - a[0]) * (b[1] - a[1]) / (b[0] - a[0])

    def local_time(self, media: Fraction) -> Fraction:
        if not self.supported:
            raise ValueError('; '.join(self.reasons))
        a, b = self._segment(media, 1)
        return a[0] + (media - a[1]) * (b[0] - a[0]) / (b[1] - a[1])

    def speed_at(self, local: Fraction) -> Fraction:
        a, b = self._segment(local, 0)
        return (b[1] - a[1]) / (b[0] - a[0])
