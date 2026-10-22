"""Machine-specific defaults, each overridable by an environment variable (and most by a CLI flag).

Nothing here points at a particular user's files. Defaults:

  FCPKIT_FCP_APP          the Final Cut Pro the live tools expect to be running
                          (default: /Applications/Final Cut Pro.app)
  FCPKIT_FCP_APPS         extra FCP .app paths to recognise, separated by ':'
                          (a SpliceKit-patched copy at ~/Applications/SpliceKit/ is
                          picked up automatically when it exists)
  FCPKIT_LOCK             cooperative lock file held while a tool drives FCP
                          (default: ~/Library/Application Support/fcpkit/FCP.lock)
  FCPKIT_SPLICEKIT_PORT   local SpliceKit JSON-RPC port, used read-only to record
                          dialogs (default 9876; the bridge is optional)
  FCPKIT_FFMPEG           ffmpeg binary (default: first on PATH, then Homebrew)
  FCPKIT_MOTION_TEMPLATES user Motion Templates folder
                          (default: ~/Movies/Motion Templates.localized)
  FCPKIT_CAPTION_PATTERN  regex on a title's template name that marks it as a caption
                          in timeline_diff (default matches "caption", "subtitle" and
                          the Korean "자막"; case-insensitive)
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

STOCK_APP = Path('/Applications/Final Cut Pro.app')
# SpliceKit (an injected JSON-RPC bridge for FCP) installs a patched copy here by default.
SPLICEKIT_APP = Path('~/Applications/SpliceKit/Final Cut Pro (SpliceKit).app').expanduser()
DEFAULT_CAPTION_PATTERN = r'(?i)caption|subtitle|자막'


def _env(name: str) -> str | None:
    value = os.environ.get(name, '').strip()
    return value or None


def fcp_app() -> Path:
    """The FCP .app a live step expects to be running."""
    value = _env('FCPKIT_FCP_APP')
    return Path(value).expanduser() if value else STOCK_APP


def fcp_apps() -> tuple[Path, ...]:
    """Every FCP .app this machine may run: the configured one, extras, the stock app,
    and a SpliceKit copy if one is installed. Order is stable, duplicates removed."""
    apps = [fcp_app()]
    extra = _env('FCPKIT_FCP_APPS')
    if extra:
        apps += [Path(p).expanduser() for p in extra.split(os.pathsep) if p.strip()]
    apps.append(STOCK_APP)
    if SPLICEKIT_APP.is_dir():
        apps.append(SPLICEKIT_APP)
    seen, out = set(), []
    for app in apps:
        key = str(app).rstrip('/')
        if key not in seen:
            seen.add(key)
            out.append(app)
    return tuple(out)


def lock_path() -> Path:
    value = _env('FCPKIT_LOCK')
    if value:
        return Path(value).expanduser()
    return Path('~/Library/Application Support/fcpkit/FCP.lock').expanduser()


def splicekit_port() -> int:
    return int(_env('FCPKIT_SPLICEKIT_PORT') or 9876)


def ffmpeg() -> str:
    return _env('FCPKIT_FFMPEG') or shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'


def motion_templates() -> Path:
    value = _env('FCPKIT_MOTION_TEMPLATES')
    return Path(value).expanduser() if value else Path.home() / 'Movies' / 'Motion Templates.localized'


def caption_pattern() -> str:
    return _env('FCPKIT_CAPTION_PATTERN') or DEFAULT_CAPTION_PATTERN
