#!/usr/bin/env python3
"""Generate 480x480 fractal animation clips for the shuffle mode. Requires Pillow and FFmpeg.

Each clip draws one pattern growing stage by stage, holds it, fades out and starts over
in a new color. Clips share the clock generator's encoder settings, so
assemble_playlist.py can join them without re-encoding.
"""
import argparse
import colorsys
import math
from pathlib import Path
import random
import sys
import time

from PIL import Image, ImageDraw

from generate_clock_video import encode_movie, positive_float, positive_int

SIZE = 480
CENTER = SIZE / 2
LINE_WIDTH = 2
FADE_SECONDS = 1.0


# --- geometry: each stage is a list of line segments ((x1, y1), (x2, y2)) ------------

def polyline(points):
    return list(zip(points, points[1:]))


def koch_stages(levels=6, radius=210):
    corners = [(CENTER + radius * math.sin(a), CENTER - radius * math.cos(a))
               for a in (0, 2 * math.pi / 3, 4 * math.pi / 3)]
    points = corners + corners[:1]
    stages = [polyline(points)]
    for _ in range(levels - 1):
        refined = [points[0]]
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            dx, dy = (x2 - x1) / 3, (y2 - y1) / 3
            a, b = (x1 + dx, y1 + dy), (x1 + 2 * dx, y1 + 2 * dy)
            # Peak of the outward bump: rotate (dx, dy) by -60 degrees from a.
            peak = (a[0] + dx * 0.5 + dy * math.sqrt(3) / 2,
                    a[1] - dx * math.sqrt(3) / 2 + dy * 0.5)
            refined += [a, peak, b, (x2, y2)]
        points = refined
        stages.append(polyline(points))
    return stages


def triangle(a, b, c):
    return [(a, b), (b, c), (c, a)]


def sierpinski_stages(levels=7, side=420):
    height = side * math.sqrt(3) / 2
    top = (CENTER, CENTER - height / 2)
    left = (CENTER - side / 2, CENTER + height / 2)
    right = (CENTER + side / 2, CENTER + height / 2)
    stages = [triangle(top, left, right)]
    current = [(top, left, right)]
    for _ in range(levels - 1):
        added, following = [], []
        for a, b, c in current:
            ab = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            bc = ((b[0] + c[0]) / 2, (b[1] + c[1]) / 2)
            ca = ((c[0] + a[0]) / 2, (c[1] + a[1]) / 2)
            added += triangle(ab, bc, ca)
            following += [(a, ab, ca), (ab, b, bc), (ca, bc, c)]
        stages.append(added)
        current = following
    return stages


def hilbert_point(order, index):
    """Map a distance along the Hilbert curve to grid cell (x, y)."""
    x = y = 0
    step = 1
    while step < 2 ** order:
        rx = 1 & (index // 2)
        ry = 1 & (index ^ rx)
        if ry == 0:
            if rx == 1:
                x, y = step - 1 - x, step - 1 - y
            x, y = y, x
        x += step * rx
        y += step * ry
        index //= 4
        step *= 2
    return x, y


def hilbert_stages(levels=6, side=420):
    stages = []
    for order in range(1, levels + 1):
        cells = 2 ** order
        cell = side / cells
        origin = CENTER - side / 2 + cell / 2
        points = [(origin + x * cell, origin + y * cell)
                  for x, y in (hilbert_point(order, i) for i in range(cells * cells))]
        stages.append(polyline(points))
    return stages


def dragon_stages(levels=13, box=400):
    # Fixed endpoints: each level folds every segment, so all levels overlap.
    paths = [[(0.0, 0.0), (1.0, 0.0)]]
    for _ in range(levels - 1):
        previous, path = paths[-1], [paths[-1][0]]
        for turn, ((x1, y1), (x2, y2)) in enumerate(zip(previous, previous[1:])):
            sign = 1 if turn % 2 == 0 else -1
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            path += [(mx - sign * (y2 - y1) / 2, my + sign * (x2 - x1) / 2), (x2, y2)]
        paths.append(path)
    xs = [x for x, _ in paths[-1]]
    ys = [y for _, y in paths[-1]]
    scale = box / max(max(xs) - min(xs), max(ys) - min(ys))
    ox = CENTER - (max(xs) + min(xs)) / 2 * scale
    oy = CENTER - (max(ys) + min(ys)) / 2 * scale
    return [polyline([(ox + x * scale, oy + y * scale) for x, y in path]) for path in paths]


def tree_stages(levels=10, trunk=118, spread=24, shrink=0.72):
    stages = []
    tips = [((CENTER, SIZE - 20), -90.0, trunk)]
    for _ in range(levels):
        segments, following = [], []
        for (x, y), angle, length in tips:
            end = (x + length * math.cos(math.radians(angle)),
                   y + length * math.sin(math.radians(angle)))
            segments.append(((x, y), end))
            following += [(end, angle - spread, length * shrink),
                          (end, angle + spread, length * shrink)]
        stages.append(segments)
        tips = following
    return stages


# --- renderers ---------------------------------------------------------------------

def color(hue, value=1.0):
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.75, value))


def fade(image, factor):
    if factor >= 1:
        return image
    return image.point(lambda v: int(v * factor))


class StagedRenderer:
    """Reveal stages one after another; `additive` keeps earlier stages bright,
    otherwise the previous stage stays as a dim outline under the new one."""

    def __init__(self, stages, additive, seed, period=75.0, hold=10.0):
        self.stages, self.additive = stages, additive
        self.period, self.hold = period, hold
        self.rng = random.Random(seed)
        self.cycle = self.stage = None

    def _draw(self, canvas, segments, fill):
        draw = ImageDraw.Draw(canvas)
        for segment in segments:
            draw.line(segment, fill=fill, width=LINE_WIDTH)

    def frame(self, seconds):
        cycle, offset = divmod(seconds, self.period)
        if cycle != self.cycle:
            self.cycle, self.stage = cycle, None
            self.hue = self.rng.random()
        progress = min(1.0, offset / (self.period - self.hold)) * len(self.stages)
        stage = min(int(progress), len(self.stages) - 1)
        if stage != self.stage:
            self.stage, self.drawn = stage, 0
            self.canvas = Image.new('RGB', (SIZE, SIZE), 'black')
            if self.additive:
                for earlier in self.stages[:stage]:
                    self._draw(self.canvas, earlier, color(self.hue))
            elif stage:
                self._draw(self.canvas, self.stages[stage - 1], color(self.hue, 0.3))
        segments = self.stages[stage]
        count = len(segments) if progress >= len(self.stages) else \
            int((progress - stage) * len(segments))
        self._draw(self.canvas, segments[self.drawn:count], color(self.hue))
        self.drawn = max(self.drawn, count)
        edge = min(offset, self.period - offset) / FADE_SECONDS
        return fade(self.canvas.copy(), edge)


class LissajousRenderer:
    """Phase drifts continuously; the frequency ratio changes every `period` seconds."""
    RATIOS = ((1, 2), (3, 2), (3, 4), (5, 4), (5, 6))

    def __init__(self, seed, period=60.0, radius=200, points=900, drift=20.0):
        self.rng = random.Random(seed)
        self.period, self.radius, self.points, self.drift = period, radius, points, drift
        self.cycle = None

    def frame(self, seconds):
        cycle, offset = divmod(seconds, self.period)
        if cycle != self.cycle:
            self.cycle = cycle
            self.hue = self.rng.random()
            self.ratio = self.rng.choice(self.RATIOS)
        a, b = self.ratio
        phase = 2 * math.pi * seconds / self.drift
        curve = [(CENTER + self.radius * math.sin(a * t + phase),
                  CENTER + self.radius * math.sin(b * t))
                 for t in (2 * math.pi * i / self.points for i in range(self.points + 1))]
        image = Image.new('RGB', (SIZE, SIZE), 'black')
        ImageDraw.Draw(image).line(curve, fill=color(self.hue), width=LINE_WIDTH)
        return fade(image, min(offset, self.period - offset) / FADE_SECONDS)


PATTERNS = {
    'koch': lambda seed: StagedRenderer(koch_stages(), False, seed),
    'sierpinski': lambda seed: StagedRenderer(sierpinski_stages(), True, seed),
    'hilbert': lambda seed: StagedRenderer(hilbert_stages(), False, seed),
    'dragon': lambda seed: StagedRenderer(dragon_stages(), False, seed),
    'tree': lambda seed: StagedRenderer(tree_stages(), True, seed),
    'lissajous': lambda seed: LissajousRenderer(seed),
}


def frames(pattern, minutes, fps, seed):
    renderer = PATTERNS[pattern](seed)
    for index in range(round(minutes * 60 * fps)):
        yield renderer.frame(index / fps)


def generate(pattern, minutes, fps, seed, output, overwrite):
    started = time.monotonic()
    encode_movie(frames(pattern, minutes, fps, seed), fps, output, overwrite)
    print(f'Created {output} ({pattern}, {minutes:g} min) in '
          f'{time.monotonic() - started:.1f}s', flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--pattern', choices=sorted(PATTERNS))
    target.add_argument('--all', action='store_true',
                        help='write every pattern as OUTPUT_DIR/<pattern>.mp4')
    parser.add_argument('--output', type=Path, help='with --pattern: the .mp4 to write')
    parser.add_argument('--output-dir', type=Path, help='with --all: directory for the clips')
    parser.add_argument('--minutes', type=positive_float, default=5,
                        help='clip length (default: 5)')
    parser.add_argument('--fps', type=positive_int, default=15)
    parser.add_argument('--seed', type=int, default=None,
                        help='color/ratio sequence seed (default: random)')
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    if args.fps > 60:
        parser.error('--fps must be <= 60')
    if args.pattern and (not args.output or args.output.suffix.lower() != '.mp4'):
        parser.error('--pattern needs --output ending in .mp4')
    if args.all and not args.output_dir:
        parser.error('--all needs --output-dir')
    if args.seed is None:
        args.seed = random.randrange(2 ** 32)
    return args


def main():
    args = parse_args()
    jobs = ([(name, args.output_dir / f'{name}.mp4') for name in sorted(PATTERNS)]
            if args.all else [(args.pattern, args.output)])
    try:
        for offset, (name, output) in enumerate(jobs):
            generate(name, args.minutes, args.fps, args.seed + offset, output, args.overwrite)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Cancelled; incomplete movie removed.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
