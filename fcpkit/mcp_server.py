#!/usr/bin/env python3
"""Read-only MCP server for fcpkit (stdio, stdlib only).

Exposes the checking tools to an MCP client (Claude Code, Claude Desktop, …).
v0 never writes an FCPXML, imports into Final Cut Pro or changes a library.
The one tool that writes files is fcpkit_filmstrip: PNGs go to a NEW temporary
folder (or a new folder you name, refused if it exists) and come back as images;
it is annotated readOnlyHint=false.

Tools:
  fcpkit_handles          transition handle + jump-cut-dissolve check
  fcpkit_effects_check    effect/template resolution against FCP's registries
  fcpkit_timeline_diff    what changed between two timelines
  fcpkit_relink_preview   where the media lives, what a prefix remap would find
  fcpkit_filmstrip        program-window filmstrip + waveform around timeline times
  fcpkit_libraries        libraries/events/projects open in the running FCP
                          (Apple events, read only, never launches FCP)

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP stdio).
Register with Claude Code:  claude mcp add fcpkit -- fcpkit-mcp
"""
from __future__ import annotations

import base64
import contextlib
import json
import sys
import tempfile
import traceback
from fractions import Fraction
from pathlib import Path

from . import __version__

PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
ANNOTATIONS = {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}


def _annotations(name: str) -> dict:
    # filmstrip creates PNGs in a new folder: not read-only, but never overwrites or deletes
    return {**ANNOTATIONS, 'readOnlyHint': False} if name == 'fcpkit_filmstrip' else ANNOTATIONS


def _sec(value) -> Fraction:
    return Fraction(str(value)).limit_denominator(10 ** 6)


# ---------------------------------------------------------------- tools

def tool_handles(xml: str, measure: bool = False) -> dict:
    from . import handles
    from .timeline import Timeline
    report = handles.check(Timeline(xml), measure=measure)
    report['exit_status'] = ('FAIL' if report['counts'].get('FAIL') else
                             'REVIEW' if report['counts'].get('REVIEW') else 'PASS')
    return report


def tool_effects_check(xml: str, templates_dir: str | None = None) -> dict:
    from . import effects_check
    report = effects_check.check(xml, templates_dir=templates_dir)
    report['exit_status'] = {0: 'PASS', 1: 'FAIL', 2: 'REVIEW'}[effects_check.exit_code(report)]
    return report


def tool_timeline_diff(a: str, b: str, caption_pattern: str | None = None, kinds: list | None = None) -> dict:
    from . import timeline_diff
    report = timeline_diff.diff(a, b, caption_pattern)
    if kinds:
        report['changes'] = [c for c in report['changes'] if c['kind'] in kinds]
    return report


def tool_relink_preview(xml: str, from_prefix: str | None = None, to_prefix: str | None = None) -> dict:
    from . import relink_preview
    if (from_prefix is None) != (to_prefix is None):
        raise ValueError('from_prefix and to_prefix go together')
    report = relink_preview.preview(xml, from_prefix, to_prefix)
    report['exit_status'] = {0: 'PASS', 1: 'FAIL', 2: 'REVIEW'}[relink_preview.exit_code(report)]
    return report


def tool_filmstrip(xml: str, at: list, pre: float = 0.6, post: float = 0.6, frames: int = 12,
                   out_dir: str | None = None) -> tuple[dict, list[Path]]:
    from . import filmstrip
    from .timeline import Timeline
    if not at:
        raise ValueError('at: give one or more timeline seconds')
    if out_dir:
        folder = Path(out_dir).expanduser()
        if folder.exists():
            raise FileExistsError(f'{folder} exists; give a new folder (nothing is overwritten)')
        folder.mkdir(parents=True)
    else:
        folder = Path(tempfile.mkdtemp(prefix='fcpkit-filmstrip-'))
    tl = Timeline(xml)
    pictures, metas = [], []
    for c in at[:8]:
        out = folder / f'program_{float(c):09.3f}.png'
        metas.append(filmstrip.program_strip(tl, _sec(c), _sec(pre), _sec(post), out, int(frames)))
        pictures.append(out)
    return {'folder': str(folder), 'pictures': [str(p) for p in pictures], 'strips': metas}, pictures


def tool_libraries(durations: bool = True, timeout: int = 20) -> dict:
    from . import fcp_libraries
    return fcp_libraries.snapshot(timeout=int(timeout), durations=bool(durations))


XML = {'type': 'string', 'description': 'path to a .fcpxml file or .fcpxmld bundle'}
TOOLS = {
    'fcpkit_handles': (tool_handles, 'Check every transition: does each side have the media it needs (multicam, '
                       'compound and retime aware), and does a dissolve sit on a jump cut (it would show removed '
                       'material)? Statuses PASS/INFO/REVIEW/FAIL; unknown is REVIEW, never PASS.',
                       {'xml': XML, 'measure': {'type': 'boolean', 'description': 'measure removed audio under '
                                                'jump-cut dissolves with ffmpeg'}}, ['xml']),
    'fcpkit_effects_check': (tool_effects_check, 'Check that every <effect> resource resolves: Motion templates '
                             '(src/uid files), FCP built-in templates, FxPlug UUIDs against FCP\'s own registry and '
                             'installed plug-ins. MISSING/REVIEW/OK/INFO per effect.',
                             {'xml': XML, 'templates_dir': {'type': 'string', 'description': 'user Motion '
                                                            'Templates folder (optional)'}}, ['xml']),
    'fcpkit_timeline_diff': (tool_timeline_diff, 'What changed between two timelines (e.g. a generated build vs '
                             'the editor\'s FCP export): unique-key matching, frame deltas of start/end/source, '
                             'lane and text changes, added/removed items.',
                             {'a': {**XML, 'description': 'baseline timeline'},
                              'b': {**XML, 'description': 'changed timeline'},
                              'caption_pattern': {'type': 'string', 'description': 'regex on title template names '
                                                  'that marks captions'},
                              'kinds': {'type': 'array', 'items': {'type': 'string'},
                                        'description': 'only these kinds: main, caption, title, overlay, audio'}},
                             ['a', 'b']),
    'fcpkit_relink_preview': (tool_relink_preview, 'Report each asset\'s media path, whether it exists (NFC/NFD '
                              'aware), bookmark presence, and what a whole-segment prefix remap would find. Never '
                              'rewrites the XML.',
                              {'xml': XML, 'from_prefix': {'type': 'string'}, 'to_prefix': {'type': 'string'}},
                              ['xml']),
    'fcpkit_filmstrip': (tool_filmstrip, 'Pictures of the main storyline around timeline times: evenly spaced '
                         'frames resolved through multicam angles/compounds, waveform underneath, red ticks at '
                         'cuts. Writes PNGs to a new folder and returns them as images (max 8 times).',
                         {'xml': XML, 'at': {'type': 'array', 'items': {'type': 'number'},
                                             'description': 'timeline seconds'},
                          'pre': {'type': 'number', 'default': 0.6}, 'post': {'type': 'number', 'default': 0.6},
                          'frames': {'type': 'integer', 'default': 12},
                          'out_dir': {'type': 'string', 'description': 'new folder for the PNGs (default: temp)'}},
                         ['xml', 'at']),
    'fcpkit_libraries': (tool_libraries, 'List the libraries, events and projects open in the running Final Cut '
                         'Pro (AppleScript, read only). Refuses if FCP is not running or two copies run; never '
                         'launches FCP.',
                         {'durations': {'type': 'boolean', 'default': True},
                          'timeout': {'type': 'integer', 'default': 20}}, []),
}


def tool_list() -> list[dict]:
    return [{'name': name, 'description': desc, 'annotations': _annotations(name),
             'inputSchema': {'type': 'object', 'properties': props, 'required': req, 'additionalProperties': False}}
            for name, (_, desc, props, req) in TOOLS.items()]


def call_tool(name: str, arguments: dict) -> dict:
    if name not in TOOLS:
        raise KeyError(name)
    fn, _, props, req = TOOLS[name]
    unknown = set(arguments) - set(props)
    missing = [k for k in req if k not in arguments]
    if unknown or missing:
        return {'content': [{'type': 'text', 'text': f'bad arguments: unknown {sorted(unknown)}, missing {missing}'}],
                'isError': True}
    try:
        with contextlib.redirect_stdout(sys.stderr):  # stdout belongs to the protocol
            result = fn(**arguments)
    except Exception as err:  # reported to the client, never a silent pass
        return {'content': [{'type': 'text', 'text': f'{type(err).__name__}: {err}'}], 'isError': True}
    images = []
    if isinstance(result, tuple):
        result, pictures = result
        images = [{'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(p.read_bytes()).decode()}
                  for p in pictures if p.is_file()]
    text = json.dumps(result, ensure_ascii=False, indent=1, default=str)
    return {'content': [{'type': 'text', 'text': text}] + images, 'isError': False}


# ---------------------------------------------------------------- protocol

def handle(message: dict) -> dict | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    method, mid = message.get('method'), message.get('id')
    if mid is None:  # notification (e.g. notifications/initialized)
        return None
    try:
        if method == 'initialize':
            asked = (message.get('params') or {}).get('protocolVersion')
            result = {'protocolVersion': asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                      'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'fcpkit', 'version': __version__},
                      'instructions': 'Read-only FCPXML / Final Cut Pro checks. Statuses: PASS, INFO, REVIEW '
                                      '(could not verify — not a pass), FAIL/MISSING.'}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': tool_list()}
        elif method == 'tools/call':
            params = message.get('params') or {}
            try:
                result = call_tool(params.get('name'), params.get('arguments') or {})
            except KeyError:
                return {'jsonrpc': '2.0', 'id': mid,
                        'error': {'code': -32602, 'message': f'unknown tool {params.get("name")!r}'}}
        else:
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'method not found: {method}'}}
    except Exception as err:  # protocol-level failure
        traceback.print_exc(file=sys.stderr)
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32603, 'message': f'{type(err).__name__}: {err}'}}
    return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def serve(stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            reply = handle(message) if isinstance(message, dict) else {
                'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'batches are not supported'}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + '\n')
            stdout.flush()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.parse_args(argv)
    serve()
    return 0


if __name__ == '__main__':
    sys.exit(main())
