"""Connect window, abort and recovery paths of one refresh cycle.

Time is simulated with a fake clock; USB power, mount and the producer are faked.
Copying to the "display" directory and checksum verification are real.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import crystal_ball_updater as m

START = datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)

    def sleep_until(self, moment):
        self.at = max(self.at, moment)


class CycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.usb = root/'usb'
        self.usb.mkdir()
        (self.usb/'00_clock_auto.mp4').write_bytes(b'old movie')
        (self.usb/'other_sample.mp4').write_bytes(b'sample')
        self.state = root/'state'
        self.state.mkdir()
        self.args = SimpleNamespace(uuid='ABCD-1234', hub='1', port='2', device_timeout=20,
            interval_minutes=30, duration_minutes=35, lead_minutes=3, pi5_shared_power=False,
            connect_before_seconds=60, connect_deadline_seconds=45, resume_old_on_abort=False,
            state_dir=self.state, filename='00_clock_auto.mp4',
            producer_command=['gen', '--output', '{output}'])
        self.clock = Clock(START - timedelta(minutes=3))
        self.events = []
        self.producer_seconds = 30
        self.umount_error = None
        self.mounted_after_umount = []
        self.automounted = True

    def fake_command(self, cmd, **kw):
        if '--output' in cmd:
            self.events.append(('generate', round(kw['timeout'])))
            self.clock.advance(self.producer_seconds)
            Path(cmd[cmd.index('--output')+1]).write_bytes(b'new movie')
        elif cmd[0] == 'umount':
            self.events.append('umount')
            if self.umount_error:
                raise self.umount_error
        return ''

    def fake_mounts(self, device):
        # The display is already auto-mounted at self.usb until umount runs.
        if not self.automounted:
            return []
        return self.mounted_after_umount if 'umount' in self.events else [str(self.usb)]

    def run_cycle(self, connect=None):
        def power(args, action):
            self.events.append(action)
        connect = connect or (lambda args: (power(args, 'on'), self.clock.advance(5))[0] or Path('/dev/sdz1'))
        with patch.object(m, 'now', self.clock), \
             patch.object(m, 'sleep_until', side_effect=self.clock.sleep_until), \
             patch.object(m, 'command', side_effect=self.fake_command), \
             patch.object(m, 'validate_video'), \
             patch.object(m, 'connect_device', side_effect=connect), \
             patch.object(m, 'device_path', return_value=Path('/dev/sdz1')), \
             patch.object(m, 'mounts', side_effect=self.fake_mounts), \
             patch.object(m, 'power', side_effect=power):
            return m.cycle(self.args, START)

    def movie(self):
        return (self.usb/'00_clock_auto.mp4').read_bytes()

    # --- normal path -----------------------------------------------------------

    def test_connects_only_at_the_connect_window_and_cuts_at_start(self):
        self.assertEqual(self.run_cycle(), START)
        self.assertEqual(self.events, [('generate', 135), 'on', 'umount', 'off'])
        self.assertEqual(self.movie(), b'new movie')
        self.assertEqual(self.args.usb_state, 'disconnected')

    def test_connect_window_is_configurable(self):
        self.args.connect_before_seconds = 120
        seen = []
        def connect(args):
            seen.append(self.clock())
            return Path('/dev/sdz1')
        self.run_cycle(connect)
        self.assertEqual(seen, [START - timedelta(seconds=120)])

    # --- before USB is connected: old movie keeps playing ----------------------

    def test_slow_generation_skips_connecting(self):
        self.producer_seconds = 140   # finishes 40 s before start, past the 45 s deadline
        with self.assertRaisesRegex(RuntimeError, 'connect deadline passed'):
            self.run_cycle()
        self.assertNotIn('on', self.events)
        self.assertEqual(self.args.usb_state, 'not_connected')
        self.assertEqual(self.movie(), b'old movie')

    def test_expired_schedule_never_runs_the_producer(self):
        self.clock.at = START - timedelta(seconds=44)
        with self.assertRaisesRegex(RuntimeError, 'no time left'):
            self.run_cycle()
        self.assertEqual(self.events, [])
        self.assertEqual(self.args.usb_state, 'not_connected')

    def test_producer_timeout_ends_at_the_connect_deadline(self):
        self.clock.at = START - timedelta(seconds=200)
        self.run_cycle()
        self.assertEqual(self.events[0], ('generate', 155))

    # --- after USB is connected, before the old movie is replaced ---------------

    def late_connect(self, args):
        self.events.append('on')
        self.clock.at = START - timedelta(seconds=10)   # device appeared too late
        return Path('/dev/sdz1')

    def test_late_device_leaves_usb_connected_by_default(self):
        with self.assertRaisesRegex(RuntimeError, 'transfer deadline too close'):
            self.run_cycle(self.late_connect)
        self.assertEqual(self.events[1:], ['on', 'umount'])
        self.assertEqual(self.args.usb_state, 'connected')
        self.assertEqual(self.movie(), b'old movie')
        self.assertEqual(sorted(p.name for p in self.usb.iterdir()),
                         ['00_clock_auto.mp4', 'other_sample.mp4'])

    def test_late_device_returns_to_old_movie_when_enabled(self):
        self.args.resume_old_on_abort = True
        with self.assertRaisesRegex(RuntimeError, 'transfer deadline too close'):
            self.run_cycle(self.late_connect)
        self.assertEqual(self.events[1:], ['on', 'umount', 'off'])
        self.assertEqual(self.args.usb_state, 'disconnected_old_movie')
        self.assertEqual(self.movie(), b'old movie')

    def test_device_never_detected_returns_to_old_movie_when_enabled(self):
        self.args.resume_old_on_abort = True
        def missing(args):
            self.events.append('on')
            raise TimeoutError('USB device not detected')
        self.automounted = False
        with self.assertRaises(TimeoutError):
            self.run_cycle(missing)
        self.assertEqual(self.events[1:], ['on', 'off'])
        self.assertEqual(self.args.usb_state, 'disconnected_old_movie')

    def test_checksum_failure_returns_to_old_movie_when_enabled(self):
        self.args.resume_old_on_abort = True
        with patch.object(m, 'digest', side_effect=['a', 'b']):
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                self.run_cycle()
        self.assertEqual(self.events[-1], 'off')
        self.assertEqual(self.movie(), b'old movie')
        self.assertFalse((self.usb/'.clock-upload.tmp').exists())

    def test_still_mounted_never_powers_off_even_when_enabled(self):
        self.args.resume_old_on_abort = True
        self.umount_error = RuntimeError('target is busy')
        self.mounted_after_umount = [str(self.usb)]
        with self.assertRaisesRegex(RuntimeError, 'busy'):
            self.run_cycle(self.late_connect)
        self.assertNotIn('off', self.events)
        self.assertEqual(self.args.usb_state, 'connected')

    # --- after the old movie was replaced ---------------------------------------

    def test_failure_after_replacement_never_returns_to_old_movie(self):
        self.args.resume_old_on_abort = True
        real_commit = m.commit_movie
        def commit_then_fail(temporary, destination):
            real_commit(temporary, destination)
            raise OSError('directory fsync failed')
        with patch.object(m, 'commit_movie', side_effect=commit_then_fail):
            with self.assertRaisesRegex(OSError, 'fsync'):
                self.run_cycle()
        self.assertNotIn('off', self.events)
        self.assertEqual(self.args.usb_state, 'connected')

    def test_missed_start_after_replacement_leaves_usb_connected(self):
        self.args.resume_old_on_abort = True
        real_commit = m.commit_movie
        def slow_commit(temporary, destination):
            real_commit(temporary, destination)
            self.clock.at = START + timedelta(seconds=5)
        with patch.object(m, 'commit_movie', side_effect=slow_commit):
            with self.assertRaisesRegex(RuntimeError, 'playback deadline missed'):
                self.run_cycle()
        self.assertNotIn('off', self.events)
        self.assertEqual(self.movie(), b'new movie')

    # --- umount exit status 32 --------------------------------------------------

    def test_umount_32_accepted_when_verifiably_unmounted(self):
        self.umount_error = subprocess.CalledProcessError(32, ['umount'])
        self.assertEqual(self.run_cycle(), START)
        self.assertEqual(self.events[-1], 'off')

    def test_umount_32_rejected_while_still_mounted(self):
        self.umount_error = subprocess.CalledProcessError(32, ['umount'])
        self.mounted_after_umount = [str(self.usb)]
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cycle()
        self.assertNotIn('off', self.events)

    def test_other_umount_failures_are_not_accepted(self):
        self.umount_error = subprocess.CalledProcessError(1, ['umount'])
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cycle()
        self.assertNotIn('off', self.events)


class FailureRecordTests(unittest.TestCase):
    def test_failure_record_says_where_usb_was_left(self):
        with tempfile.TemporaryDirectory() as d:
            args = SimpleNamespace(state_dir=Path(d), cycles=1, interval_minutes=30, lead_minutes=3)
            def fail(a, start):
                a.usb_state = 'disconnected_old_movie'
                raise RuntimeError('transfer deadline too close')
            with patch.object(m, 'parse_args', return_value=args), \
                 patch.object(m, 'cycle', side_effect=fail), self.assertLogs('crystal-updater'):
                self.assertEqual(m.main(), 1)
            record = json.loads((Path(d)/'last_failure.json').read_text())
            self.assertEqual(record['usb'], 'disconnected_old_movie')

    def test_restart_ignores_corrupt_state_files(self):
        # The schedule is never restored from state files, so corruption cannot block a restart.
        with tempfile.TemporaryDirectory() as d:
            for name in ('last_success.json', 'last_failure.json'):
                (Path(d)/name).write_text('{broken')
            args = SimpleNamespace(state_dir=Path(d), cycles=1, interval_minutes=30, lead_minutes=3)
            with patch.object(m, 'parse_args', return_value=args), \
                 patch.object(m, 'cycle', return_value=START) as cycle:
                self.assertEqual(m.main(), 0)
            self.assertIsNone(cycle.call_args.args[1])


class ConnectSettingTests(unittest.TestCase):
    BASE = ['--uuid', 'ABCD-1234', '--hub', '1', '--port', '2']

    def test_defaults(self):
        a = m.parse_args(self.BASE)
        self.assertEqual((a.connect_before_seconds, a.connect_deadline_seconds), (60, 45))
        self.assertFalse(a.resume_old_on_abort)

    def test_invalid_relations_rejected(self):
        for extra in (['--connect-deadline-seconds', '70'],            # after connect window
                      ['--connect-deadline-seconds', '15'],            # no transfer margin
                      ['--connect-before-seconds', '200']):            # beyond lead time
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                m.parse_args(self.BASE + extra)

    def test_config_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'config.json'
            path.write_text(json.dumps({'uuid': 'ABCD-1234', 'hub': '1', 'port': '2',
                'connect_before_seconds': 90, 'connect_deadline_seconds': 50,
                'resume_old_on_abort': True}))
            a = m.parse_args(['--config', str(path)])
            self.assertEqual((a.connect_before_seconds, a.connect_deadline_seconds), (90, 50))
            self.assertTrue(a.resume_old_on_abort)
            path.write_text(json.dumps({'uuid': 'ABCD-1234', 'hub': '1', 'port': '2',
                                        'resume_old_on_abort': 'yes'}))
            with self.assertRaisesRegex(SystemExit, 'boolean'):
                m.parse_args(['--config', str(path)])


if __name__ == '__main__':
    unittest.main()
