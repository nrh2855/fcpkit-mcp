"""snap_to_cuts buckets + lossless DTD-valid write; timeline_diff matching."""
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fcpkit import snap_to_cuts  # noqa: E402
from fcpkit import timeline_diff  # noqa: E402
from fixtures import asset, fcpxml, write  # noqa: E402
from fcpkit.timeline import Timeline  # noqa: E402


def cap(offset, duration, text, lane=1, start='0s'):
    return (f'<title ref="rt" lane="{lane}" offset="{offset}" start="{start}" duration="{duration}" name="{text}">'
            f'<text><text-style ref="ts{abs(hash(text)) % 10000}">{text}</text-style></text>'
            f'<text-style-def id="ts{abs(hash(text)) % 10000}"><text-style font="Helvetica" fontSize="60"/></text-style-def>'
            '</title>')


class SnapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        src = self.dir / 'a.mp4'
        src.write_bytes(b'')  # resolution only; no media read
        self.res = asset('r2', src, 100)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, a_children, b_children=''):
        # cut at 5 s; clip A shows source 10-15, B shows 40-45
        spine = (f'<asset-clip ref="r2" offset="0s" start="10s" duration="5s" name="A">{a_children}</asset-clip>'
                 f'<asset-clip ref="r2" offset="5s" start="40s" duration="5s" name="B">{b_children}</asset-clip>')
        return write(self.dir, 'x.fcpxml', fcpxml(self.res, spine, '10s'))

    def plan(self, path, **kw):
        tl = Timeline(path)
        return snap_to_cuts.Snapper(tl, 'Basic Title', None, None, Fraction(1, 2), Fraction(0), None, **kw).plan()

    def test_start_after_cut_moves_back(self):
        rows = self.plan(self.build('', cap('40.2s', '1s', '안녕')))
        self.assertEqual(rows[0]['bucket'], 'moved')
        self.assertEqual(rows[0]['new_start'], 5.0)

    def test_on_cut_and_too_far(self):
        rows = self.plan(self.build('', cap('40s', '1s', 'on') + cap('41.5s', '1s', 'far')))
        self.assertEqual([r['bucket'] for r in rows], ['on_cut', 'no_cut'])

    def test_caption_spanning_the_cut_blocks(self):
        rows = self.plan(self.build(cap('14s', '1.6s', '앞'), cap('40.4s', '1s', '뒤', lane=2)))
        back = [r for r in rows if r['text'] == '뒤'][0]
        self.assertEqual(back['bucket'], 'skipped')
        self.assertIn('speech continues', back['reason'])

    def test_adjacent_boundary_within_shared_max_moves_both(self):
        # previous caption ends where this one starts, 2 frames after the cut
        prev_end = Fraction(5) + Fraction(2, 30)
        rows = self.plan(self.build(cap('13s', f'{(prev_end - 3).numerator}/{(prev_end - 3).denominator}s', '앞'),
                                    cap(f'{(40 * 30 + 2)}/30s', '1s', '뒤')))
        back = [r for r in rows if r['text'] == '뒤'][0]
        self.assertEqual(back['bucket'], 'moved')
        self.assertEqual(back['also_trimmed'][0]['new_end'], 5.0)
        rows = self.plan(self.build(cap('13s', f'{(prev_end - 3).numerator}/{(prev_end - 3).denominator}s', '앞'),
                                    cap(f'{(40 * 30 + 2)}/30s', '1s', '뒤')), shared_max=1)
        self.assertEqual([r for r in rows if r['text'] == '뒤'][0]['bucket'], 'skipped')

    def test_write_is_dtd_valid_and_idempotent(self):
        path = self.build('', cap('40.2s', '1s', '안녕'))
        out = self.dir / 'y.fcpxml'
        snap_to_cuts.main([str(path), '--select', 'Basic Title', '--out', str(out)])
        self.assertTrue(out.exists())
        self.assertNotEqual(path.read_bytes(), out.read_bytes())
        rows = self.plan(out)
        self.assertEqual(rows[0]['bucket'], 'on_cut')
        with self.assertRaises(FileExistsError):
            snap_to_cuts.main([str(path), '--select', 'Basic Title', '--out', str(out)])


class DiffTest(unittest.TestCase):
    def test_moved_trimmed_added(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / 'a.mp4'
            src.write_bytes(b'')
            res = asset('r2', src, 100)
            a = write(d, 'a.fcpxml', fcpxml(res, '<asset-clip ref="r2" offset="0s" start="10s" duration="5s" name="A">'
                                                + cap('11s', '1s', '하나') + cap('12s', '1s', '둘') + '</asset-clip>', '5s'))
            b = write(d, 'b.fcpxml', fcpxml(res, '<asset-clip ref="r2" offset="0s" start="10s" duration="4s" name="A">'
                                                + cap('11.5s', '1s', '하나') + cap('13s', '0.5s', '셋') + '</asset-clip>', '4s'))
            rep = timeline_diff.diff(a, b)
            ch = {(c['stage'], c.get('text') if isinstance(c.get('text'), str) else c['name']): c for c in rep['changes']}
            self.assertEqual(ch[('text', '하나')]['d_start_f'], 15)
            self.assertIn(('removed', '둘'), ch)
            self.assertIn(('added', '셋'), ch)
            self.assertEqual(ch[('exact', 'A')] if ('exact', 'A') in ch else
                             [c for c in rep['changes'] if c['name'] == 'A'][0]['d_end_f'], -30)


if __name__ == '__main__':
    unittest.main()
