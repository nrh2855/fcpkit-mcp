# Changelog

## 0.1.0 — unreleased

First standalone release, extracted from a private editing toolkit.

- `fcpkit-brief` / MCP `fcpkit_brief`: the timeline as a compact readable brief (symbol table,
  one line per cut with everything starting under it, coverage n/n), English or Korean labels;
  main-storyline compound clips are opened in place, overlay compounds report how much they hide.

- `fcpkit-brief-apply` / MCP `fcpkit_brief_apply`: edit the brief, apply the changed lines back to a new,
  verified FCPXML. Brief v2: keyframe values, overlay compound contents, range-scoped stats, stable ids.

Security review before release:
- Every XML read refuses entity declarations and DOCTYPE internal subsets
  (entity-expansion bombs on old expat; XXE was already off by default).
- `fcpkit_filmstrip` is annotated `readOnlyHint: false` (it writes PNGs into a new folder).
- ffmpeg output paths are passed absolute so a file name can never be read as an option.

- Timeline resolver with exact `Fraction` time: asset clips, clips, sync clips,
  multicam angles (video and audio separately), compound clips, linear retimes;
  unsupported retimes are reported, never assumed.
- `fcpkit-handles`: transition handle check at the transition's real position,
  jump-cut-dissolve detection, optional removed-audio loudness and handle filmstrips.
- `fcpkit-filmstrip`: program-window and source-range frame strips with waveform and cut ticks.
- `fcpkit-effects`: effect/template resolution against FCP's own FxPlug list,
  built-in Motion templates, user and library templates.
- `fcpkit-relink-preview`, `fcpkit-diff`, `fcpkit-snap`, `fcpkit-timeline`.
- `fcpkit-libraries` (AppleScript listing by app path) and `fcpkit-import`
  (`<import-options>` targeting with pre-flight checks, lock, plan id, receipt).
- `fcpkit-plan`, `fcpkit-ledger`, `fcpkit-watch-export`.
- `fcpkit-mcp`: read-only MCP server (stdio, stdlib only).
- Configuration by environment variables (`FCPKIT_*`) instead of fixed paths;
  the external-volume check now covers every `/Volumes/<name>` that holds media.
- Removed from the private version: a hook into an external incremental
  reviewer in `watch-export` (use `--against` for a diff instead).
