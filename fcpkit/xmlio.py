"""Load and save FCPXML (.fcpxml or .fcpxmld) through lossless envelope I/O.

`fcpxml_io` keeps the outer envelope (XML declaration, DOCTYPE, trailing
bytes) byte-for-byte, refuses when the source changed since parsing, never
overwrites the source, refuses an existing output, and validates the result
against the installed FCP DTD. This module only adds bundle handling.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import fcpxml_io
from .timeline import xml_path


def load(path: str | Path):
    """(tree, envelope) of the XML inside `path` (bundle-aware)."""
    return fcpxml_io.parse_fcpxml(xml_path(Path(path)))


def save_new(tree, envelope, source: str | Path, output: str | Path) -> dict:
    """Write an edited tree to a NEW file or bundle; returns fcpxml_io's receipt."""
    source, output = Path(source).expanduser(), Path(output).expanduser()
    rendered = fcpxml_io.render_fcpxml(tree, envelope)
    if source.is_dir():
        if output.suffix != '.fcpxmld':
            raise ValueError('a bundle source needs a .fcpxmld output')
        if output.exists():
            raise FileExistsError(str(output))
        # copy the sidecars, then publish Info.fcpxml into the new bundle
        shutil.copytree(source, output, ignore=lambda d, names: ['Info.fcpxml'] if Path(d) == source else [])
        try:
            return fcpxml_io.publish_fcpxml(tree, output / 'Info.fcpxml', rendered)
        except Exception:
            shutil.rmtree(output)  # only the bundle this call just created
            raise
    return fcpxml_io.publish_fcpxml(tree, output, rendered)
