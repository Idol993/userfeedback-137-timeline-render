"""Generate synthetic test media locally.

All assets are produced with ffmpeg lavfi sources only; nothing is
downloaded. Every source gets two recognition aids for integration tests:

* a full-frame colour matrix tint (distinct dominant colour), and
* a solid 80 px marker band across the top whose pure colour uniquely
  identifies the source even after scaling/padding.

Each audible source also gets a distinct sine frequency.
"""

import argparse
import subprocess
from pathlib import Path

# name -> (size, fps, sample_rate (0 = silent), tone Hz, marker RGB,
#          colour-matrix tint, PTS offset, extra video filter)
SPECS = [
    ('landscape24.mp4', '640x360', 24, 44100, 440, (255, 0, 0),
     'rr=1:gg=0.12:bb=0.12', 0.0, None),
    ('portrait25.mp4', '360x640', 25, 48000, 880, (0, 0, 255),
     'rr=0.12:gg=0.15:bb=1', 0.0, None),
    ('silent30.mp4', '800x450', 30, 0, 0, (0, 255, 0),
     'rr=0.1:gg=1:bb=0.1', 0.0, None),
    ('offset30.mp4', '640x360', 30, 48000, 660, (255, 255, 0),
     'rr=1:gg=1:bb=0.08', 2.0, None),
    ('vfr.mp4', '640x360', 30, 48000, 1320, (255, 0, 255),
     'rr=1:gg=0.1:bb=1', 0.0,
     "setpts=PTS*(1+0.35*sin(N/2.0))"),
    ('mono30.mp4', '640x360', 30, 48000, 1760, (0, 255, 255),
     'rr=0.1:gg=1:bb=1', 0.0, None),
]

# Marker colour -> single-letter label used by the integration tests.
MARKER_LABELS = {
    (255, 0, 0): 'r', (0, 0, 255): 'b', (0, 255, 0): 'g',
    (255, 255, 0): 'y', (255, 0, 255): 'm', (0, 255, 255): 'c',
}


def _marker_filter(rgb):
    r, g, b = rgb
    return (f'drawbox=x=0:y=0:w=iw:h=80:color=0x{r:02x}{g:02x}{b:02x}:t=fill')


def make(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for name, size, fps, rate, hz, marker, tint, offset, extra_vf in SPECS:
        target = root / name
        vfilters = []
        if extra_vf:
            vfilters.append(extra_vf)
        vfilters += [f'colorchannelmixer={tint}', _marker_filter(marker)]
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i',
            f'testsrc2=size={size}:rate={fps}:duration=3',
        ]
        if rate:
            cmd += ['-f', 'lavfi', '-i',
                    f'sine=frequency={hz}:sample_rate={rate}:duration=3']
        cmd += ['-vf', ','.join(vfilters),
                '-c:v', 'libx264', '-preset', 'ultrafast',
                '-pix_fmt', 'yuv420p', '-threads', '2']
        if rate:
            cmd += ['-c:a', 'aac', '-ar', str(rate), '-ac', '1']
        if 'vfr' in name:
            cmd += ['-fps_mode', 'vfr']
        if offset:
            cmd += ['-output_ts_offset', str(offset)]
        cmd += [str(target)]
        subprocess.run(cmd, check=True)
    # Deliberately undecodable input.
    (root / 'broken.mp4').write_bytes(b'not an mp4, just garbage bytes')
    return root


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='fixtures/generated')
    print(make(parser.parse_args().output))
