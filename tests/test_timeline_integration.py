"""End-to-end timeline export tests.

These decode the actual rendered MP4 (pixels via rawvideo, audio via WAV)
and verify timeline math, source identity at cuts/transitions, silence
padding and the fixed output profile.
"""

from pathlib import Path

from tests._media_helpers import (
    FPS, SAMPLE_RATE, decode_audio, decode_frames, dominant_channel,
    dominant_frequency, frame_rgb, mean_rgb, rms, streams, wait_state,
)
from tests.conftest import submit


def clip(fixtures, name, start, end):
    return {'path': str(Path(fixtures) / name), 'in': start, 'out': end}


def _download(client, job_id):
    response = client.get(f'/exports/{job_id}/download')
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'video/mp4'
    return response.content


def test_mixed_timeline_hard_cuts(client, fixtures, media_root, tmp_path):
    # 24fps/44.1kHz landscape, portrait 25fps, silent 30fps, offset PTS, VFR.
    clips = [
        clip(fixtures, 'landscape24.mp4', 0.2, 2.0),   # 1.8s red, 440 Hz
        clip(fixtures, 'portrait25.mp4', 0.0, 1.5),    # 1.5s blue, 880 Hz
        clip(fixtures, 'silent30.mp4', 0.5, 2.5),      # 2.0s green, silent
        clip(fixtures, 'offset30.mp4', 0.3, 1.8),      # 1.5s yellow, 660 Hz
        clip(fixtures, 'vfr.mp4', 0.1, 2.0),           # 1.9s magenta, 1320 Hz
    ]
    job_id = submit(client, clips, transitions=[0, 0, 0, 0])
    job = wait_state(client, job_id, 'succeeded')

    expected = 1.8 + 1.5 + 2.0 + 1.5 + 1.9
    out = tmp_path / f'{job_id}.mp4'
    out.write_bytes(_download(client, job_id))
    video, audio, duration = streams(out)

    # Fixed output profile.
    assert video['codec_name'] == 'h264'
    assert (video['width'], video['height']) == (1280, 720)
    assert video['pix_fmt'] == 'yuv420p'
    assert video['r_frame_rate'] == '30/1'
    assert audio['codec_name'] == 'aac'
    assert int(audio['sample_rate']) == SAMPLE_RATE
    assert int(audio['channels']) == 2

    # Duration within one output frame; audio and video ends within 50 ms.
    assert abs(duration - expected) <= 1 / FPS + 1e-3
    audio_duration = float(audio.get('duration', duration))
    assert abs(audio_duration - float(video.get('duration', duration))) <= 0.05

    # Boundaries on the hard-cut timeline.
    cuts = [0.0, 1.8, 3.3, 5.3, 6.8]
    frame_times = [
        cuts[0] + 0.1,                          # red
        cuts[1] - 0.15, cuts[1] + 0.15,        # red then blue at a hard cut
        cuts[2] + 0.2,                           # green (silent source)
        cuts[3] + 0.2,                           # yellow (offset PTS source)
        cuts[4] + 0.2,                           # magenta (VFR source)
    ]
    frames = decode_frames(out, frame_times)
    expectations = ['r', 'r', 'b', 'g', 'y', 'm']
    for frame, expected_tint, t in zip(frames, expectations, frame_times):
        tint, rgb = dominant_channel(frame)
        assert tint == expected_tint, (t, tint, rgb)

    # Letterboxing: portrait source gets black side bars, not stretched.
    portrait_frame = decode_frames(out, [cuts[1] + 0.5])[0]
    bar = portrait_frame[: 200 * 1280 * 3]
    bar_mean = (sum(bar[0::3]) + sum(bar[1::3]) + sum(bar[2::3])) / len(bar)
    center, _ = dominant_channel(portrait_frame)
    assert bar_mean < 25, bar_mean
    assert center == 'b'

    # First frame really belongs to the first source (red, not black).
    first = decode_frames(out, [0.0])[0]
    assert dominant_channel(first)[0] == 'r'
    assert max(mean_rgb(first)) > 40

    # Audio: audible sources keep their tone; the silent clip is padded with
    # real silence and does not truncate the timeline.
    red_audio = decode_audio(out, start=0.4, duration=0.4)
    assert dominant_frequency(red_audio) == 440
    blue_audio = decode_audio(out, start=cuts[1] + 0.4, duration=0.4)
    assert dominant_frequency(blue_audio) == 880
    silent_audio = decode_audio(out, start=cuts[2] + 0.3, duration=0.8)
    assert rms(silent_audio) < 0.01
    yellow_audio = decode_audio(out, start=cuts[3] + 0.4, duration=0.4)
    assert dominant_frequency(yellow_audio) == 660
    magenta_audio = decode_audio(out, start=cuts[4] + 0.4, duration=0.4)
    assert dominant_frequency(magenta_audio) == 1320


def test_crossfade_timeline(client, fixtures, tmp_path):
    clips = [
        clip(fixtures, 'landscape24.mp4', 0.0, 2.0),   # red, 2.0s
        clip(fixtures, 'silent30.mp4', 0.0, 2.0),      # green, 2.0s, silent
        clip(fixtures, 'mono30.mp4', 0.0, 2.0),        # cyan, 2.0s, mono
    ]
    transitions = [0.5, 0.5]
    job_id = submit(client, clips, transitions)
    wait_state(client, job_id, 'succeeded')
    expected = 2.0 + 2.0 + 2.0 - 1.0

    out = tmp_path / f'{job_id}.mp4'
    out.write_bytes(_download(client, job_id))
    _, audio, duration = streams(out)
    assert abs(duration - expected) <= 1 / FPS + 1e-3

    # First boundary: red->green xfade occupies [1.5, 2.0].
    before, middle, after = decode_frames(out, [1.35, 1.75, 2.2])
    assert dominant_channel(before)[0] == 'r'
    assert dominant_channel(after)[0] == 'g'
    # Mid-transition marker band is a real blend of both markers (both
    # contributing channels well above zero): no black flash, no freeze.
    r, g, b = frame_rgb(middle)
    assert r > 60 and g > 60 and b < 60, (r, g, b)

    # Second boundary: green->cyan xfade occupies [3.0, 3.5].
    before2, middle2, after2 = decode_frames(out, [2.85, 3.25, 3.7])
    assert dominant_channel(before2)[0] == 'g'
    assert dominant_channel(after2)[0] == 'c'
    r2, g2, b2 = frame_rgb(middle2)
    assert g2 > 60 and b2 > 60 and r2 < 60, (r2, g2, b2)

    # No black frame anywhere around the transitions.
    for probe_frame in decode_frames(out, [1.5, 1.6, 1.7, 1.9, 3.0, 3.2, 3.4]):
        assert max(mean_rgb(probe_frame)) > 10

    # Consecutive mid-transition frames must differ (no frozen frame).
    seq = decode_frames(out, [1.6, 1.64, 1.68])
    assert seq[0] != seq[1] and seq[1] != seq[2]

    # Audio: 440 Hz before first fade; silence through the silent clip;
    # the mono 1760 Hz source returns afterwards as stereo.
    assert dominant_frequency(decode_audio(out, 0.5, 0.3)) == 440
    assert rms(decode_audio(out, 2.3, 0.5)) < 0.01
    assert dominant_frequency(decode_audio(out, 4.2, 0.3)) == 1760
    assert int(audio['channels']) == 2


def test_mono_source_is_stereo_and_duration_exact(client, fixtures, tmp_path):
    clips = [clip(fixtures, 'mono30.mp4', 0.25, 2.75)]
    job_id = submit(client, clips, [])
    wait_state(client, job_id, 'succeeded')
    out = tmp_path / f'{job_id}.mp4'
    out.write_bytes(_download(client, job_id))
    video, audio, duration = streams(out)
    assert int(audio['channels']) == 2
    assert int(audio['sample_rate']) == SAMPLE_RATE
    assert abs(duration - 2.5) <= 1 / FPS + 1e-3
    assert abs(float(audio['duration']) - float(video['duration'])) <= 0.05
