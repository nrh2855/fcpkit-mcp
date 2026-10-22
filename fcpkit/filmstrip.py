#!/usr/bin/env python3
"""Filmstrip + waveform pictures from SOURCE media (no FCP, no render).

Two uses:
- `source`  — one source range: N frames across [t0, t1] of a video file, the
  audio waveform of the same range underneath (from the same or another file,
  e.g. the multicam audio angle).
- `program` — a timeline window: frames and audio of the main storyline across
  [T-pre, T+post], following every cut inside the window (each frame resolved
  through mc-clip angles / compounds), with a red tick at each cut. Use it for
  a join or a caption start to see a clipped word onset or a jump.

Picture layout (ffmpeg only): frames tiled in one row, waveform drawn over a
dark plate (showwavespic is transparent; on white PNG viewers it vanishes),
stacked under the row. A source without audio falls back to frames only.
Labels are not burnt in (this ffmpeg has no drawtext); every picture gets a
JSON sidecar with the times, files and cut positions.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

from . import config
from .timeline import Timeline, existing_path

PLATE = '0x101014'
TRACE = '0x00E5FF'


def _run(cmd):
    # the last argument is always the output file: make it absolute so a name like '-x.png' is never an option
    cmd = list(cmd[:-1]) + [os.path.abspath(cmd[-1])]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f'ffmpeg failed ({r.returncode}): ' + r.stderr[-1500:])
    return r


def _frame(src: Path, at: float, height: int, out: Path):
    _run([config.ffmpeg(), '-v', 'error', '-y', '-ss', f'{max(at, 0):.6f}', '-i', str(src), '-frames:v', '1',
          '-vf', f'scale=-2:{height}', str(out)])


def _black(width: int, height: int, out: Path):
    _run([config.ffmpeg(), '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}',
          '-frames:v', '1', str(out)])


def _tile(frames: list[Path], out: Path):
    inputs = []
    for f in frames:
        inputs += ['-i', str(f)]
    n = len(frames)
    graph = ''.join(f'[{i}:v]scale=-2:ih,setsar=1[f{i}];' for i in range(n))
    graph += ''.join(f'[f{i}]' for i in range(n)) + f'hstack=inputs={n}[out]' if n > 1 else '[f0]copy[out]'
    _run([config.ffmpeg(), '-v', 'error', '-y', *inputs, '-filter_complex', graph, '-map', '[out]', '-frames:v', '1', str(out)])


def _size(png: Path) -> tuple[int, int]:
    data = png.read_bytes()[16:24]
    return int.from_bytes(data[:4], 'big'), int.from_bytes(data[4:], 'big')


def _waveform(segments: list[tuple[Path, float, float]], width: int, height: int, out: Path,
              ticks: list[float] = ()) -> bool:
    """Concatenate audio segments (file, t0, dur) and draw one waveform; red ticks at x fractions."""
    segments = [s for s in segments if s[0] is not None and s[2] > 0]
    if not segments:
        return False
    inputs, graph = [], ''
    for i, (src, t0, dur) in enumerate(segments):
        inputs += ['-ss', f'{max(t0, 0):.6f}', '-t', f'{dur:.6f}', '-i', str(src)]
        graph += f'[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{i}];'
    graph += ''.join(f'[a{i}]' for i in range(len(segments)))
    graph += f'concat=n={len(segments)}:v=0:a=1[cat];' if len(segments) > 1 else 'anull[cat];'
    graph += (f'color=c={PLATE}:s={width}x{height}[plate];'
              f'[cat]showwavespic=s={width}x{height}:colors={TRACE}:scale=sqrt[trace];'
              f'[plate][trace]overlay=format=auto[w0]')
    last = 'w0'
    for k, x in enumerate(ticks):
        px = max(0, min(width - 2, int(round(x * width))))
        graph += f';[{last}]drawbox=x={px}:y=0:w=2:h={height}:color=red@0.9:t=fill[w{k + 1}]'
        last = f'w{k + 1}'
    try:
        _run([config.ffmpeg(), '-v', 'error', '-y', *inputs, '-filter_complex', graph, '-map', f'[{last}]',
              '-frames:v', '1', str(out)])
    except RuntimeError:
        return False
    return True


def _stack(top: Path, bottom: Path, out: Path):
    _run([config.ffmpeg(), '-v', 'error', '-y', '-i', str(top), '-i', str(bottom), '-filter_complex',
          '[0:v][1:v]vstack=inputs=2[o]', '-map', '[o]', '-frames:v', '1', str(out)])


def source_strip(video: Path | None, t0: float, t1: float, out: Path, frames: int = 8, height: int = 180,
                 audio: Path | None = None, wave_height: int = 90, ticks: tuple = ()) -> dict:
    """Frames of `video` across [t0, t1] (inclusive ends) + waveform of `audio` (default: same file).

    `ticks` are source times to mark with a red line on the waveform."""
    out = Path(out)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        times = [t0 + (t1 - t0) * i / max(frames - 1, 1) for i in range(frames)]
        pics = []
        for i, at in enumerate(times):
            p = tmp / f'f{i:03d}.png'
            if video is not None:
                _frame(video, at, height, p)
            else:
                _black(height * 16 // 9, height, p)
            pics.append(p)
        row = tmp / 'row.png'
        _tile(pics, row)
        width, _ = _size(row)
        wave = tmp / 'wave.png'
        audio = audio or video
        marks = [(x - t0) / (t1 - t0) for x in ticks if t0 < x < t1]
        has_wave = _waveform([(audio, t0, t1 - t0)], width, wave_height, wave, marks) if audio else False
        out.parent.mkdir(parents=True, exist_ok=True)
        if has_wave:
            _stack(row, wave, out)
        else:
            shutil.copyfile(row, out)
    meta = {'kind': 'source', 'video': str(video) if video else None, 'audio': str(audio) if audio else None,
            't0': t0, 't1': t1, 'frame_times': times, 'ticks': list(ticks), 'waveform': has_wave}
    out.with_suffix('.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def program_strip(tl: Timeline, center: Fraction, pre: Fraction, post: Fraction, out: Path,
                  frames: int = 12, height: int = 160, wave_height: int = 90) -> dict:
    """Main-storyline frames + audio across [center-pre, center+post], following cuts."""
    a, b = max(Fraction(0), center - pre), min(tl.duration, center + post)
    main = [i for i in tl.main_items() if i.tag != 'transition']

    def covering(at):
        return next((i for i in main if i.abs_start <= at < i.abs_end), None)

    samples = [a + (b - a) * Fraction(k, max(frames - 1, 1)) for k in range(frames)]
    samples[-1] = b - tl.frame / 2  # stay inside the window's last frame
    rows, segs, cuts = [], [], [c for c in tl.cut_points() if a < c < b]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pics = []
        for k, at in enumerate(samples):
            item = covering(at)
            p = tmp / f'f{k:03d}.png'
            res = tl.resolve(item, item.to_local(at), 'video') if item else None
            src = existing_path(res.file) if res and res.file else None
            if src is not None:
                _frame(src, float(res.file_time), height, p)
            else:
                _black(height * 16 // 9, height, p)
            pics.append(p)
            rows.append({'t': float(at), 'item': item.name if item else None, 'file': str(src) if src else None,
                         'file_time': float(res.file_time) if res and res.file_time is not None else None,
                         'notes': res.notes if res else ['no main item']})
        # audio: split the window at cuts and resolve each piece's audio angle
        edges = [a] + cuts + [b]
        for lo, hi in zip(edges, edges[1:]):
            item = covering((lo + hi) / 2)
            if item is None:
                continue
            res = tl.resolve(item, item.to_local(lo), 'audio')
            src = existing_path(res.file) if res.file else None
            if src is not None and res.file_time is not None:
                segs.append((src, float(res.file_time), float(hi - lo)))
        row = tmp / 'row.png'
        _tile(pics, row)
        width, _ = _size(row)
        wave = tmp / 'wave.png'
        ticks = [float((c - a) / (b - a)) for c in cuts]
        has_wave = _waveform(segs, width, wave_height, wave, ticks)
        out.parent.mkdir(parents=True, exist_ok=True)
        if has_wave:
            _stack(row, wave, out)
        else:
            shutil.copyfile(row, out)
    meta = {'kind': 'program', 'xml': str(tl.xml), 'center': float(center), 'window': [float(a), float(b)],
            'cuts': [float(c) for c in cuts], 'frames': rows, 'waveform': has_wave,
            'note': 'red ticks on the waveform = cuts; frames are evenly spaced across the window'}
    Path(out).with_suffix('.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def _sec(value: str) -> Fraction:
    return Fraction(value).limit_denominator(10 ** 6)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('source', help='one source range')
    s.add_argument('video')
    s.add_argument('t0', type=float)
    s.add_argument('t1', type=float)
    s.add_argument('--audio')
    s.add_argument('--frames', type=int, default=8)
    s.add_argument('--out', required=True)
    p = sub.add_parser('program', help='timeline window(s) of the main storyline')
    p.add_argument('xml')
    p.add_argument('--at', action='append', default=[], help='timeline seconds (repeatable)')
    p.add_argument('--cuts', action='store_true', help='one picture per main-storyline cut')
    p.add_argument('--pre', default='0.6')
    p.add_argument('--post', default='0.6')
    p.add_argument('--frames', type=int, default=12)
    p.add_argument('--out-dir', required=True)
    args = ap.parse_args(argv)
    if args.cmd == 'source':
        meta = source_strip(Path(args.video), args.t0, args.t1, Path(args.out), args.frames,
                            audio=Path(args.audio) if args.audio else None)
        print(json.dumps(meta, ensure_ascii=False))
        return
    tl = Timeline(args.xml)
    centers = [_sec(x) for x in args.at] + (tl.cut_points() if args.cuts else [])
    out_dir = Path(args.out_dir)
    for c in centers:
        name = f'program_{float(c):09.3f}.png'
        meta = program_strip(tl, c, _sec(args.pre), _sec(args.post), out_dir / name, args.frames)
        print(out_dir / name, 'cuts', meta['cuts'], 'wave', meta['waveform'])


if __name__ == '__main__':
    main()
