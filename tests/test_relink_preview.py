"""relink_preview: synthetic FCPXMLs in a temp dir."""
import contextlib
import io
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fcpkit import relink_preview  # noqa: E402


def url(path: str) -> str:
    return 'file://' + quote(path)


def fcpxml(assets: list[tuple[str, str, bool]]) -> str:
    rows = []
    for i, (name, src, bookmark) in enumerate(assets, start=2):
        bm = '<bookmark>Ym9vaw==</bookmark>' if bookmark else ''
        rows.append(f'<asset id="r{i}" name="{name}" start="0s" duration="5s" hasVideo="1" format="r1">'
                    f'<media-rep kind="original-media" src="{src}">{bm}</media-rep></asset>')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
 <resources>
  <format id="r1" frameDuration="1/30s" width="1920" height="1080"/>
  {''.join(rows)}
 </resources>
 <library><event name="e"><project name="p">
  <sequence format="r1" duration="5s" tcStart="0s"><spine><gap name="Gap" offset="0s" duration="5s"/></spine></sequence>
 </project></event></library>
</fcpxml>'''


class RelinkPreviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, assets):
        p = self.dir / 'a.fcpxml'
        p.write_text(fcpxml(assets), encoding='utf-8')
        return p

    def test_remap_matches_whole_segments_only(self):
        new = self.dir / 'new'
        (new).mkdir()
        (new / 'x.mov').write_text('x')
        p = self.write([('x', url(str(self.dir / 'Media/A/x.mov')), True),
                        ('y', url(str(self.dir / 'Media/AB/y.mov')), False)])
        rep = relink_preview.preview(p, str(self.dir / 'Media/A'), str(new))
        rows = {r['name']: r for r in rep['rows']}
        self.assertTrue(rows['x']['remap_matched'])
        self.assertEqual(rows['x']['target'], str(new / 'x.mov'))
        self.assertTrue(rows['x']['target_exists'])
        self.assertFalse(rows['y']['remap_matched'])
        self.assertEqual(rep['summary']['remap']['matched'], 1)
        self.assertEqual(rep['summary']['with_bookmark'], 1)
        self.assertEqual(rep['summary']['unresolved'], 1)      # y is missing and not remapped
        self.assertEqual(relink_preview.exit_code(rep), 1)

    def test_remap_helper(self):
        self.assertIsNone(relink_preview.remap(Path('/Media/AB/y.mov'), '/Media/A', '/N'))
        self.assertEqual(relink_preview.remap(Path('/Media/A/y.mov'), '/Media/A', '/N/'), Path('/N/y.mov'))
        self.assertEqual(relink_preview.remap(Path('/Media/A'), '/Media/A', '/N'), Path('/N'))

    def test_nfd_url_finds_nfc_file(self):
        name = unicodedata.normalize('NFC', '한글 영상.mov')
        (self.dir / name).write_text('x')
        nfd_src = url(unicodedata.normalize('NFD', str(self.dir / name)))
        self.assertIn('%E1%84', nfd_src)                      # really percent-encoded NFD
        rep = relink_preview.preview(self.write([('k', nfd_src, True)]))
        row = rep['rows'][0]
        self.assertTrue(row['exists'])
        self.assertEqual(row['status'], 'OK')
        self.assertEqual(rep['summary']['unresolved'], 0)
        self.assertEqual(relink_preview.exit_code(rep), 0)

    def test_nfc_prefix_matches_nfd_path(self):
        base = str(self.dir / '한글')
        p = self.write([('k', url(unicodedata.normalize('NFD', base + '/a.mov')), False)])
        rep = relink_preview.preview(p, unicodedata.normalize('NFC', base), str(self.dir))
        self.assertTrue(rep['rows'][0]['remap_matched'])

    def test_missing_grouped_by_root(self):
        rep = relink_preview.preview(self.write([('m', url('/Volumes/NoSuchDrive/a.mov'), True)]))
        self.assertEqual(rep['summary']['by_root'], {'/Volumes/NoSuchDrive': {'assets': 1, 'exists': 0,
                                                                              'missing': 1, 'review': 0}})
        self.assertEqual(rep['rows'][0]['status'], 'MISSING')

    def test_prefix_matching_nothing_is_review(self):
        f = self.dir / 'a.mov'
        f.write_text('x')
        rep = relink_preview.preview(self.write([('a', url(str(f)), True)]), '/Nope', '/Other')
        self.assertEqual(rep['summary']['remap']['matched'], 0)
        self.assertEqual(relink_preview.exit_code(rep), 2)

    def test_never_writes_xml(self):
        p = self.write([('m', url('/Volumes/NoSuchDrive/a.mov'), True)])
        before = p.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            code = relink_preview.main([str(p), '--from', '/Volumes/NoSuchDrive', '--to', str(self.dir),
                                        '--out', str(self.dir / 'r.json')])
        self.assertEqual(code, 1)
        self.assertEqual(p.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
