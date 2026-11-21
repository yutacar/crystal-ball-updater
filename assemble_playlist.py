#!/usr/bin/env python3
"""Join prepared clips in random order into one movie, without re-encoding.

Use as the updater's producer_command for the shuffle mode: it finishes in seconds,
so the display is refreshed quickly. Clips must share codec settings, e.g. the ones
written by generate_fractal_video.py --all.
"""
import argparse
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile


def playlist(clips, count, rng):
    """`count` clips in shuffled rounds; the same clip never plays twice in a row."""
    order = []
    while len(order) < count:
        round_ = list(clips)
        rng.shuffle(round_)
        if order and len(round_) > 1 and round_[0] == order[-1]:
            round_[0], round_[-1] = round_[-1], round_[0]
        order += round_
    return order[:count]


def concat_list(paths):
    # ffconcat quoting: wrap in single quotes, write ' as '\''.
    lines = ['ffconcat version 1.0']
    lines += ["file '" + str(path.resolve()).replace("'", "'\\''") + "'" for path in paths]
    return '\n'.join(lines) + '\n'


def assemble(clips_dir, minutes, clip_minutes, output, seed=None):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('ffmpeg is required on PATH')
    clips = sorted(p for p in clips_dir.glob('*.mp4') if p.is_file())
    if not clips:
        raise ValueError(f'no .mp4 clips in {clips_dir}')
    order = playlist(clips, math.ceil(minutes / clip_minutes - 1e-9), random.Random(seed))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.playlist-', dir=output.parent) as directory:
        listing = Path(directory) / 'list.ffconcat'
        listing.write_text(concat_list(order))
        movie = Path(directory) / 'movie.mp4'
        result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                                 '-f', 'concat', '-safe', '0', '-i', str(listing),
                                 '-c', 'copy', '-movflags', '+faststart', str(movie)],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or 'ffmpeg concat failed')
        os.replace(movie, output)
    print('Playlist: ' + ', '.join(p.stem for p in order), flush=True)
    return order


def parse_args(argv=None):
    from generate_clock_video import positive_float
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clips-dir', type=Path, required=True)
    parser.add_argument('--duration-minutes', type=positive_float, required=True)
    parser.add_argument('--clip-minutes', type=positive_float, default=5,
                        help='length of every clip (default: 5)')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seed', type=int, help='fixed order for testing (default: random)')
    parser.add_argument('--start-time', help='accepted for producer compatibility; unused')
    args = parser.parse_args(argv)
    if args.output.suffix.lower() != '.mp4':
        parser.error('--output must end in .mp4')
    return args


def main():
    args = parse_args()
    try:
        assemble(args.clips_dir, args.duration_minutes, args.clip_minutes, args.output, args.seed)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
