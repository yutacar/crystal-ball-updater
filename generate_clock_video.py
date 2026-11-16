#!/usr/bin/env python3
"""Generate an offline 480x480 clock movie. Requires Pillow and FFmpeg."""
import argparse
from datetime import datetime, timedelta
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageDraw, ImageFont

WEEKDAYS = ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')


def frame_time(start, index, fps):
    """Each displayed minute occupies exactly 60 seconds, independent of speed."""
    return start + timedelta(minutes=index // (60 * fps))


def label(value):
    return f'{value:%Y.%m.%d} {WEEKDAYS[value.weekday()]} {value:%H:%M}'


def find_font(requested):
    if requested:
        ImageFont.truetype(str(requested), 24)  # fail early
        return str(requested)
    candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
        '/System/Library/Fonts/Menlo.ttc',
        'C:/Windows/Fonts/consola.ttf',
        'DejaVuSansMono.ttf',
    ]
    for candidate in candidates:
        try:
            ImageFont.truetype(candidate, 24)
            return candidate
        except OSError:
            pass
    raise ValueError('No TrueType font found; supply --font /path/to/font.ttf')


class Renderer:
    """Cache only the current minute; no full-video frame buffer."""
    def __init__(self, color, font, speed, fps):
        self.color = color
        self.big = ImageFont.truetype(font, 86)
        self.small = ImageFont.truetype(font, 28)
        self.speed, self.fps = speed, fps
        self.minute = None

    def prepare(self, minute):
        self.minute = minute
        self.base = Image.new('RGB', (480, 480), 'black')
        draw = ImageDraw.Draw(self.base)
        draw.text((240, 215), minute.strftime('%H:%M'), font=self.big,
                  fill=self.color, anchor='mm', stroke_width=0)
        text = label(minute)
        bounds = self.small.getbbox(text)
        self.period = max(370, math.ceil(self.small.getlength(text)) + 80)
        self.strip = Image.new('RGB', (self.period * 2, 46), 'black')
        strip_draw = ImageDraw.Draw(self.strip)
        for x in (0, self.period):
            strip_draw.text((x, 6 - bounds[1]), text, font=self.small, fill=self.color)

    def frame(self, minute, index):
        if minute != self.minute:
            self.prepare(minute)
        # Use global frame index: crossing a minute never restarts the movement.
        phase = (index / self.fps / self.speed) % 1.0
        offset = int(phase * self.period)
        frame = self.base.copy()
        frame.paste(self.strip.crop((offset, 0, offset + 370, 46)), (55, 292))
        return frame


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return result


def positive_float(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('must be finite and greater than zero')
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start-time', help='ISO local datetime, e.g. 2026-11-15T18:20')
    parser.add_argument('--duration-minutes', type=positive_int, default=5)
    parser.add_argument('--fps', type=positive_int, default=15)
    parser.add_argument('--output', type=Path, default=Path('clock_5m.mp4'))
    parser.add_argument('--speed', type=positive_float, default=12,
                        help='seconds per complete marquee repetition (default: 12)')
    parser.add_argument('--color', default='#00FFE0', help='RGB hex, e.g. #00FFE0')
    parser.add_argument('--font', type=Path, help='TrueType/OpenType font file')
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    if args.fps > 60:
        parser.error('--fps must be <= 60')
    if not re.fullmatch(r'#[0-9A-Fa-f]{6}', args.color):
        parser.error('--color must be #RRGGBB')
    if args.output.suffix.lower() != '.mp4':
        parser.error('--output must end in .mp4')
    try:
        args.start = datetime.fromisoformat(args.start_time) if args.start_time else datetime.now().astimezone()
        args.start = args.start.replace(second=0, microsecond=0)
        # Reject date arithmetic overflow before opening an output file.
        args.start + timedelta(minutes=args.duration_minutes)
    except (ValueError, OverflowError) as exc:
        parser.error(f'invalid start/duration: {exc}')
    return args


def generate(args):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('ffmpeg is required on PATH (with libx264 support)')
    font = find_font(args.font)
    if args.output.exists() and not args.overwrite:
        raise ValueError(f'Output exists: {args.output}; use --overwrite')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    renderer = Renderer(args.color, font, args.speed, args.fps)
    frames = args.duration_minutes * 60 * args.fps
    started = time.monotonic()
    print(f'Start label: {args.start.isoformat()} (seconds truncated). '
          f'{frames} frames; {args.duration_minutes} min.', flush=True)
    # Same filesystem for atomic rename. Failed encodes never replace an existing movie.
    with tempfile.TemporaryDirectory(prefix='.clock-', dir=args.output.parent) as directory:
        movie = Path(directory) / 'clock.mp4'
        command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                   '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', '480x480',
                   '-framerate', str(args.fps), '-i', 'pipe:0', '-an',
                   '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
                   '-profile:v', 'baseline', '-level:v', '3.1', '-pix_fmt', 'yuv420p',
                   '-maxrate', '2000k', '-bufsize', '4000k',
                   '-g', str(args.fps * 2), '-movflags', '+faststart', str(movie)]
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=errors)
            try:
                for index in range(frames):
                    process.stdin.write(renderer.frame(frame_time(args.start, index, args.fps), index).tobytes())
                    if index and index % (args.fps * 60) == 0:
                        print(f'Rendered {index // (args.fps * 60)}/{args.duration_minutes} min', flush=True)
                process.stdin.close()
                code = process.wait()
            except BaseException as exc:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
                process.terminate()
                process.wait()
                errors.seek(0)
                if isinstance(exc, BrokenPipeError):
                    raise RuntimeError(errors.read().decode(errors='replace')) from exc
                raise
            if code:
                errors.seek(0)
                raise RuntimeError(errors.read().decode(errors='replace'))
        if args.overwrite:
            movie.replace(args.output)
        else:
            # Exclusive creation also protects against another writer during encoding.
            os.link(movie, args.output)
    elapsed = time.monotonic() - started
    size = args.output.stat().st_size
    print(f'Created {args.output}: {size:,} bytes in {elapsed:.2f}s '
          f'({frames / elapsed:.1f} frames/s).')
    print('Start playback at the specified minute. Looping/restarting rewinds the date/time.')


def main():
    args = parse_args()
    try:
        generate(args)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Cancelled; incomplete movie removed.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
