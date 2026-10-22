"""Minimal, optional client for a local SpliceKit JSON-RPC bridge inside Final Cut Pro.

SpliceKit (a separate project) injects a JSON-RPC server into FCP on 127.0.0.1.
fcpkit only uses it read-only, to record which dialogs FCP shows during an
import (`dialog.detect`); it never clicks. Without the bridge every tool still
works and says that dialogs were not watched.
"""
from __future__ import annotations

import json
import socket

from . import config

HOST = '127.0.0.1'


def bridge_up(port: int | None = None) -> bool:
    try:
        with socket.create_connection((HOST, port or config.splicekit_port()), timeout=1):
            return True
    except OSError:
        return False


def call(method: str, params: dict | None = None, timeout: float = 60, port: int | None = None) -> dict:
    """One newline-delimited JSON-RPC 2.0 request; returns the decoded response object."""
    payload = json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params or {}, 'id': 1}).encode() + b'\n'
    with socket.create_connection((HOST, port or config.splicekit_port()), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        buf = b''
        while b'\n' not in buf:
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    if not buf.strip():
        raise ValueError(f'empty reply from SpliceKit for {method}')
    return json.loads(buf.split(b'\n')[0])
