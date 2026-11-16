import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch
import importlib.util
import contextlib
import io
import json
from types import SimpleNamespace

MODULE = Path(__file__).with_name('crystal_ball_updater.py')

class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(), 'periodic refresh implementation is missing')
        spec = importlib.util.spec_from_file_location('refresh_clock', MODULE)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_start_is_future_minute(self):
        now = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        self.assertEqual(self.m.next_start(now, 1).isoformat(), '2027-01-01T00:01:00+00:00')

    def test_atomic_copy_and_checksum(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root/'source.mp4'; src.write_bytes(b'new movie')
            dst = root/'clock.mp4'; dst.write_bytes(b'old movie')
            self.m.install_movie(src, dst)
            self.assertEqual(dst.read_bytes(), b'new movie')
            self.assertFalse((root/'.clock-upload.tmp').exists())

    def test_copy_failure_preserves_existing_video(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            dst = root/'clock.mp4'; dst.write_bytes(b'old')
            with self.assertRaises(FileNotFoundError):
                self.m.install_movie(root/'missing.mp4', dst)
            self.assertEqual(dst.read_bytes(), b'old')

    def test_late_transfer_never_starts_wrong_time(self):
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            self.m.wait_for_start(datetime(2000, 1, 1, tzinfo=timezone.utc))

    def test_missing_device_times_out_before_mount_or_copy(self):
        with patch.object(self.m, 'device_path', return_value=None):
            with self.assertRaisesRegex(TimeoutError, 'USB device'):
                self.m.wait_device('ABCD-1234', '1', '2', 0)

    def test_invalid_config_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.m.parse_args(['--uuid','x','--hub','1','--port','2',
                               '--interval-minutes','60','--duration-minutes','5'])


    def test_two_cycles_replace_same_file_and_record_status(self):
        # Hardware boundaries are simulated; copying, checksum and status are real.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            usb = root/'usb'; usb.mkdir()
            state = root/'state'; state.mkdir()
            args = SimpleNamespace(uuid='ABCD-1234', hub='1', port='2',
                device_timeout=1, interval_minutes=15, pi5_shared_power=False, lead_minutes=1, duration_minutes=2, fps=1, state_dir=state, producer_command=['python','demo.py','--output','{output}'], filename='00_clock_auto.mp4')
            events = []
            def fake_command(cmd, **kw):
                if '--output' in cmd:
                    Path(cmd[cmd.index('--output')+1]).write_bytes(
                        b'movie-cycle-' + str(len(events)).encode())
                elif cmd[0] == 'umount':
                    events.append('unmount')
                return ''
            with patch.object(self.m, 'power', side_effect=lambda a, v: events.append(v)), \
                 patch.object(self.m, 'wait_device', return_value=Path('/dev/fake')), \
                 patch.object(self.m, 'mounts', side_effect=[[str(usb)], []]*2), \
                 patch.object(self.m, 'command', side_effect=fake_command), \
                 patch.object(self.m, 'wait_for_start'), patch.object(self.m, 'validate_video'):
                self.m.cycle(args)
                first = (usb/'00_clock_auto.mp4').read_bytes()
                self.m.cycle(args)
            self.assertNotEqual(first, (usb/'00_clock_auto.mp4').read_bytes())
            self.assertEqual(list(usb.iterdir()), [usb/'00_clock_auto.mp4'])
            self.assertEqual(events, ['on','unmount','off']*2)
            status = json.loads((state/'last_success.json').read_text())
            self.assertFalse(status['visual_playback_verified'])

    def test_failed_unmount_prevents_power_cut(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            usb = root/'usb'; usb.mkdir()
            state = root/'state'; state.mkdir()
            args = SimpleNamespace(uuid='x', hub='1', port='2', device_timeout=1,
                lead_minutes=1, duration_minutes=2, fps=1, state_dir=state, producer_command=['python','demo.py','--output','{output}'], pi5_shared_power=False,
                filename='00_clock_auto.mp4')
            events = []
            def fake_command(cmd, **kw):
                if '--output' in cmd:
                    Path(cmd[cmd.index('--output')+1]).write_bytes(b'movie')
                elif cmd[0] == 'umount':
                    raise RuntimeError('unmount failed')
                return ''
            with patch.object(self.m, 'power', side_effect=lambda a, v: events.append(v)), \
                 patch.object(self.m, 'wait_device', return_value=Path('/dev/fake')), \
                 patch.object(self.m, 'mounts', return_value=[str(usb)]), \
                 patch.object(self.m, 'command', side_effect=fake_command), patch.object(self.m, 'validate_video'):
                with self.assertRaisesRegex(RuntimeError, 'unmount failed'):
                    self.m.cycle(args)
            self.assertEqual(events, ['on'])

    def test_failed_unmount_never_deletes_files_on_the_display(self):
        # The work directory is deleted recursively; a still-mounted display must be outside it.
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            args = SimpleNamespace(uuid='x', hub='1', port='2', device_timeout=1,
                lead_minutes=1, duration_minutes=2, state_dir=state, pi5_shared_power=False,
                producer_command=['python','demo.py','--output','{output}'],
                filename='00_clock_auto.mp4')
            def fake_command(cmd, **kw):
                if '--output' in cmd:
                    Path(cmd[cmd.index('--output')+1]).write_bytes(b'movie')
                elif cmd[0] == 'mount':
                    (Path(cmd[-1])/'other_sample.mp4').write_bytes(b'keep me')
                elif cmd[0] == 'umount':
                    raise RuntimeError('unmount failed')
                return ''
            with patch.object(self.m, 'power'), \
                 patch.object(self.m, 'wait_device', return_value=Path('/dev/fake')), \
                 patch.object(self.m, 'mounts', return_value=[]), \
                 patch.object(self.m, 'command', side_effect=fake_command), \
                 patch.object(self.m, 'validate_video'):
                with self.assertRaisesRegex(RuntimeError, 'unmount failed'):
                    self.m.cycle(args)
            survivors = [p.name for p in state.rglob('*.mp4')]
            self.assertEqual(sorted(survivors), ['00_clock_auto.mp4', 'other_sample.mp4'])

    def test_checksum_failure_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root/'source.mp4'; src.write_bytes(b'new')
            dst = root/'clock.mp4'; dst.write_bytes(b'old')
            with patch.object(self.m, 'digest', side_effect=['a','b']):
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    self.m.install_movie(src, dst)
            self.assertEqual(dst.read_bytes(), b'old')
            self.assertFalse((root/'.clock-upload.tmp').exists())


class Pi5PowerTests(unittest.TestCase):
    setUp = RefreshTests.setUp
    def test_shared_power_snapshot_handles_usb3_power_bit(self):
        sample = '''Current status for hub 2 [USB 3.00]
  Port 1: 02a0 power 5gbps Rx.Detect
Current status for hub 1 [USB 2.00]
  Port 2: 0000 off
'''
        self.assertTrue(hasattr(self.m, 'parse_power_state'), 'shared-power implementation missing')
        self.assertEqual(self.m.parse_power_state(sample), [('2','1',True),('1','2',False)])

    def test_shared_power_guard_refuses_other_peripheral(self):
        self.assertTrue(hasattr(self.m, 'check_pi5_peripherals'), 'shared-power guard missing')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            other = root/'3-1'; other.mkdir()
            (other/'idVendor').write_text('1234')
            (other/'idProduct').write_text('5678')
            args = SimpleNamespace(hub='1',port='2',uuid='ABCD-1234')
            with self.assertRaisesRegex(RuntimeError, 'other USB'):
                self.m.check_pi5_peripherals(args, root)

    def test_shared_power_restores_every_port_when_off_fails(self):
        states = '\n'.join(f'Current status for hub {h} [root]\n  Port {p}: 0100 power'
            for h,p in [('1','1'),('1','2'),('2','1'),('3','1'),('3','2'),('4','1')])
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            if cmd == ['uhubctl']:
                return states
            if cmd[-1] == 'off':
                raise self.m.subprocess.SubprocessError('switch failed')
            return ''
        args = SimpleNamespace(uuid='ABCD-1234',hub='1',port='2',device_timeout=1)
        with patch.object(self.m.Path, 'read_text', return_value='Raspberry Pi 5'), \
             patch.object(self.m, 'check_pi5_peripherals'), \
             patch.object(self.m, 'command', side_effect=run):
            with self.assertRaises(self.m.subprocess.SubprocessError):
                self.m.pi5_shared_reconnect(args)
        self.assertEqual(len([cmd for cmd in calls if cmd[-1]=='on']), 6)

    def test_shared_power_requires_explicit_option(self):
        args = self.m.parse_args(['--uuid','ABCD-1234','--hub','1','--port','2', '--producer-command-json','["demo","{output}"]'])
        self.assertFalse(args.pi5_shared_power)

if __name__ == '__main__':
    unittest.main()
