"""Lossless outer-envelope handling for deterministic FCPXML rewrites.

- parse keeps the bytes before and after the <fcpxml> root (declaration,
  DOCTYPE, comments) and records the source file's revision and sha256;
- render re-serialises the edited tree, re-parses it and refuses if the
  structure or envelope changed in a way the edit did not intend;
- publish writes a NEW file only (exclusive create, never the source, never an
  existing output), after validating against the FCP DTD that ships inside the
  installed Final Cut Pro, and after re-checking that the source is unchanged.
"""

from __future__ import annotations

import io
import hashlib
import errno
import os
import re
import tempfile
import sys
import subprocess
import xml.etree.ElementTree as ET
import xml.parsers.expat
from dataclasses import dataclass
from pathlib import Path


class FCPXMLEnvelopeError(ValueError):
    pass


@dataclass(frozen=True)
class FCPXMLEnvelope:
    prefix: bytes
    suffix: bytes


_DTD_SUBSET = re.compile(rb'<!DOCTYPE[^>\[]*\[')


def reject_entities(data: bytes) -> None:
    """Refuse entity declarations and DOCTYPE internal subsets before any parser sees them.

    FCPXML never uses entities (FCP writes a bare `<!DOCTYPE fcpxml>`). Old expat
    builds (e.g. 2.2.x in macOS's /usr/bin/python3 3.9) have no protection against
    entity-expansion bombs, so a hostile file is rejected instead of expanded.
    External entities are never fetched (Python disables them by default)."""
    if b'<!ENTITY' in data or _DTD_SUBSET.search(data):
        raise FCPXMLEnvelopeError('FCPXML must not declare entities or a DOCTYPE internal subset')


def safe_parse(path) -> ET.ElementTree:
    """Read-only parse of an FCPXML file with the entity guard (comments dropped)."""
    data = Path(path).read_bytes()
    reject_entities(data)
    return ET.parse(io.BytesIO(data))


def _split_envelope(data: bytes) -> FCPXMLEnvelope:
    reject_entities(data)
    root_start: int | None = None
    root_end: int | None = None
    depth = 0
    parser = xml.parsers.expat.ParserCreate()

    def start(name: str, _: dict[str, str]) -> None:
        nonlocal root_start, depth
        if depth == 0:
            if name != "fcpxml" or root_start is not None:
                raise FCPXMLEnvelopeError("FCPXML root boundary is ambiguous")
            root_start = parser.CurrentByteIndex
        depth += 1

    def end(name: str) -> None:
        nonlocal root_end, depth
        depth -= 1
        if depth != 0:
            return
        if name != "fcpxml" or root_start is None:
            raise FCPXMLEnvelopeError("FCPXML root boundary is ambiguous")
        boundary = parser.CurrentByteIndex
        # Expat points at </fcpxml> for an explicit root close. A final
        # self-closing child also makes the preceding bytes end in '/>', but
        # must not be mistaken for a self-closing root.
        if not data.startswith(b"</fcpxml", boundary) and data[root_start:boundary].rstrip().endswith(b"/>"):
            root_end = boundary
            return
        if not data.startswith(b"</fcpxml", boundary):
            raise FCPXMLEnvelopeError("FCPXML closing boundary is ambiguous")
        closing = data.find(b">", boundary + len(b"</fcpxml"))
        if closing < 0:
            raise FCPXMLEnvelopeError("FCPXML closing boundary is incomplete")
        root_end = closing + 1

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(data, True)
    except (xml.parsers.expat.ExpatError, FCPXMLEnvelopeError) as exc:
        raise FCPXMLEnvelopeError(str(exc)) from exc
    if root_start is None or root_end is None or root_end <= root_start:
        raise FCPXMLEnvelopeError("FCPXML root boundary is missing")
    declaration = re.search(br"<\?xml\b.*?\?>", data[:root_start], re.DOTALL | re.IGNORECASE)
    if declaration:
        encoding = re.search(
            br"\bencoding\s*=\s*(['\"])(.*?)\1",
            declaration.group(0),
            re.IGNORECASE,
        )
        if encoding and encoding.group(2).lower().replace(b"_", b"-") != b"utf-8":
            raise FCPXMLEnvelopeError("Only UTF-8 FCPXML can be rewritten safely")
    return FCPXMLEnvelope(prefix=data[:root_start], suffix=data[root_end:])


def parse_fcpxml_bytes(data: bytes) -> tuple[ET.ElementTree, FCPXMLEnvelope]:
    envelope = _split_envelope(data)
    parser = ET.XMLParser(
        target=ET.TreeBuilder(insert_comments=True, insert_pis=True),
    )
    tree = ET.parse(io.BytesIO(data), parser=parser)
    return tree, envelope


def parse_fcpxml(path: Path) -> tuple[ET.ElementTree, FCPXMLEnvelope]:
    path = path.expanduser().absolute()
    before = path.stat()
    data = path.read_bytes()
    tree, envelope = parse_fcpxml_bytes(data)
    after = path.stat()
    if _file_revision(before) != _file_revision(after):
        raise FCPXMLEnvelopeError("Source changed during parsing; read the latest XML again")
    tree._fcpkit_source_revision = (path, _file_revision(after), hashlib.sha256(data).hexdigest())
    return tree, envelope


def _file_revision(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _copy_exclusive(temporary: str, output: Path) -> str:
    # exFAT/FSKit rejects both hard links and RENAME_EXCL. O_EXCL still
    # protects existing files. The file is visible while copying; consumers
    # must wait for the function/CLI to complete, not react to its creation.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as destination, open(temporary, "rb") as source:
            import shutil
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        try:
            current = output.lstat()
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                output.unlink()
        except FileNotFoundError:
            pass
        raise
    return "exclusive_copy"


def _publish_exclusive(temporary: str, output: Path) -> str:
    if sys.platform == "darwin":
        # macOS SDK sys/stdio.h: RENAME_EXCL=0x4. Never fall back to replace.
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(os.fsencode(temporary), os.fsencode(output), 0x4) != 0:
            code = ctypes.get_errno()
            if code in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                return _copy_exclusive(temporary, output)
            raise OSError(code, os.strerror(code), str(output))
        return "exclusive_rename"
    else:
        try:
            os.link(temporary, output)
        except OSError as exc:
            if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                return _copy_exclusive(temporary, output)
            raise
        return "exclusive_link"


def validate_fcpxml_dtd(rendered: bytes, *, dtd: Path | None = None) -> dict[str, str]:
    """Validate the actual candidate, offline, against its installed FCP version.

    XML well-formedness does not validate child ordering or resource references.
    Missing tooling/schema is an unavailable check, never a validation pass.
    """
    tree, _ = parse_fcpxml_bytes(rendered)
    version = tree.getroot().get('version', '')
    if not re.fullmatch(r'1\.[0-9]+', version):
        raise FCPXMLEnvelopeError('Unsupported FCPXML schema version: ' + version)
    if dtd is None:
        name = 'FCPXMLv' + version.replace('.', '_') + '.dtd'
        from . import config
        apps = list(config.fcp_apps()) + [Path('/Applications/Final Cut Pro Creator Studio.app')]
        candidates = [app / 'Contents/Frameworks/Interchange.framework/Versions/A/Resources' / name for app in apps]
        dtd = next((p for p in candidates if p.is_file()), None)
    if dtd is None or not dtd.is_file():
        raise FCPXMLEnvelopeError('FCPXML DTD unavailable for version ' + version)
    dtd = dtd.resolve()
    schema = dtd.read_bytes()
    try:
        result = subprocess.run(['/usr/bin/xmllint', '--nonet', '--noout', '--dtdvalid', dtd.as_uri(), '-'],
                                input=rendered, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FCPXMLEnvelopeError('FCPXML DTD validation unavailable: ' + str(exc)) from exc
    if dtd.read_bytes() != schema:
        raise FCPXMLEnvelopeError('FCPXML DTD changed during validation')
    if result.returncode:
        detail = result.stderr.decode(errors='replace')[-4000:]
        raise FCPXMLEnvelopeError('FCPXML DTD validation failed: ' + detail)
    return {'version': version, 'path': str(dtd), 'sha256': hashlib.sha256(schema).hexdigest(), 'status': 'pass'}


def publish_fcpxml(tree: ET.ElementTree, output: Path, rendered: bytes) -> dict[str, object]:
    """Publish a new candidate only if the parsed source is still current.

    This guards a file revision, not a live FCP library. Native project changes
    must invalidate the caller's export before invoking this file boundary.
    Exclusive publication never replaces a competing writer's output. On
    exFAT, copying is not atomically visible: wait for successful completion.
    """
    identity = getattr(tree, "_fcpkit_source_revision", None)
    if identity is None:
        raise FCPXMLEnvelopeError("Source revision is unavailable; refusing to publish")
    source, revision, sha = identity
    output = output.expanduser().absolute()
    if output.resolve() == source.resolve():
        raise FCPXMLEnvelopeError("Source XML must not be overwritten")
    if output.exists():
        raise FileExistsError(str(output))
    validation = validate_fcpxml_dtd(rendered)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=output.parent, prefix="." + output.name + ".")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        before = source.stat()
        current = source.read_bytes()
        after = source.stat()
        if (revision != _file_revision(before) or revision != _file_revision(after)
                or hashlib.sha256(current).hexdigest() != sha):
            raise FCPXMLEnvelopeError("Source changed since parsing; regenerate from the latest XML")
        method = _publish_exclusive(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"source": str(source), "source_sha256": sha,
            "output_sha256": hashlib.sha256(rendered).hexdigest(),
            "method": method,
            "dtd_validation": validation,
            "scope": "Source file checked immediately before exclusive publication; not live library freshness"}


def _structural_signature(element: ET.Element) -> tuple[object, ...]:
    if element.tag is ET.Comment:
        tag: object = "#comment"
    elif element.tag is ET.ProcessingInstruction:
        tag = "#processing-instruction"
    else:
        tag = element.tag
    return (
        tag,
        tuple(sorted(element.attrib.items())),
        element.text,
        element.tail,
        tuple(_structural_signature(child) for child in element),
    )


def render_fcpxml(tree: ET.ElementTree, envelope: FCPXMLEnvelope) -> bytes:
    body = ET.tostring(tree.getroot(), encoding="utf-8", short_empty_elements=True)
    rendered = envelope.prefix + body + envelope.suffix
    parsed, output_envelope = parse_fcpxml_bytes(rendered)
    if (
        output_envelope != envelope
        or _structural_signature(parsed.getroot())
        != _structural_signature(tree.getroot())
    ):
        raise FCPXMLEnvelopeError("FCPXML structure or envelope changed during rewrite")
    return rendered
