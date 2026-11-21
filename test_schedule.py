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


class Stop(BaseException):
    """Ends the daemon loop in tests."""


class RunTests(unittest.TestCase):
    """Daemon loop: one retry before the movie on screen ends, and schedule restore."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.args = SimpleNamespace(state_dir=self.state, cycles=0, interval_minutes=30,
                                    duration_minutes=35, lead_minutes=3, retry_before_end_minutes=1)
        self.clock = Clock(START - timedelta(minutes=4))   # fresh schedule starts at START
        self.boot = 'boot-a'
        self.starts = []

    def run_loop(self, outcomes):
        outcomes = list(outcomes)

        def fake_cycle(args, start):
            self.starts.append(start)
            if not outcomes:
                raise Stop
            outcome = outcomes.pop(0)
            if outcome == 'ok':
                self.clock.at = start
                args.usb_state = 'disconnected'
                return start
            # Preparation failures happen before the connect deadline; later ones at start.
            self.clock.at = start - timedelta(seconds=50) if outcome == 'not_connected' else start
            args.usb_state = outcome
            raise RuntimeError(f'failed with usb={outcome}')

        with patch.object(m, 'now', self.clock), \
             patch.object(m, 'sleep_until', side_effect=self.clock.sleep_until), \
             patch.object(m, 'boot_id', side_effect=lambda: self.boot), \
             patch.object(m, 'cycle', side_effect=fake_cycle), \
             self.assertLogs('crystal-updater', 'INFO') as self.logs:
            try:
                return m.run(self.args)
            except Stop:
                return 'stopped'

    def failure(self):
        return json.loads((self.state/'last_failure.json').read_text())

    def playback(self):
        return json.loads((self.state/'playback.json').read_text())

    def write_playback(self, start, retry_used=False, boot='boot-a'):
        (self.state/'playback.json').write_text(json.dumps({
            'start': start.isoformat(), 'duration_minutes': 35,
            'next_slot': (start + timedelta(minutes=30)).isoformat(),
            'retry_used': retry_used, 'boot_id': boot}))

    # --- retry within one process -------------------------------------------------

    def test_preparation_failure_retries_one_minute_before_the_movie_ends(self):
        slot1, retry = START + timedelta(minutes=30), START + timedelta(minutes=34)
        self.assertEqual(self.run_loop(['ok', 'not_connected', 'ok']), 'stopped')
        self.assertEqual(self.starts, [START, slot1, retry, START + timedelta(minutes=60)])
        record = self.failure()
        self.assertEqual(record['retry_at'], retry.isoformat())
        self.assertTrue(record['old_movie_uninterrupted'])
        self.assertEqual(self.playback()['start'], retry.isoformat())
        self.assertEqual(self.playback()['next_slot'], (START + timedelta(minutes=60)).isoformat())
        self.assertFalse(self.playback()['retry_used'])

    def test_retry_uses_a_movie_for_the_retry_time(self):
        self.run_loop(['ok', 'not_connected'])
        self.assertEqual(self.starts[-1], START + timedelta(minutes=34))

    def test_retry_happens_only_once(self):
        self.assertEqual(self.run_loop(['ok', 'not_connected', 'not_connected']), 1)
        self.assertEqual(len(self.starts), 3)
        self.assertIsNone(self.failure()['retry_at'])

    def test_failure_after_usb_connected_is_not_retried(self):
        for usb in ('connected', 'disconnected_old_movie'):
            with self.subTest(usb=usb):
                self.setUp()
                self.assertEqual(self.run_loop(['ok', usb]), 1)
                self.assertEqual(len(self.starts), 2)
                self.assertEqual(self.failure()['usb'], usb)
                self.assertFalse(self.failure()['old_movie_uninterrupted'])
                self.assertIsNone(self.failure()['retry_at'])

    def test_no_retry_without_a_known_movie_on_screen(self):
        self.assertEqual(self.run_loop(['not_connected']), 1)
        self.assertEqual(self.starts, [START])
        self.assertTrue(self.failure()['old_movie_uninterrupted'])

    def test_retry_disabled(self):
        self.args.retry_before_end_minutes = 0
        self.assertEqual(self.run_loop(['ok', 'not_connected']), 1)

    def test_one_shot_runs_neither_restore_nor_retry(self):
        self.args.cycles = 1
        self.write_playback(START - timedelta(minutes=10))
        self.assertEqual(self.run_loop(['not_connected']), 1)
        self.assertEqual(self.starts, [START])

    # --- restart ------------------------------------------------------------------

    def test_restart_continues_on_the_next_slot(self):
        last = START - timedelta(minutes=30)
        self.write_playback(last)
        self.clock.at = START - timedelta(minutes=20)
        self.run_loop([])
        self.assertEqual(self.starts, [START])

    def test_restart_after_the_slot_uses_the_retry_once(self):
        last = START - timedelta(minutes=30)
        self.write_playback(last)
        self.clock.at = START + timedelta(minutes=1)        # retry prepares at START+1
        self.run_loop([])
        self.assertEqual(self.starts, [START + timedelta(minutes=4)])
        self.assertTrue(self.playback()['retry_used'])

    def test_restart_does_not_repeat_a_consumed_retry(self):
        self.write_playback(START - timedelta(minutes=30), retry_used=True)
        self.clock.at = START + timedelta(minutes=1)
        self.run_loop([])
        self.assertEqual(self.starts, [START + timedelta(minutes=5)])   # fresh schedule

    def test_restart_after_the_old_movie_is_too_old_starts_fresh(self):
        self.write_playback(START - timedelta(minutes=30))
        self.clock.at = START + timedelta(minutes=2)
        self.run_loop([])
        self.assertEqual(self.starts, [START + timedelta(minutes=6)])

    def test_restart_after_reboot_starts_fresh(self):
        self.write_playback(START - timedelta(minutes=30), boot='boot-b')
        self.clock.at = START - timedelta(minutes=20)
        self.run_loop([])
        self.assertEqual(self.starts, [START - timedelta(minutes=16)])

    def test_restart_ignores_corrupt_state_files(self):
        for name in ('playback.json', 'last_success.json', 'last_failure.json'):
            (self.state/name).write_text('{broken')
        self.run_loop([])
        self.assertEqual(self.starts, [START])
        self.assertTrue(any('ignoring unreadable' in line for line in self.logs.output))

    def test_restart_ignores_timestamps_without_offset(self):
        (self.state/'playback.json').write_text(json.dumps({
            'start': '2026-11-16T12:00:00', 'duration_minutes': 35,
            'next_slot': '2026-11-16T12:30:00', 'retry_used': False, 'boot_id': 'boot-a'}))
        self.run_loop([])
        self.assertEqual(self.starts, [START])


class PlaybackRecordTests(CycleTests):
    """cycle() forgets the movie on screen exactly when it connects USB."""

    def setUp(self):
        super().setUp()
        (self.state/'playback.json').write_text('{}')

    def test_connecting_forgets_the_old_movie(self):
        self.run_cycle()
        self.assertFalse((self.state/'playback.json').exists())

    def test_aborting_before_connecting_keeps_it(self):
        self.producer_seconds = 140
        with self.assertRaises(RuntimeError):
            self.run_cycle()
        self.assertTrue((self.state/'playback.json').exists())


class ConnectSettingTests(unittest.TestCase):
    BASE = ['--uuid', 'ABCD-1234', '--hub', '1', '--port', '2']

    def test_defaults(self):
        a = m.parse_args(self.BASE)
        self.assertEqual((a.connect_before_seconds, a.connect_deadline_seconds), (60, 45))
        self.assertEqual(a.retry_before_end_minutes, 1)
        self.assertFalse(a.resume_old_on_abort)

    def test_invalid_relations_rejected(self):
        for extra in (['--connect-deadline-seconds', '70'],            # after connect window
                      ['--connect-deadline-seconds', '15'],            # no transfer margin
                      ['--connect-before-seconds', '200'],             # beyond lead time
                      ['--retry-before-end-minutes', '-1'],
                      ['--retry-before-end-minutes', '35']):           # whole movie
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
