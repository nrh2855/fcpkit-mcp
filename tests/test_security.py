"""Hostile-input guards: entity declarations are refused before parsing."""
import tempfile
import unittest
from pathlib import Path

from fcpkit import effects_check, fcpxml_io, relink_preview
from fcpkit.mcp_server import tool_list
from fcpkit.timeline import Timeline

BOMB = b'''<?xml version="1.0"?>
<!DOCTYPE fcpxml [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>
<fcpxml version="1.11"><resources/><library><event name="&b;"/></library></fcpxml>'''
XXE = b'''<?xml version="1.0"?>
<!DOCTYPE fcpxml [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<fcpxml version="1.11"><resources/><library><event name="&x;"/></library></fcpxml>'''


class EntityGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_reader_refuses_entities(self):
        for i, data in enumerate((BOMB, XXE)):
            p = self.dir / f'h{i}.fcpxml'
            p.write_bytes(data)
            for read in (lambda: Timeline(p), lambda: effects_check.check(p), lambda: relink_preview.preview(p),
                         lambda: fcpxml_io.parse_fcpxml(p)):
                with self.assertRaises(fcpxml_io.FCPXMLEnvelopeError):
                    read()

    def test_plain_doctype_still_parses(self):
        fcpxml_io.reject_entities(b'<?xml version="1.0"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.11"/>')


class McpAnnotationTest(unittest.TestCase):
    def test_only_filmstrip_is_not_read_only(self):
        hints = {t['name']: t['annotations']['readOnlyHint'] for t in tool_list()}
        self.assertFalse(hints.pop('fcpkit_filmstrip'))
        self.assertTrue(all(hints.values()))


if __name__ == '__main__':
    unittest.main()
