import unittest
from datetime import datetime
from contextlib import redirect_stderr
from io import StringIO

import generate_clock_video as clock


class ClockTests(unittest.TestCase):
    def test_minute_boundary_and_year_rollover(self):
        start = datetime(2026, 12, 31, 23, 59)
        self.assertEqual(clock.frame_time(start, 899, 15), start)
        self.assertEqual(clock.frame_time(start, 900, 15), datetime(2027, 1, 1))
        self.assertIn('FRI', clock.label(datetime(2027, 1, 1)))

    def test_render_black_corners_and_continuous_cycle(self):
        renderer = clock.Renderer('#00FFE0', clock.find_font(None), 12, 15)
        start = datetime(2026, 11, 15, 18, 20)
        first = renderer.frame(start, 0)
        self.assertEqual(first.size, (480, 480))
        self.assertEqual(first.getpixel((0, 0)), (0, 0, 0))
        self.assertEqual(first.getpixel((240, 10)), (0, 0, 0))
        self.assertIsNotNone(first.getbbox())
        self.assertEqual(first.tobytes(), renderer.frame(start, 180).tobytes())
        self.assertNotEqual(first.tobytes(), renderer.frame(start, 1).tobytes())

    def test_bad_cli_inputs(self):
        for args in (['--fps', '0'], ['--speed', 'nan'], ['--speed', '-1'],
                     ['--duration-minutes', '0'], ['--color', 'bad'],
                     ['--start-time', 'not-a-date'], ['--output', 'bad.avi']):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                clock.parse_args(args)

    def test_minute_changes_both_clock_and_marquee(self):
        renderer = clock.Renderer('#00FFE0', clock.find_font(None), 12, 15)
        start = datetime(2026, 11, 15, 23, 59)
        old = renderer.frame(start, 0)
        new = renderer.frame(clock.frame_time(start, 900, 15), 900)
        self.assertNotEqual(old.crop((0, 150, 480, 270)).tobytes(),
                            new.crop((0, 150, 480, 270)).tobytes())
        self.assertNotEqual(old.crop((0, 292, 480, 338)).tobytes(),
                            new.crop((0, 292, 480, 338)).tobytes())

    def test_seconds_are_truncated_and_offset_preserved(self):
        args = clock.parse_args(['--start-time', '2026-11-15T18:20:59+09:00'])
        self.assertEqual(args.start.isoformat(), '2026-11-15T18:20:00+09:00')


if __name__ == '__main__':
    unittest.main()
