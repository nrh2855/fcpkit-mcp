# fcpkit-mcp

Exact, safety-first tools for **Final Cut Pro** timelines (FCPXML) on macOS.

fcpkit reads `.fcpxml` files and `.fcpxmld` bundles with exact rational time,
resolves what every clip really shows and plays — through **multicam angles,
compound clips, sync clips and retimes** — and uses that to check the
things FCP gets wrong silently: transitions that run past the media,
dissolves over jump cuts, effects and templates that will go missing on import,
media that will not relink. A guarded importer puts an XML into the library
you name, and nowhere else.

- **Python 3.9+, standard library only.** `ffmpeg` is needed only for pictures and loudness.
- **Never overwrites a source.** Every write goes to a new file, is validated against the FCP DTD that ships inside your Final Cut Pro, and is refused if the source changed since it was read.
- **Unknown is REVIEW, never PASS.** A mapping fcpkit cannot resolve exactly is reported as such; exit code 2 means "could not verify".

> Status: alpha (0.1.0). Developed against Final Cut Pro 12.3 and 12.4 on macOS, FCPXML 1.11–1.14.

## Install

```bash
pip install .            # from a checkout (installs the fcpkit-* commands and fcpkit-mcp)
```

Every tool is also runnable as a module: `python3 -m fcpkit.handles X.fcpxmld`.

## Tools

Exit codes for the checkers: **0** pass, **1** defect found, **2** could not verify (REVIEW).

### Read first: the brief

`fcpkit-brief X.fcpxmld [--from 60 --to 120] [--detail 1|2|3] [--lang en|ko] [--out brief.md] [--json rows.json]`

Raw FCPXML is machine detail — ids, rational times, text-style definitions, effect blobs, bookmarks. Reading it costs a model (or a person) a lot of time and things get missed. The brief "decompiles" it into the edit:

- a symbol table: every source / multicam / compound becomes `M1, M2 …`, every template or effect `T1, T2 …`, so long names appear once;
- one line per main-storyline cut — `▌00:07.01 M1 @05:36.90 [angle] vol -15dB (1.94s)` — and under it everything that *starts* during that cut: captions and titles with their text, overlays, sounds, music, transitions, markers, with lane, time, length and key values (volume, scale/position, effects, speed, keyframes);
- values shared by most cuts are stated once in the header, not repeated;
- a lane table, per-minute density, and a coverage line: `timed items shown 1413/1413 (all)` — anything not shown is named.

On two real 15-minute multicam/longform timelines: 1.52 MB → 69 KB and 1.16 MB → 61 KB, every timed item shown. `--json` keeps exact rational times and the XML path of each row, so you can go from a brief line back to the element. Over MCP this is `fcpkit_brief` — have the agent call it before reading any XML.

### Checks (read only)

| Command | What it does |
|---|---|
| `fcpkit-handles X.fcpxmld [--measure] [--strips DIR]` | For every transition (main storyline and connected storylines) measures where it really sits — FCP exports off-centre transitions — and whether each side has the media it needs, resolving multicam angles, compounds and linear retimes. Classifies the cut as **jump** (same source, time jumps), continuous, different source, or fade. **A dissolve over a jump cut shows the material the cut removed → FAIL.** `--measure` reports how loud the removed audio under it is (ffmpeg `volumedetect`); `--strips` writes filmstrips of both handles. Reports the longest centred transition that would fit. |
| `fcpkit-filmstrip program X --at 61.7 --out-dir out/` | A row of frames + waveform (red ticks = cuts) of the main storyline around a timeline time, each frame resolved through angles/compounds. `--cuts` makes one per cut. `fcpkit-filmstrip source FILE t0 t1 --out p.png` does the same for a raw source range. A JSON sidecar records every time and file. |
| `fcpkit-effects X.fcpxmld` | Checks every `<effect>` resource: user Motion templates (`src` and the `~/Movies` file, and whether they are the same file), templates embedded in a library, FCP built-in templates (looked up inside FCP's own `MotionEffect.fxp`), and `FxPlug:<UUID>` effects against FCP's internal plug-in list and installed third-party plug-ins. A template uid without `src`, or a bare UUID without the `FxPlug:` prefix, is MISSING (FCP drops those silently). |
| `fcpkit-relink-preview X [--from P --to Q]` | Where each asset's media lives, whether it exists now (NFC/NFD-aware: Korean, Japanese and accented names come in both forms), whether it carries a bookmark, grouped by volume; with `--from/--to`, what a whole-path-segment prefix remap would find. Never edits the XML — relinking is done in FCP (File › Relink Files). |
| `fcpkit-diff A.fcpxmld B.fcpxmld [--kind caption]` | What changed between two timelines, e.g. your generated build and the editor's FCP export. Matching runs in stages from most to least certain (identical → same text → same source in-point → same placement → most-overlapping source range) and only pairs keys that are unique on both sides. Reports frame deltas of start/end/source, lane and text changes, added/removed items, grouped by kind. |
| `fcpkit-timeline X [--resolve 61]` | The shared base: every timed element with absolute start/end and lane; `--resolve` shows which file and file time the main storyline shows and plays at a timeline second. |

### Edits (always to a new file)

| Command | What it does |
|---|---|
| `fcpkit-snap X --select REGEX [--out NEW]` | Moves caption / zoom starts back onto a main-storyline cut when the cut is at most `--before` (0.5 s) earlier and speech does not continue across it. Every selected item lands in one bucket: moved, on_cut, no_cut, or skipped with a reason (another caption spans the cut, same-lane overlap, anchor would leave its host, not enough media). Without `--out` it only reports. `--select` matches `"<template name> \| <clip name>"`, e.g. `--select '^Lower Third'` or, for a Korean template, `--select '^하단 자막'`. |

### Final Cut Pro (live) and safety rails

| Command | What it does |
|---|---|
| `fcpkit-libraries running \| list \| diff` | Which FCP copy is running — identified by **executable path**, because several copies (stock, an older version, a SpliceKit-patched copy) share the bundle id `com.apple.FinalCutApp` — and, through FCP's official AppleScript dictionary, the libraries, events and projects it has open. Read only; never launches FCP; refuses when two copies run. |
| `fcpkit-import X.fcpxmld --library L.fcpbundle --dry-run` | Imports into the library you name, guarded. It writes an import **copy** whose first child is `<import-options>` (library location, `copy assets` 0, `suppress warnings` 0), then — only after every check passes — opens it with the running FCP, polls until the projects appear in a new event, records any FCP dialog (via an optional SpliceKit bridge; it never clicks), and writes a JSON receipt. `--dry-run` runs the offline checks, writes the copy and prints the live steps. |
| `fcpkit-plan make \| check` | Binds an approval to one exact job: plan id = sha256 of (action, XML content hash, library, event, app, validity window). A rebuilt XML, another library or an expired window → refused. |
| `fcpkit-ledger list \| verify \| undo \| record` | Append-only write ledger (JSONL) with input/output sha256 (bundles hashed whole). Undo never deletes: it moves outputs into `undone/` and refuses if a file changed since. |
| `fcpkit-watch-export --dir D [--against BUILD]` | Waits for a fresh FCP "Export XML" by content hash (stable across two polls, newest wins) and optionally diffs it against your build. |
| `fcpkit-mcp` | MCP server (see below). |

Import checks, all before any Apple event: lock file free; plan id matches;
every `/Volumes/<name>` holding media is mounted; exactly one FCP running and
it is the planned app; target library is **open** in FCP (path match is case-,
slash- and NFC/NFD-insensitive); no open library modified in the last 10
minutes (someone may be editing); no event of the same name in the target
library; no FCP dialog already open; the copy does not exist yet.

## Behaviour verified live on Final Cut Pro 12.3

Observed with a scratch library while building the importer (not inferred):

| Situation | What FCP did |
|---|---|
| `<import-options>` library location names a library that is open (not the first one) | Imported exactly there; projects visible in the listing within ~10 s. |
| Library location does not exist | FCP **silently created a new library** at that path and imported into it — hence the "must be open" check (`--create-library` allows it on purpose). |
| The XML also contains `<library location="…">` pointing elsewhere | **`import-options` wins**; the other library was untouched. |
| A 30-frame transition whose clips have only 6 frames of handle | Imported **without any warning**, shortened to 6 frames — the maximum `fcpkit-handles` had predicted. Warning dialogs cannot catch this; check handles before importing. |
| AppleScript `persistent ID` of libraries/events/projects | Cannot be coerced to text (error −1700). fcpkit reads every field in its own `try` and identifies objects by `id` (a UUID). |
| Two FCP copies installed (12.3 and 12.4) | Same bundle id; `tell application "Final Cut Pro"` is ambiguous. Addressing the running copy by its full `.app` path reached the right one (checked by the responding version). |

Known from earlier use or other sources, and guarded against, but not re-tested here:
without `<import-options>` a flat `.fcpxml` imports nothing silently and a
`.fcpxmld` opens a blocking "which library?" picker; importing an event whose
name/uid already exists in the library fails with "Invalid argument"
(fcpkit refuses before sending anything).

Not yet verified: stock FCP 12.4 import behaviour, recording of real warning
dialogs (missing media), XML files over 1 MB.

## Configuration

Defaults suit a stock install; override with environment variables (most also have a CLI flag).

| Variable | Default | Used by |
|---|---|---|
| `FCPKIT_FCP_APP` | `/Applications/Final Cut Pro.app` | the app `fcpkit-import` expects to be running (`--app`) |
| `FCPKIT_FCP_APPS` | — (`:`-separated) | extra FCP copies to recognise; a SpliceKit copy in `~/Applications/SpliceKit/` is detected automatically |
| `FCPKIT_LOCK` | `~/Library/Application Support/fcpkit/FCP.lock` | cooperative lock while a tool drives FCP (`--lock`) |
| `FCPKIT_SPLICEKIT_PORT` | `9876` | optional SpliceKit bridge, read only, for dialog recording |
| `FCPKIT_FFMPEG` | `ffmpeg` on `PATH` | filmstrips, loudness |
| `FCPKIT_MOTION_TEMPLATES` | `~/Movies/Motion Templates.localized` | `fcpkit-effects` (`--templates-dir`) |
| `FCPKIT_CAPTION_PATTERN` | `(?i)caption\|subtitle\|자막` | which title templates `fcpkit-diff` counts as captions (`--caption-pattern`); "자막" is Korean for caption |

## Safety model

1. **Sources are never overwritten.** Writes go through `fcpxml_io`: the bytes outside the `<fcpxml>` root (declaration, DOCTYPE) are kept byte-for-byte, the re-serialised tree is re-parsed and compared, the result is validated with `xmllint` against the DTD inside your installed Final Cut Pro, the output must not exist (exclusive create), and the source is re-hashed just before publishing.
2. **Unknown is REVIEW, not PASS.** Unsupported retimes (holds, reverse, eased multi-point ramps), unresolved references, missing registries — all surface as REVIEW with the reason.
3. **Live steps are opt-in and bounded.** Nothing launches FCP implicitly. An import needs an approved plan id, takes a lock, refuses on any failed check, and leaves a receipt. Lock files are renamed, not deleted, when released.
4. **Undo moves, never deletes**, and only files whose hash still matches.
5. **Hostile files are refused, not expanded.** FCPXML never uses entities, so any `<!ENTITY>` or DOCTYPE internal subset is rejected before parsing (old expat builds, such as the one in macOS's `/usr/bin/python3` 3.9, have no entity-expansion limits). External entities are never fetched; `xmllint` runs with `--nonet`. No tool runs a shell: every subprocess gets an argument list, and the only value put into AppleScript (the FCP app path) is escaped.

## MCP server (read only, except filmstrip pictures)

`fcpkit-mcp` speaks MCP over stdio (stdlib only) and exposes `fcpkit_brief`, `fcpkit_handles`,
`fcpkit_effects_check`, `fcpkit_timeline_diff`, `fcpkit_relink_preview`,
`fcpkit_filmstrip` (returns the PNGs as images) and `fcpkit_libraries`. It has
no tool that writes XML or imports into FCP.

```bash
claude mcp add fcpkit -- fcpkit-mcp
```

## How it compares with fcp-mcp-server

[DareDev256/fcp-mcp-server](https://github.com/DareDev256/fcp-mcp-server) is a broad MCP server for driving FCP (editing, assembly, transcription, automatic import). Several fcpkit tools started from ideas found while reading it (v0.27.2): `<import-options>` targeting, AppleScript library listing, handle checks, filmstrip + waveform, effect registry lookup, the write journal. fcpkit is narrower and differs in emphasis:

| | fcpkit | fcp-mcp-server (v0.27.2, code read, not run) |
|---|---|---|
| Scope | checks, a few guarded edits, guarded import | wide editing and automation surface |
| Multicam / sync clips | resolved to the active angle, separately for video and audio | `mc-clip` / `sync-clip` not in its clip walk, so not handle-checked |
| Time | exact `Fraction`s from each sequence's own `frameDuration`; round half away from zero | frame rate from the first `<format>` (default 24); `int(seconds × fps)` truncation |
| Unknown cases | REVIEW with a reason | unresolvable clips are skipped |
| FCP behaviour evidence | observed live on FCP 12.3 (above) | tests simulate `osascript` |

Pick fcp-mcp-server when you want an agent to edit broadly; pick fcpkit when you want to know, exactly, whether a timeline will survive the trip into FCP.

## Tests

```bash
python3 -m unittest discover -s tests
```

All tests use synthetic XML and ffmpeg-generated media; none sends an Apple
event or opens FCP (the live import flow runs against fakes). `FCPKIT_TEST_TMP`
sets where temporary files go. One test compiles the listing AppleScript with
`osacompile` and is skipped when no Final Cut Pro is installed.

## License

MIT — see [LICENSE](LICENSE).
