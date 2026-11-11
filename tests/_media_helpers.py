"""Helpers for integration tests: real probing / decoding via ffmpeg.

No third-party media libraries are used; frames are decoded to raw RGB and
audio to PCM WAV so tests inspect actual rendered pixels and samples.
"""

import json
import math
import struct
import subprocess
import wave
from pathlib import Path

WIDTH = 1280
HEIGHT = 720
FPS = 30
SAMPLE_RATE = 48000


def probe(path):
    proc = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_format', '-show_streams',
         '-of', 'json', str(path)],
        capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def streams(path):
    data = probe(path)
    video = next(s for s in data['streams'] if s['codec_type'] == 'video')
    audio = next(s for s in data['streams'] if s['codec_type'] == 'audio')
    return video, audio, float(data['format']['duration'])


def decode_frames(path, times):
    """Decode the frame nearest to each requested time as RGB byte strings."""
    frames = []
    for t in times:
        proc = subprocess.run(
            ['ffmpeg', '-v', 'error', '-ss', f'{t:.6f}', '-i', str(path),
             '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
            capture_output=True, check=True)
        expected = WIDTH * HEIGHT * 3
        assert len(proc.stdout) >= expected, 'decoded frame has wrong size'
        frames.append(proc.stdout[:expected])
    return frames


def _sampled_pixels(frame):
    # Pixel-aligned stride: every 3 bytes form one RGB triple.
    step = 3
    start = HEIGHT // 8 * WIDTH * 3
    end = 7 * HEIGHT // 8 * WIDTH * 3
    region = frame[start:end: step]
    return region[0::3], region[1::3], region[2::3]


def frame_rgb(frame):
    """Average R/G/B over the solid source marker band (top 80 px).

    make_fixtures.py paints a unique pure-colour band at the top of every
    source; scaling preserves it and pillarbox/letterbox padding never
    overlaps it, so the band identifies which source a frame came from.
    """
    band = frame[: 80 * WIDTH * 3]
    n = len(band) // 3
    return (sum(band[0::3]) / n, sum(band[1::3]) / n,
            sum(band[2::3]) / n)


# Marker signatures painted by tools/make_fixtures.py.
_TINTS = {
    'r': {'r'}, 'b': {'b'}, 'g': {'g'},
    'y': {'r', 'g'}, 'm': {'r', 'b'}, 'c': {'g', 'b'},
}


def classify_tint(frame):
    """Return one of r/g/b/y/m/c from the marker band colour."""
    r, g, b = frame_rgb(frame)
    peak = max(r, g, b)
    active = {name for name, value in (('r', r), ('g', g), ('b', b))
              if value >= peak * 0.6}
    for label, signature in _TINTS.items():
        if signature == active:
            return label, (r, g, b)
    return max((('r', r), ('g', g), ('b', b)), key=lambda item: item[1])[0], (r, g, b)


def dominant_channel(frame):
    return classify_tint(frame)


def frame_brightness(frame):
    r, g, b = frame_rgb(frame)
    return (r + g + b) / 3


def mean_rgb(frame):
    """Plain mean over the sampled picture region (black frame detection)."""
    reds, greens, blues = _sampled_pixels(frame)
    n = len(reds)
    return (sum(reds) / n, sum(greens) / n, sum(blues) / n)


def decode_audio(path, start=0.0, duration=None):
    """Decode audio to mono float samples at 48 kHz."""
    out = Path(path).with_suffix('.check.wav')
    cmd = ['ffmpeg', '-v', 'error', '-y', '-ss', f'{start:.6f}']
    if duration is not None:
        cmd += ['-t', f'{duration:.6f}']
    cmd += ['-i', str(path), '-vn', '-ac', '1', '-ar', str(SAMPLE_RATE),
            '-f', 'wav', str(out)]
    subprocess.run(cmd, check=True)
    with wave.open(str(out), 'rb') as wav:
        assert wav.getframerate() == SAMPLE_RATE
        raw = wav.readframes(wav.getnframes())
        width = wav.getsampwidth()
    out.unlink(missing_ok=True)
    if width == 2:
        samples = struct.unpack('<%dh' % (len(raw) // 2), raw)
        return [s / 32768.0 for s in samples]
    raise AssertionError(f'unexpected sample width {width}')


def rms(samples):
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def dominant_frequency(samples, rate=SAMPLE_RATE):
    """Goertzel power at each candidate tone; return the strongest (Hz).

    Returns 0.0 for silence. The normalised coefficient is evaluated at the
    exact candidate frequency rather than a quantised DFT bin.
    """
    candidates = [440, 660, 880, 1320, 1760]
    n = min(len(samples), rate)  # 1 s window
    window = samples[:n]
    if rms(window) < 0.01:
        return 0.0
    best_hz, best_power = 0.0, 0.0
    for hz in candidates:
        omega = 2 * math.pi * hz / rate
        coeff = 2 * math.cos(omega)
        s0 = s1 = s2 = 0.0
        for x in window:
            s0 = x + coeff * s1 - s2
            s2, s1 = s1, s0
        power = s1 * s1 + s2 * s2 - coeff * s1 * s2
        if power > best_power:
            best_power, best_hz = power, hz
    return float(best_hz)


def wait_state(client, job_id, expected, timeout=60.0):
    import time
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f'/exports/{job_id}').json()
        if last['state'] == expected:
            return last
        if expected not in ('queued', 'running') and last['state'] in (
                'succeeded', 'failed', 'cancelled'):
            return last
        time.sleep(0.1)
    raise AssertionError(f'job {job_id} never reached {expected}, last={last}')
