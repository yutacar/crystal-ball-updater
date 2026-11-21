#!/usr/bin/env python3
"""Schedule and verify video uploads to a USB video display on Linux.

Generate while playback continues, identify the USB filesystem by UUID AND port,
copy with read-back verification, unmount, then disconnect at a future minute.
"""
import argparse
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

LOG = logging.getLogger('crystal-updater')

# USB vendor/product ID of the supported display (Actions Semiconductor mass storage).
# Only consulted by the opt-in Raspberry Pi 5 shared-power guard.
DISPLAY_USB_ID = ('10d6', '1101')

# Seconds before the playback start that must remain when the old movie is about to be
# replaced, leaving room for the directory fsync and unmount.
TRANSFER_MARGIN_SECONDS = 15

# util-linux umount exit status for "umount failure", returned e.g. when the filesystem
# was already unmounted by someone else.
UMOUNT_FAILURE = 32


def now():
    return datetime.now().astimezone()


def seconds_until(moment):
    return (moment - now()).total_seconds()


def sleep_until(moment):
    while (remaining := seconds_until(moment)) > 0:
        time.sleep(min(remaining, 0.2))


def command(args, privileged=False, timeout=60):
    if privileged and os.geteuid() != 0:
        args = ['sudo', '-n', *args]
    LOG.info('command: %s', ' '.join(map(str, args)))
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout).stdout


def producer_command(template, start, duration, output):
    values = {'{start}': start.isoformat(), '{duration}': str(duration), '{output}': str(output)}
    result = []
    for item in template:
        for key, value in values.items():
            item = item.replace(key, value)
        result.append(item)
    return result


def validate_video(movie, minutes):
    if not movie.is_file() or movie.stat().st_size == 0:
        raise RuntimeError('producer did not create a nonempty video')
    meta = json.loads(command(['ffprobe','-v','error','-show_entries',
        'format=duration:stream=codec_type','-of','json',str(movie)]))
    duration = float(meta.get('format',{}).get('duration',0))
    if not any(row.get('codec_type')=='video' for row in meta.get('streams',[])):
        raise RuntimeError('producer output has no video stream')
    if not duration >= minutes*60 - 0.1:
        raise RuntimeError('producer video is too short')


def next_start(now, lead_minutes):
    return now.replace(second=0, microsecond=0) + timedelta(minutes=lead_minutes + 1)


def device_path(uuid, hub, port):
    candidate = Path('/dev/disk/by-uuid') / uuid
    if not candidate.exists():
        return None
    device = candidate.resolve()
    topology = (Path('/sys/class/block') / device.name).resolve().parts
    usb_port = f'{hub}-{port}' if '-' not in hub else f'{hub}.{port}'
    if usb_port not in topology:
        raise RuntimeError(f'UUID {uuid} is not on expected USB port {usb_port}')
    return device


def wait_device(uuid, hub, port, timeout):
    deadline = time.monotonic() + timeout
    while True:
        found = device_path(uuid, hub, port)
        if found:
            return found
        if time.monotonic() >= deadline:
            raise TimeoutError(f'USB device UUID={uuid} not detected on hub={hub} port={port}')
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def power(args, action):
    command(['uhubctl', '-e', '-l', args.hub, '-p', args.port, '-a', action], privileged=True)


def parse_power_state(output):
    states = []
    hub = None
    for line in output.splitlines():
        match = re.match(r'Current status for hub (\S+)', line)
        if match:
            hub = match.group(1)
        match = re.match(r'\s+Port (\d+): [0-9a-fA-F]{4} (.*)', line)
        if match:
            states.append((hub, match.group(1), 'power' in match.group(2).split()))
    return states


def check_pi5_peripherals(args, root=Path('/sys/bus/usb/devices')):
    expected = f'{args.hub}-{args.port}'
    for node in root.iterdir():
        if not re.fullmatch(r'\d+-\d+(?:\.\d+)*', node.name):
            continue
        vendor = (node/'idVendor').read_text().strip()
        product = (node/'idProduct').read_text().strip()
        if node.name != expected or (vendor, product) != DISPLAY_USB_ID:
            raise RuntimeError(f'other USB peripheral {node.name}; shared power cycle refused')
    device = device_path(args.uuid, args.hub, args.port)
    if device and mounts(device):
        raise RuntimeError('mounted USB filesystem; shared power cycle refused')


def pi5_shared_reconnect(args):
    model = Path('/proc/device-tree/model').read_text().rstrip('\x00')
    if 'Raspberry Pi 5' not in model:
        raise RuntimeError('shared power mode is restricted to Raspberry Pi 5')
    check_pi5_peripherals(args)
    states = parse_power_state(command(['uhubctl'], privileged=True))
    expected = {('1','1'), ('1','2'), ('2','1'), ('3','1'), ('3','2'), ('4','1')}
    if len(states) != 6 or {(h,p) for h,p,_ in states} != expected:
        raise RuntimeError('unexpected hub topology; shared power cycle refused')
    LOG.info('pi5_shared_power_cycle: all onboard USB ports, 10 seconds')
    try:
        for hub in ('1','2','3','4'):
            command(['uhubctl','-e','-l',hub,'-a','off'], privileged=True)
        off = parse_power_state(command(['uhubctl'], privileged=True))
        if len(off) != 6 or any(enabled for _,_,enabled in off):
            raise RuntimeError('not all USB ports are OFF')
        time.sleep(10)
    finally:
        # USB2/USB3 power bits differ; use the textual power state from the snapshot.
        # Attempt every restoration even when one port command fails.
        errors = []
        for hub, port, enabled in states:
            try:
                command(['uhubctl','-e','-l',hub,'-p',port,'-a',
                         'on' if enabled else 'off'], privileged=True)
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError('USB power restoration failed: ' + '; '.join(errors))
    return wait_device(args.uuid, args.hub, args.port, args.device_timeout)


def connect_device(args):
    power(args, 'on')
    try:
        return wait_device(args.uuid, args.hub, args.port,
                           min(3, args.device_timeout) if args.pi5_shared_power else args.device_timeout)
    except TimeoutError:
        if not args.pi5_shared_power:
            raise
        return pi5_shared_reconnect(args)


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stage_movie(source, destination):
    """Copy next to `destination` and verify by read-back; the old movie is untouched."""
    temporary = destination.parent / '.clock-upload.tmp'
    if temporary.exists() or destination.is_symlink():
        raise RuntimeError('unsafe destination or stale upload; inspect manually')
    try:
        with open(source, 'rb') as src, open(temporary, 'xb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if digest(source) != digest(temporary):
            raise RuntimeError('USB copy checksum mismatch')
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def commit_movie(temporary, destination):
    """Replace the old movie with a staged one. From here on the old movie is gone."""
    try:
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def install_movie(source, destination):
    """On-device staging and read-back before replacing this tool's one filename."""
    commit_movie(stage_movie(source, destination), destination)


def wait_for_start(start):
    if seconds_until(start) < -0.5:
        raise RuntimeError('playback deadline missed; refusing incorrect clock start')
    sleep_until(start)


def write_status(path, data):
    staging = path.with_suffix('.tmp')
    staging.write_text(json.dumps(data, indent=2) + '\n')
    staging.replace(path)


def mounts(device):
    # findmnt returns 1 when unmounted; lsblk returns an empty mountpoint list.
    info = json.loads(command(['lsblk', '--json', '--output', 'MOUNTPOINTS', str(device)]))
    return [p for row in info['blockdevices'] for p in (row.get('mountpoints') or []) if p]


def unmount(mountpoint, device):
    try:
        command(['umount', str(mountpoint)], privileged=True)
    except subprocess.CalledProcessError as exc:
        # Accept "umount failure" only when the filesystem is verifiably gone.
        if exc.returncode != UMOUNT_FAILURE or os.path.ismount(mountpoint) or mounts(device):
            raise
        LOG.warning('umount exited %d but %s is not mounted; continuing', exc.returncode, mountpoint)


def resume_old_movie(args, device):
    """After a failure before the old movie was replaced, cut USB power so the display
    plays the old movie again. Only when the filesystem is verifiably unmounted."""
    try:
        device = device or device_path(args.uuid, args.hub, args.port)
        if os.path.ismount(args.state_dir/'mount') or (device and mounts(device)):
            LOG.warning('USB filesystem still mounted; leaving USB connected')
            return False
        power(args, 'off')
    except (OSError, RuntimeError, ValueError, LookupError, subprocess.SubprocessError) as exc:
        LOG.warning('could not return to the old movie: %s', exc)
        return False
    return True


def cycle(args, start=None):
    """One refresh. args.usb_state tells the caller where USB was left if this raises."""
    start = start or next_start(now(), args.lead_minutes)
    connect_at = start - timedelta(seconds=args.connect_before_seconds)
    connect_deadline = start - timedelta(seconds=args.connect_deadline_seconds)
    args.usb_state = 'not_connected'
    with tempfile.TemporaryDirectory(prefix='crystal-clock-', dir=args.state_dir) as work:
        movie = Path(work) / 'clock.mp4'
        budget = seconds_until(connect_deadline)
        if budget <= 0:
            raise RuntimeError('no time left to generate before the connect deadline')
        command(producer_command(args.producer_command, start, args.duration_minutes, movie),
                timeout=budget)
        validate_video(movie, args.duration_minutes)
        # Connecting stops playback, so keep the old movie on screen as long as possible.
        sleep_until(connect_at)
        if seconds_until(connect_deadline) <= 0:
            raise RuntimeError('connect deadline passed; USB not connected, old movie keeps playing')
        args.usb_state = 'connected'
        device = None
        try:
            device = connect_device(args)
            transfer(args, device, movie, start)
        except Exception as exc:
            replaced = getattr(exc, 'replaced', False)
            if not replaced and args.resume_old_on_abort and resume_old_movie(args, device):
                args.usb_state = 'disconnected_old_movie'
            raise
        wait_for_start(start)
        power(args, 'off')
        args.usb_state = 'disconnected'
        status = {'status': 'transferred_and_disconnected', 'start': start.isoformat(),
                  'uuid': args.uuid, 'visual_playback_verified': False,
                  'interval_minutes': args.interval_minutes,
                  'next_start': (start+timedelta(minutes=args.interval_minutes)).isoformat()}
        write_status(args.state_dir/'last_success.json', status)
        LOG.info('transferred_and_disconnected: %s (screen confirmation required)', start.isoformat())
        return start


def transfer(args, device, movie, start):
    """Mount, replace the movie, unmount. On failure, exc.replaced tells whether the
    old movie is already gone."""
    replaced = False
    try:
        existing = mounts(device)
        if len(existing) > 1:
            raise RuntimeError('multiple mounts; refusing to disconnect')
        # Never mount inside `work`: its cleanup is a recursive delete, which would
        # erase the display's files if an unmount ever failed.
        mountpoint = Path(existing[0]) if existing else args.state_dir/'mount'
        mounted = bool(existing)
        try:
            if not existing:
                mountpoint.mkdir(exist_ok=True)
                command(['mount', '-o', f'nosuid,nodev,noexec,uid={os.getuid()},gid={os.getgid()}',
                         str(device), str(mountpoint)], privileged=True)
                mounted = True
            destination = mountpoint/args.filename
            temporary = stage_movie(movie, destination)
            try:
                # Check time before changing the old file; keep a margin for unmount.
                if seconds_until(start) < TRANSFER_MARGIN_SECONDS:
                    raise RuntimeError('transfer deadline too close; old movie preserved')
                replaced = True
                commit_movie(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            LOG.info('copy_verified start=%s sha256=%s', start.isoformat(), digest(movie))
        finally:
            if mounted:
                unmount(mountpoint, device)
        if mounts(device):
            raise RuntimeError('device still mounted; refusing USB power off')
    except Exception as exc:
        exc.replaced = replaced
        raise


def parse_args(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['--config']:
        if len(argv) != 2:
            raise SystemExit('usage: --config PATH (without other arguments)')
        try:
            config = json.loads(Path(argv[1]).read_text())
        except (OSError, ValueError) as exc:
            raise SystemExit(f'cannot load config: {exc}') from exc
        allowed = {'uuid','hub','port','filename','interval_minutes','duration_minutes',
                   'lead_minutes','device_timeout','cycles','state_dir','producer_command','pi5_shared_power',
                   'connect_before_seconds','connect_deadline_seconds','resume_old_on_abort'}
        if not isinstance(config,dict) or set(config)-allowed:
            raise SystemExit('config contains unsupported keys')
        argv = []
        for key,value in config.items():
            if key == 'producer_command':
                argv += ['--producer-command-json',json.dumps(value)]
            elif key in ('pi5_shared_power', 'resume_old_on_abort'):
                if not isinstance(value,bool):raise SystemExit(f'{key} must be boolean')
                if value:argv.append(f"--{key.replace('_','-')}")
            else:
                argv.append(f"--{key.replace('_','-')}={value}")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--producer-command-json',
                   help='optional external producer argv; default: bundled clock generator')
    p.add_argument('--uuid', required=True)
    p.add_argument('--pi5-shared-power', action='store_true',
                   help='allow all onboard USB ports to cycle only when no other USB peripherals are present')
    p.add_argument('--filename', default='00_clock_auto.mp4', help='existing selected clock filename to replace')
    p.add_argument('--hub', required=True)
    p.add_argument('--port', required=True)
    p.add_argument('--interval-minutes', type=int, default=30)
    p.add_argument('--duration-minutes', type=int, default=35)
    p.add_argument('--lead-minutes', type=int, default=3)
    p.add_argument('--device-timeout', type=float, default=20)
    p.add_argument('--cycles', type=int, default=1, help='0: keep refreshing; default: one cycle')
    p.add_argument('--connect-before-seconds', type=float, default=60,
                   help='connect USB this many seconds before playback start (default: 60)')
    p.add_argument('--connect-deadline-seconds', type=float, default=45,
                   help='skip connecting when fewer seconds remain (default: 45)')
    p.add_argument('--resume-old-on-abort', action='store_true',
                   help='on failure before replacement, cut USB power to replay the old movie')
    p.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/crystal-ball-updater')
    a = p.parse_args(argv)
    try:
        a.producer_command = (json.loads(a.producer_command_json) if a.producer_command_json else
            [sys.executable, str(Path(__file__).with_name('generate_clock_video.py')),
             '--start-time', '{start}', '--duration-minutes', '{duration}', '--output', '{output}'])
    except ValueError:
        p.error('producer command must be valid JSON')
    if (not isinstance(a.producer_command,list) or not a.producer_command
            or not all(isinstance(item,str) and item for item in a.producer_command)
            or not any('{output}' in item for item in a.producer_command)):
        p.error('producer must be a nonempty argv array containing {output}')
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.mp4', a.filename):
        p.error('filename must be a simple ASCII .mp4 basename')
    if not re.fullmatch(r'[A-Za-z0-9-]+', a.uuid) or not re.fullmatch(r'\d+(?:[.-]\d+)*', a.hub) or not re.fullmatch(r'\d+', a.port):
        p.error('invalid UUID or USB topology')
    if (a.interval_minutes < 1 or a.lead_minutes < 1 or a.duration_minutes < a.interval_minutes + a.lead_minutes
            or a.cycles < 0 or not 0 < a.device_timeout <= 120):
        p.error('positive timings required; duration must cover interval + generation lead')
    if not (TRANSFER_MARGIN_SECONDS < a.connect_deadline_seconds <= a.connect_before_seconds
            <= a.lead_minutes * 60):
        p.error(f'need {TRANSFER_MARGIN_SECONDS} < connect-deadline-seconds'
                ' <= connect-before-seconds <= lead-minutes*60')
    return a


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with open(args.state_dir/'refresh.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Leave the running instance's state files untouched.
            LOG.error('another instance holds %s; exiting', lock.name)
            return 1
        try:
            count = 0
            target = None
            while True:
                # A failed cycle exits nonzero; systemd retries with a bounded delay.
                started = cycle(args, target)
                count += 1
                if args.cycles and count >= args.cycles:
                    return 0
                target = started + timedelta(minutes=args.interval_minutes)
                next_prepare = target - timedelta(minutes=args.lead_minutes)
                sleep_until(next_prepare)
        except (OSError, RuntimeError, ValueError, LookupError, subprocess.SubprocessError) as exc:
            usb = getattr(args, 'usb_state', 'unknown')
            LOG.exception('refresh_failed: %s; usb=%s', exc, usb)
            write_status(args.state_dir/'last_failure.json', {
                'time': now().isoformat(), 'error': str(exc), 'usb': usb})
            return 1


if __name__ == '__main__':
    sys.exit(main())
