#!/usr/bin/env python3
"""Transition handle check: does each transition have the media it needs,
and does a dissolve show (or play) material the cut removed?

For every <transition> in any spine (main storyline and connected storylines):
- The cut is where the incoming element starts (or where the outgoing one
  ends when there is no incoming one). The transition covers
  [cut - before, cut + after]; it is measured where it really sits, not
  assumed centred (FCP exports off-centre transitions).
- The outgoing clip must have `after` of media past its out-point and the
  incoming clip `before` of media ahead of its in-point (video always; audio
  too when the transition carries an audio crossfade). FCP refuses an import
  whose transition reaches past the media ("Encountered an unexpected value").
- Media limits come from timeline.py: asset range, multicam angle segment,
  compound range. Titles, generators and stills are unlimited. Anything that
  cannot be resolved exactly is REVIEW, never PASS.

Cut kind:
- jump      both sides come from the same source (same asset / multicam /
            compound) but the source time jumps: the handles ARE the removed
            material, so the dissolve shows the cut.
- continuous same source, contiguous time (an angle switch): handles are fine.
- different different sources (B-roll, card): handles show unchosen frames.
- fade      one side is empty (fade in/out of a connected storyline).

`--measure` reads the removed audio under each jump handle (ffmpeg
volumedetect on the audio angle) so an audible removed word is a FAIL.
`--strips DIR` writes source filmstrip+waveform pictures of both handles.

Exit 0 = all PASS/INFO, 1 = any FAIL, 2 = REVIEW only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from . import config
from .timeline import Timeline, existing_path, round_frames

AUDIBLE_DB = -40.0  # max_volume above this in removed audio = audible


def _source_key(tl: Timeline, el) -> str | None:
    """Identity of the source timeline a clip's local time refers to."""
    if el is None:
        return None
    ref = el.get('ref')
    res = tl.resources.get(ref) if ref else None
    if res is None:
        return None
    return f'{res.tag}:{res.get("uid") or res.get("id")}'


def _volume(src: Path, t0: float, dur: float) -> dict | None:
    if dur <= 0:
        return None
    r = subprocess.run([config.ffmpeg(), '-hide_banner', '-nostats', '-ss', f'{max(t0, 0):.6f}', '-t', f'{dur:.6f}',
                        '-i', str(src), '-vn', '-af', 'volumedetect', '-f', 'null', '-'],
                       capture_output=True, text=True)
    mean = re.search(r'mean_volume:\s*(-?[\d.]+|-inf) dB', r.stderr)
    peak = re.search(r'max_volume:\s*(-?[\d.]+|-inf) dB', r.stderr)
    if not peak:
        return None
    conv = lambda m: float('-inf') if m.group(1) == '-inf' else float(m.group(1))  # noqa: E731
    return {'mean_db': conv(mean) if mean else None, 'max_db': conv(peak)}


def _side(tl, item, edge: str, need: Fraction, kind: str) -> dict:
    """Handle on one side. edge='tail' for the outgoing clip, 'head' for the incoming one."""
    f = tl.frame
    if item is None:
        return {'status': 'PASS', 'need': 0, 'note': 'no clip on this side'}
    if item.tag in ('title', 'caption') or (need == 0):
        return {'status': 'PASS', 'need_frames': float(need / f), 'note': 'generator' if need else 'nothing needed'}
    probe = item.start + item.duration - f / 2 if edge == 'tail' else item.start + f / 2
    res = tl.resolve(item, probe, kind)
    side = {'clip': item.name, 'tag': item.tag, 'need_frames': float(need / f),
            'file': str(res.file) if res.file else None, 'notes': res.notes}
    if res.unlimited:
        side.update(status='PASS', have_frames=None, note='unlimited (title/still/generator)')
        return side
    if res.no_material or res.lo is None:
        if kind == 'audio' and any('no audio' in n for n in res.notes):
            side.update(status='PASS', note='no audio on this side')
            return side
        side.update(status='REVIEW', note='media range unknown: ' + '; '.join(res.notes))
        return side
    have = (res.hi - (item.start + item.duration)) if edge == 'tail' else (item.start - res.lo)
    side['have_frames'] = float(have / f)
    if not res.exact:
        side.update(status='REVIEW', note='inexact mapping: ' + '; '.join(res.notes))
    elif have >= need:
        side['status'] = 'PASS'
    else:
        side.update(status='FAIL', note=f'has {float(have / f):.1f} frames, needs {float(need / f):.1f}')
    side['_have'] = have
    return side


def check(tl: Timeline, measure=False, strips: Path | None = None) -> dict:
    f = tl.frame
    rows = []
    for tr in [i for i in tl.items if i.tag == 'transition']:
        kids = [c for c in tr.spine if isinstance(c.tag, str) and c.get('lane') is None
                and c.tag not in ('spine',)] if tr.spine is not None else []
        k = kids.index(tr.el)
        out_el = kids[k - 1] if k > 0 and kids[k - 1].tag != 'transition' else None
        in_el = kids[k + 1] if k + 1 < len(kids) and kids[k + 1].tag != 'transition' else None
        out_it, in_it = tl.item_of(out_el), tl.item_of(in_el)
        if out_it is not None and out_it.tag == 'gap':
            out_it = None
        if in_it is not None and in_it.tag == 'gap':
            in_it = None
        cut = in_it.abs_start if in_it else (out_it.abs_end if out_it else tr.abs_start)
        before, after = cut - tr.abs_start, tr.abs_end - cut
        has_audio_fade = tr.el.find('filter-audio') is not None
        row = {'at': float(tr.abs_start), 'cut': float(cut), 'name': tr.name, 'lane': tr.lane,
               'main': tr.spine is tl.main, 'duration_frames': float(tr.duration / f),
               'before_frames': float(before / f), 'after_frames': float(after / f),
               'outgoing': out_it.name if out_it else None, 'incoming': in_it.name if in_it else None}
        sides = {'video_out': _side(tl, out_it, 'tail', after, 'video'),
                 'video_in': _side(tl, in_it, 'head', before, 'video')}
        if has_audio_fade:
            sides['audio_out'] = _side(tl, out_it, 'tail', after, 'audio')
            sides['audio_in'] = _side(tl, in_it, 'head', before, 'audio')
        # cut kind
        if out_it is None or in_it is None:
            kind = 'fade'
        elif _source_key(tl, out_it.el) and _source_key(tl, out_it.el) == _source_key(tl, in_it.el):
            out_src, in_src = out_it.start + out_it.duration, in_it.start
            kind = 'continuous' if out_src == in_src else 'jump'
            row['removed_seconds'] = float(in_src - out_src)
        else:
            kind = 'different'
        row['kind'] = kind
        # longest transition that fits at the current alignment, and centred
        haves = {n: s.get('_have') for n, s in sides.items()}
        out_have = min((v for n, v in haves.items() if n.endswith('_out') and v is not None), default=None)
        in_have = min((v for n, v in haves.items() if n.endswith('_in') and v is not None), default=None)
        if out_have is not None and in_have is not None:
            row['max_centred_frames'] = 2 * max(0, min(round_frames(out_have, f) if out_have >= 0 else 0,
                                                       round_frames(in_have, f) if in_have >= 0 else 0))
        for s in sides.values():
            s.pop('_have', None)
        row['sides'] = sides
        status = 'PASS'
        if any(s['status'] == 'FAIL' for s in sides.values()):
            status = 'FAIL'
        elif any(s['status'] == 'REVIEW' for s in sides.values()):
            status = 'REVIEW'
        if kind == 'jump':
            row['shows_cut'] = True
            status = 'FAIL' if status != 'REVIEW' else status
            row['note'] = ('dissolve over a jump cut shows removed material '
                           f'({float(after / f):.0f} f after the out-point, {float(before / f):.0f} f before the in-point)')
            if measure:
                row['removed_audio'] = _removed_audio(tl, out_it, in_it, before, after)
                loud = [v for v in row['removed_audio'].values() if v and v.get('max_db', -999) > AUDIBLE_DB]
                row['removed_audio_audible'] = bool(loud)
        elif kind == 'different' and status == 'PASS' and tr.spine is tl.main:
            status = 'INFO'
            row['note'] = 'dissolve between different sources: handles show unchosen frames — look at the strip'
        row['status'] = status
        if strips is not None and kind in ('jump', 'different'):
            row['strips'] = _strips(tl, out_it, in_it, before, after, strips, tr)
        rows.append(row)
    counts = {}
    for r in rows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    return {'xml': str(tl.xml), 'project': tl.project.get('name'), 'frame': f'{tl.frame}',
            'transitions': len(rows), 'counts': counts, 'rows': rows}


def _removed_audio(tl, out_it, in_it, before, after) -> dict:
    """Loudness of the removed audio the crossfade plays: after the out-point, before the in-point."""
    half = tl.frame / 2
    out = {}
    # resolve half a frame inside each edge, then step back out to the edge
    for label, item, probe, offset, dur in (
            ('out_tail', out_it, out_it.start + out_it.duration - half, half, after),
            ('in_head', in_it, in_it.start + half, -half - before, before)):
        if dur <= 0:
            continue
        res = tl.resolve(item, probe, 'audio')
        src = existing_path(res.file) if res.file else None
        if src is None or res.file_time is None:
            out[label] = None
            continue
        out[label] = _volume(src, float(res.file_time + offset), float(dur))
    return out


def _strips(tl, out_it, in_it, before, after, folder: Path, tr) -> dict:
    from . import filmstrip
    made = {}
    for label, item, edge, dur in (('out_tail', out_it, 'tail', after), ('in_head', in_it, 'head', before)):
        if item is None or dur <= 0:
            continue
        local = item.start + item.duration - tl.frame / 2 if edge == 'tail' else item.start + tl.frame / 2
        v, a = tl.resolve(item, local, 'video'), tl.resolve(item, local, 'audio')
        vsrc = existing_path(v.file) if v.file else None
        asrc = existing_path(a.file) if a.file else None
        if vsrc is None or v.file_time is None:
            continue
        # chosen frames up to the edge, then the handle (removed/unchosen) frames
        if edge == 'tail':
            t0, t1 = float(v.file_time + tl.frame / 2 - dur), float(v.file_time + tl.frame / 2 + dur)
        else:
            t0, t1 = float(v.file_time - tl.frame / 2 - dur), float(v.file_time - tl.frame / 2 + dur)
        name = f'tr_{float(tr.abs_start):09.3f}_{label}.png'
        try:
            # red tick = the clip's edge; frames past it on the tail side / before it on the head side are the handle
            filmstrip.source_strip(vsrc, t0, t1, folder / name, frames=8, audio=asrc, ticks=((t0 + t1) / 2,))
            made[label] = str(folder / name)
        except RuntimeError as exc:
            made[label] = f'error: {exc}'[:300]
    return made


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('xml')
    ap.add_argument('--measure', action='store_true', help='measure removed audio under jump-cut dissolves')
    ap.add_argument('--strips', help='folder for handle filmstrips (jump/different cuts)')
    ap.add_argument('--out', help='write the JSON report here')
    ap.add_argument('--all', action='store_true', help='print every row, not only non-PASS')
    args = ap.parse_args(argv)
    tl = Timeline(args.xml)
    report = check(tl, args.measure, Path(args.strips) if args.strips else None)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text)
    print(f"{report['project']}: {report['transitions']} transitions {report['counts']}")
    for r in report['rows']:
        if args.all or r['status'] not in ('PASS',):
            bad = {k: s.get('note', s['status']) for k, s in r['sides'].items() if s['status'] != 'PASS'}
            print(f"  {r['status']:6} {r['at']:9.3f}s lane {r['lane']} {r['kind']:10} {r['name']} "
                  f"{r['before_frames']:.0f}+{r['after_frames']:.0f}f max {r.get('max_centred_frames')} "
                  f"{r.get('note', '')} {bad if bad else ''}"
                  + (f" removed audio {r.get('removed_audio')}" if 'removed_audio' in r else ''))
    sys.exit(1 if report['counts'].get('FAIL') else 2 if report['counts'].get('REVIEW') else 0)


if __name__ == '__main__':
    main()
