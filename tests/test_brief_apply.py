"""brief_apply: edited brief lines change exactly their elements; illegal edits are refused."""
import re
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from fcpkit import brief_apply
from fcpkit import timeline_brief
from fixtures import asset, fcpxml, write  # noqa: E402
from fcpkit.timeline import Timeline


def cap(offset, duration, text, lane=1):
    return (f'<title ref="rt" lane="{lane}" offset="{offset}" duration="{duration}" name="{text}">'
            f'<text><text-style ref="s{abs(hash(text)) % 999}">{text}</text-style></text>'
            f'<text-style-def id="s{abs(hash(text)) % 999}"><text-style font="Helvetica" fontSize="60"/>'
            '</text-style-def></title>')


SFX = '<asset-clip ref="r3" lane="-1" offset="{o}" start="0s" duration="0.5s" name="pop"><adjust-volume amount="-15dB"/></asset-clip>'


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        a, b = self.dir / 'a.mp4', self.dir / 'pop.wav'
        a.write_bytes(b'')
        b.write_bytes(b'')
        res = asset('r2', a, 100) + asset('r3', b, 2)
        spine = ('<asset-clip ref="r2" offset="0s" start="10s" duration="5s" name="A"><adjust-volume amount="-5dB"/>'
                 + cap('11s', '1s', '하나') + SFX.format(o='12s') + '</asset-clip>'
                 '<asset-clip ref="r2" offset="5s" start="40s" duration="5s" name="B"><adjust-volume amount="-5dB"/>'
                 + cap('41s', '1s', '둘') + '</asset-clip>')
        self.xml = write(self.dir, 'x.fcpxml', fcpxml(res, spine, '10s'))
        text, _ = timeline_brief.render(self.xml, ids=True, lang='ko')
        self.brief = text

    def tearDown(self):
        self.tmp.cleanup()

    def run_edit(self, edit, **kw):
        edited = edit(self.brief)
        p = self.dir / 'e.md'
        p.write_text(edited, encoding='utf-8')
        out = self.dir / f'out{len(list(self.dir.glob("out*")))}.fcpxml'
        return brief_apply.apply(self.xml, p, out, **kw), out

    @staticmethod
    def sub(pattern, repl):
        return lambda s: re.sub(pattern, repl, s, count=1, flags=re.M)

    def test_text_time_length_vol_delete(self):
        def edit(s):
            s = re.sub(r'(\+1 00:01\.00 T\d+ )"하나" 1\.00s', r'\g<1>"하나다" 1.50s', s)
            s = re.sub(r'\+1 00:01\.00 (T\d+ "하나다")', r'+2 00:00.50 \1', s)     # move, longer, new lane
            s = re.sub(r'(-1 00:02\.00 M\d+ .*?)vol -15dB', r'\1vol -9dB', s)
            s = re.sub(r'^.*"둘".*\n', '', s, flags=re.M)                            # delete caption 2
            return s
        rep, out = self.run_edit(edit)
        self.assertEqual(rep['status'], 'WRITTEN', rep)
        tl = Timeline(out)
        titles = {timeline_brief.Brief.text(i.el): i for i in tl.items if i.tag == 'title'}
        self.assertEqual(set(titles), {'하나다'})
        t1 = titles['하나다']
        self.assertEqual((float(t1.abs_start), float(t1.duration), t1.lane), (0.5, 1.5, 2))
        sfx = next(i for i in tl.items if i.lane == -1)
        self.assertEqual(sfx.el.find('adjust-volume').get('amount'), '-9dB')

    def test_move_across_cut_reanchors_in_dtd_order(self):
        rep, out = self.run_edit(lambda s: re.sub(r'\+1 00:01\.00 (T\d+ "하나")', r'+1 00:06.00 \1', s))
        self.assertEqual(rep['status'], 'WRITTEN', rep)
        tl = Timeline(out)
        t1 = next(i for i in tl.items if i.tag == 'title' and timeline_brief.Brief.text(i.el) == '하나')
        self.assertEqual(float(t1.abs_start), 6.0)
        self.assertEqual(t1.parent.name, 'B')
        kids = [k.tag for k in t1.parent.el]
        self.assertLess(kids.index('adjust-volume'), kids.index('title'))

    def test_illegal_edits_refuse_and_write_nothing(self):
        def edit(s):
            s = re.sub(r'^▌00:05\.00 (.*)', r'▌00:04.00 \1', s, flags=re.M)        # main time: ripple
            s = s.replace('"하나"', '"하나" fx T99', 1)                              # body change
            return s + '\n  +3 00:01.00 T1 "새 줄" 1.00s\n'                           # new line
        rep, out = self.run_edit(edit)
        self.assertEqual(rep['status'], 'REFUSED')
        self.assertEqual(len(rep['refused']), 3)
        self.assertFalse(out.exists())

    def test_stale_brief_is_refused(self):
        self.xml.write_text(self.xml.read_text() + '\n', encoding='utf-8')     # XML changed after the brief
        with self.assertRaises(brief_apply.Refused):
            self.run_edit(lambda s: s)

    def test_unchanged_brief_changes_nothing(self):
        rep, out = self.run_edit(lambda s: s)
        self.assertEqual(rep['status'], 'NO-CHANGE')
        self.assertFalse(out.exists())


class SplitRunAndRangeTest(unittest.TestCase):
    def test_text_change_inside_one_styled_run_and_range_brief(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a = d / 'a.mp4'
            a.write_bytes(b'')
            title = ('<title ref="rt" lane="1" offset="11s" duration="1s" name="t">'
                     '<text><text-style ref="k1">소리가 작아지게 된</text-style><text-style ref="k2">다</text-style></text>'
                     '<text-style-def id="k1"><text-style font="Helvetica" fontSize="60" kerning="-6"/></text-style-def>'
                     '<text-style-def id="k2"><text-style font="Helvetica" fontSize="60" kerning="-3"/></text-style-def>'
                     '</title>')
            spine = ('<asset-clip ref="r2" offset="0s" start="10s" duration="5s" name="A">' + title + '</asset-clip>'
                     '<asset-clip ref="r2" offset="5s" start="40s" duration="5s" name="B">'
                     + cap('41s', '1s', '먼 자막') + '</asset-clip>')
            xml = write(d, 'x.fcpxml', fcpxml(asset('r2', a, 100), spine, '10s'))
            text, _ = timeline_brief.render(xml, start=Fraction(0), end=Fraction(3), ids=True, lang='ko')
            self.assertNotIn('먼 자막', text)                       # outside the range
            p = d / 'e.md'
            p.write_text(text.replace('작아지게 된다"', '작아집니다"'), encoding='utf-8')
            rep = brief_apply.apply(xml, p, d / 'o.fcpxml')
            self.assertEqual(rep['status'], 'WRITTEN', rep)
            t = next(i for i in Timeline(d / 'o.fcpxml').items if i.tag == 'title' and i.name == 't')
            self.assertEqual([(ts.get('ref'), ts.text) for ts in t.el.iter('text-style') if ts.get('ref')],
                             [('k1', '소리가 작아집니'), ('k2', '다')])
            self.assertTrue(any(i.name == '먼 자막' for i in Timeline(d / 'o.fcpxml').items))  # untouched


if __name__ == '__main__':
    unittest.main()
