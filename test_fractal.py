import contextlib
import io
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import unittest

import assemble_playlist as playlist
import generate_fractal_video as fractal


def lit_pixels(image):
    return sum(1 for value in image.convert('L').getdata() if value > 16)


class GeometryTests(unittest.TestCase):
    def test_stage_sizes_follow_each_fractal_rule(self):
        self.assertEqual([len(s) for s in fractal.koch_stages(4)], [3, 12, 48, 192])
        self.assertEqual([len(s) for s in fractal.sierpinski_stages(4)], [3, 3, 9, 27])
        self.assertEqual([len(s) for s in fractal.hilbert_stages(3)], [3, 15, 63])
        self.assertEqual([len(s) for s in fractal.dragon_stages(4)], [1, 2, 4, 8])
        self.assertEqual([len(s) for s in fractal.tree_stages(4)], [1, 2, 4, 8])

    def test_every_default_pattern_stays_on_screen(self):
        for name, stages in (('koch', fractal.koch_stages()),
                             ('sierpinski', fractal.sierpinski_stages()),
                             ('hilbert', fractal.hilbert_stages()),
                             ('dragon', fractal.dragon_stages()),
                             ('tree', fractal.tree_stages())):
            points = [p for stage in stages for segment in stage for p in segment]
            with self.subTest(pattern=name):
                self.assertTrue(all(0 <= x <= fractal.SIZE and 0 <= y <= fractal.SIZE
                                    for x, y in points))

    def test_hilbert_curve_visits_every_cell_once_with_unit_steps(self):
        cells = [fractal.hilbert_point(3, i) for i in range(64)]
        self.assertEqual(len(set(cells)), 64)
        self.assertTrue(all(abs(x1 - x2) + abs(y1 - y2) == 1
                            for (x1, y1), (x2, y2) in zip(cells, cells[1:])))


class RendererTests(unittest.TestCase):
    def test_all_patterns_render_fade_in_and_out(self):
        for name, make in fractal.PATTERNS.items():
            renderer = make(3)
            with self.subTest(pattern=name):
                start, middle = renderer.frame(0), renderer.frame(55)
                self.assertEqual(middle.size, (480, 480))
                self.assertEqual(lit_pixels(start), 0)
                self.assertGreater(lit_pixels(middle), 0)
                self.assertEqual(lit_pixels(renderer.frame(renderer.period)), 0)

    def test_drawing_grows_within_a_stage(self):
        renderer = fractal.PATTERNS['hilbert'](3)
        counts = [lit_pixels(renderer.frame(t)) for t in (56.0, 58.5, 61.0, 64.0)]  # all in the last stage
        self.assertEqual(counts, sorted(counts))
        self.assertLess(counts[0], counts[-1])

    def test_same_seed_same_movie_new_cycle_new_color(self):
        a, b = fractal.PATTERNS['koch'](9), fractal.PATTERNS['koch'](9)
        self.assertEqual(a.frame(40).tobytes(), b.frame(40).tobytes())
        first = a.frame(70).getbbox(), a.hue
        a.frame(75 + 70)
        self.assertNotEqual(first[1], a.hue)

    def test_cli_rejects_bad_targets(self):
        for args in (['--pattern', 'koch'], ['--pattern', 'koch', '--output', 'x.avi'],
                     ['--all'], ['--pattern', 'nope', '--output', 'x.mp4'],
                     ['--all', '--output-dir', 'd', '--fps', '0']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                fractal.parse_args(args)


class PlaylistTests(unittest.TestCase):
    def test_rounds_cover_every_clip_and_never_repeat_back_to_back(self):
        clips = ['a', 'b', 'c', 'd', 'e', 'f']
        for seed in range(200):
            order = playlist.playlist(clips, 30, random.Random(seed))
            with self.subTest(seed=seed):
                self.assertEqual(len(order), 30)
                self.assertTrue(all(x != y for x, y in zip(order, order[1:])))
                for start in range(0, 30, 6):
                    self.assertEqual(sorted(order[start:start + 6]), clips)

    def test_single_clip_repeats(self):
        self.assertEqual(playlist.playlist(['a'], 3, random.Random(0)), ['a', 'a', 'a'])

    def test_concat_list_quotes_paths(self):
        text = playlist.concat_list([Path("/clips/it's.mp4")])
        self.assertIn("file '/clips/it'\\''s.mp4'", text)

    def test_cli_accepts_updater_placeholders(self):
        args = playlist.parse_args(['--clips-dir', 'clips', '--duration-minutes', '35',
                                    '--output', 'out.mp4', '--start-time', '2026-11-21T12:00'])
        self.assertEqual(args.duration_minutes, 35)

    def test_empty_clip_directory_is_an_error(self):
        with tempfile.TemporaryDirectory() as d, patch_which():
            with self.assertRaisesRegex(ValueError, 'no .mp4 clips'):
                playlist.assemble(Path(d), 35, 5, Path(d)/'out.mp4')


@contextlib.contextmanager
def patch_which():
    from unittest.mock import patch
    with patch.object(playlist.shutil, 'which', return_value='/usr/bin/ffmpeg'):
        yield


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
class EncodeAndJoinTests(unittest.TestCase):
    def test_generated_clips_join_without_reencoding(self):
        with tempfile.TemporaryDirectory() as d:
            clips = Path(d)/'clips'
            with contextlib.redirect_stdout(io.StringIO()):
                for name in ('koch', 'lissajous'):
                    fractal.generate(name, 0.05, 15, 1, clips/f'{name}.mp4', False)
                order = playlist.assemble(clips, 0.15, 0.05, Path(d)/'out.mp4', seed=1)
            self.assertEqual(len(order), 3)
            info = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(Path(d)/'out.mp4')],
                                  capture_output=True, text=True).stderr
            self.assertIn('Duration: 00:00:09.00', info)
            self.assertIn('480x480', info)


if __name__ == '__main__':
    unittest.main()
