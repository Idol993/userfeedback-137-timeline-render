"""Timeline rendering.

Builds one ffmpeg filter_complex that normalises every source to the fixed
output profile (1280x720, 30 fps, yuv420p, 48 kHz stereo AAC), trims each
clip to its [in, out] presentation window with PTS reset to zero, adds
silence for sources without an audio track, and joins clips with hard cuts
or xfade/acrossfade transitions whose duration is removed from the timeline.
"""

import json
import os
import subprocess
import threading
from pathlib import Path

OUT_WIDTH = 1280
OUT_HEIGHT = 720
OUT_FPS = 30
OUT_SAMPLE_RATE = 48000
# One output frame duration; duration tolerances are measured against it.
FRAME_DURATION = 1.0 / OUT_FPS
# Kept in the job record / error log so a failure stays diagnosable.
LOG_TAIL_LIMIT = 4000


class RenderError(Exception):
    """The request is invalid or ffmpeg could not produce the export."""


class RenderCancelled(Exception):
    """Raised when the job was cancelled while ffmpeg was running."""


def _run_ffprobe(path):
    try:
        proc = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_format', '-show_streams',
                '-of', 'json', str(path),
            ],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        raise RenderError('ffprobe 不可用，无法检查输入素材')
    except subprocess.TimeoutExpired:
        raise RenderError(f'ffprobe 探测超时: {path}')
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1:] or ['无法解码']
        raise RenderError(f'输入不可解码 {path}: {detail[0]}')
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RenderError(f'ffprobe 输出无法解析: {path}')


def _stream_duration(stream, container):
    for holder in (stream, container):
        value = holder.get('duration')
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def probe_sources(clips):
    """Validate every referenced file and return per-clip stream metadata."""
    probed = []
    for index, clip in enumerate(clips):
        path = Path(clip['path'])
        if not path.is_file():
            raise RenderError(f'片段 {index + 1} 的文件不存在: {path}')
        data = _run_ffprobe(path)
        streams = data.get('streams', [])
        video = next((s for s in streams if s.get('codec_type') == 'video'), None)
        if video is None:
            raise RenderError(f'片段 {index + 1} 不含视频流: {path}')
        audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
        width = int(video.get('width') or 0)
        height = int(video.get('height') or 0)
        if width <= 0 or height <= 0:
            raise RenderError(f'片段 {index + 1} 的视频分辨率无效: {path}')
        duration = _stream_duration(video, data.get('format', {}))
        probed.append({
            'path': str(path.resolve()),
            'duration': duration,
            'has_audio': audio is not None,
        })
    return probed


def validate_request(request, probed):
    """Validate trim windows and transitions; return clip and transition lists."""
    clips = request.get('clips')
    if not isinstance(clips, list) or not clips:
        raise RenderError('clips 不能为空')
    transitions = request.get('transition_seconds') or []
    if not isinstance(transitions, list):
        raise RenderError('transition_seconds 必须是数组')
    if len(transitions) > max(0, len(clips) - 1):
        raise RenderError(
            f'transition_seconds 长度 {len(transitions)} 超过相邻片段边界数 '
            f'{max(0, len(clips) - 1)}')

    clip_specs = []
    for index, (clip, meta) in enumerate(zip(clips, probed)):
        try:
            start = float(clip['in'])
            end = float(clip['out'])
        except (KeyError, TypeError, ValueError):
            raise RenderError(f'片段 {index + 1} 的 in/out 必须是数字')
        if not (start >= 0.0) or start != start or start in (float('inf'), float('-inf')):
            raise RenderError(f'片段 {index + 1} 的 in 非法: {start}')
        if end != end or end in (float('inf'), float('-inf')):
            raise RenderError(f'片段 {index + 1} 的 out 非法: {end}')
        if end <= start:
            raise RenderError(
                f'片段 {index + 1} 裁切区间非法: in={start}, out={end}，须满足 out > in')
        if meta['duration'] is not None and end > meta['duration'] + 1e-6:
            raise RenderError(
                f'片段 {index + 1} 裁切越界: out={end:g}s 超过素材时长 '
                f'{meta["duration"]:.3f}s ({meta["path"]})')
        clip_specs.append({'start': start, 'end': end, 'meta': meta})

    transition_specs = []
    for index, value in enumerate(transitions):
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            raise RenderError(f'转场 {index + 1} 的时长必须是数字')
        if seconds < 0.0 or seconds != seconds or seconds == float('inf'):
            raise RenderError(f'转场 {index + 1} 的时长非法: {value}')
        transition_specs.append(seconds)

    timeline = sum(c['end'] - c['start'] for c in clip_specs)
    timeline -= sum(transition_specs)
    if timeline <= 0.0:
        raise RenderError(
            f'转场总时长 {sum(transition_specs):g}s 不短于素材总时长，'
            '无法生成正长度时间线')
    for index, seconds in enumerate(transition_specs):
        left = clip_specs[index]['end'] - clip_specs[index]['start']
        right = clip_specs[index + 1]['end'] - clip_specs[index + 1]['start']
        # xfade/acrossfade need real headroom on both sides; a transition as
        # long as (or within rounding of) a clip would collapse that clip.
        if seconds + 1e-6 >= min(left, right):
            raise RenderError(
                f'转场 {index + 1} 时长 {seconds:g}s 不短于其相邻片段'
                f'（{left:g}s / {right:g}s）')
    return clip_specs, transition_specs, timeline


def _build_filter_graph(clip_specs, transition_specs):
    """Construct the ffmpeg -filter_complex script and its input list."""
    inputs = []
    lines = []
    labels = []
    for index, spec in enumerate(clip_specs):
        start, end = spec['start'], spec['end']
        # probe_sources() resolved this to an absolute path so it stays
        # reachable regardless of the ffmpeg working directory.
        path = spec['meta']['path']
        inputs += ['-i', path]
        if spec['meta']['has_audio']:
            video_chain = (
                f'[{index}:v]settb=AVTB,setpts=PTS-STARTPTS,'
                f'trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,'
                f'fps={OUT_FPS},'
                f'scale={OUT_WIDTH}:{OUT_HEIGHT}:force_original_aspect_ratio=decrease,'
                f'pad={OUT_WIDTH}:{OUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,'
                f'setsar=1,settb=AVTB,tpad=stop=-1:stop_mode=clone,'
                f'trim=duration={end - start:.6f},setpts=PTS-STARTPTS[v{index}]'
            )
            audio_chain = (
                f'[{index}:a]asetpts=PTS-STARTPTS,'
                f'atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,'
                f'aformat=sample_rates={OUT_SAMPLE_RATE}:channel_layouts=stereo,'
                f'apad,atrim=duration={end - start:.6f},asetpts=PTS-STARTPTS[a{index}]'
            )
            lines += [video_chain, audio_chain]
        else:
            # No audio track: synthesise stereo silence covering the whole clip.
            duration = end - start
            video_chain = (
                f'[{index}:v]settb=AVTB,setpts=PTS-STARTPTS,'
                f'trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,'
                f'fps={OUT_FPS},'
                f'scale={OUT_WIDTH}:{OUT_HEIGHT}:force_original_aspect_ratio=decrease,'
                f'pad={OUT_WIDTH}:{OUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,'
                f'setsar=1,settb=AVTB,tpad=stop=-1:stop_mode=clone,'
                f'trim=duration={duration:.6f},setpts=PTS-STARTPTS[v{index}]'
            )
            audio_chain = (
                f'anullsrc=channel_layout=stereo:sample_rate={OUT_SAMPLE_RATE},'
                f'atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a{index}]'
            )
            lines += [video_chain, audio_chain]
        labels.append((f'v{index}', f'a{index}'))

    current_v, current_a = labels[0]
    accumulated = clip_specs[0]['end'] - clip_specs[0]['start']
    # Frame-exact target length. Per-clip rounding at non-integer durations
    # would otherwise accumulate past one frame over many hard cuts.
    target_frames = round(accumulated * OUT_FPS)
    for boundary in range(len(clip_specs) - 1):
        next_v, next_a = labels[boundary + 1]
        transition = (
            transition_specs[boundary]
            if boundary < len(transition_specs) else 0.0
        )
        out_v, out_a = f'cv{boundary}', f'ca{boundary}'
        clip_frames = round(
            (clip_specs[boundary + 1]['end']
             - clip_specs[boundary + 1]['start']) * OUT_FPS)
        transition_frames = round(transition * OUT_FPS)
        if transition > 0.0:
            offset = accumulated - transition
            lines.append(
                f'[{current_v}][{next_v}]xfade=transition=fade:'
                f'duration={transition:.6f}:offset={offset:.6f}[{out_v}]')
            lines.append(
                f'[{current_a}][{next_a}]acrossfade=d={transition:.6f}:'
                f'c1=qsin:c2=qsin[{out_a}]')
            accumulated = accumulated + (
                clip_specs[boundary + 1]['end']
                - clip_specs[boundary + 1]['start']
            ) - transition
        else:
            lines.append(f'[{current_v}][{next_v}]concat=n=2:v=1:a=0[{out_v}]')
            lines.append(f'[{current_a}][{next_a}]concat=n=2:v=0:a=1[{out_a}]')
            accumulated += (
                clip_specs[boundary + 1]['end']
                - clip_specs[boundary + 1]['start']
            )
        target_frames += clip_frames - transition_frames
        current_v, current_a = out_v, out_a

    # Pin both streams to the exact target length so rounding inside each
    # normalised clip cannot accumulate (video: frame count, audio: samples).
    final_v, final_a = 'vout', 'aout'
    target_samples = target_frames * OUT_SAMPLE_RATE // OUT_FPS
    lines.append(
        f'[{current_v}]trim=end_frame={target_frames},setpts=PTS-STARTPTS[{final_v}]')
    lines.append(
        f'[{current_a}]atrim=end_sample={target_samples},'
        f'asetpts=PTS-STARTPTS[{final_a}]')
    return inputs, ';\n'.join(lines), final_v, final_a, accumulated


def _start_ffmpeg(cmd, work_dir):
    creationflags = 0
    if os.name == 'nt':
        # Detach from the console so the whole process tree can be killed.
        creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    return subprocess.Popen(
        cmd, cwd=str(work_dir), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace',
        creationflags=creationflags,
    )


def _terminate(process):
    if process.poll() is not None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except ProcessLookupError:
        pass


def render(request, output, temp_dir, is_cancelled=None, register_process=None):
    """Render one export.

    Writes to ``temp_dir`` first, verifies the result and only then lets the
    caller publish it. Raises :class:`RenderCancelled` when the cancellation
    flag flips while ffmpeg is running. ``register_process`` (if given) is
    called with the spawned Popen so the caller can track/terminate it.
    """
    is_cancelled = is_cancelled or (lambda: False)
    register_process = register_process or (lambda proc: None)
    output = Path(output)
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    clips = request.get('clips') or []
    probed = probe_sources(clips)
    clip_specs, transition_specs, expected = validate_request(request, probed)
    inputs, graph, final_v, final_a, _ = _build_filter_graph(
        clip_specs, transition_specs)

    # Absolute because ffmpeg runs with temp_dir as its working directory.
    temp_output = (temp_dir / 'export.mp4').resolve()
    cmd = [
        'ffmpeg', '-hide_banner', '-nostdin', '-y',
        '-loglevel', 'error',
        *inputs,
        '-filter_complex', graph,
        '-map', f'[{final_v}]', '-map', f'[{final_a}]',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
        '-pix_fmt', 'yuv420p', '-r', str(OUT_FPS),
        '-c:a', 'aac', '-b:a', '160k',
        '-ar', str(OUT_SAMPLE_RATE), '-ac', '2',
        '-movflags', '+faststart',
        str(temp_output),
    ]

    process = _start_ffmpeg(cmd, temp_dir)
    register_process(process)

    def _watchdog():
        # Poll so a cancellation flips into a process kill within ~0.2 s.
        while True:
            if process.poll() is not None:
                return
            if is_cancelled():
                _terminate(process)
                return
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()
    stderr_tail, _ = process.communicate()
    watchdog.join(timeout=2)

    if is_cancelled():
        raise RenderCancelled('任务已取消')
    if process.returncode != 0:
        tail = (stderr_tail or '').strip()[-LOG_TAIL_LIMIT:]
        raise RenderError(
            f'ffmpeg 退出码 {process.returncode}: '
            f'{tail or "无错误输出"}')

    verify_output(temp_output, expected)
    output.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_output, output)
    return expected


def verify_output(path, expected_duration):
    """Probe and fully decode the rendered file before it is published."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 1024:
        raise RenderError('渲染结果缺失或过小，判定为不完整文件')
    probe = _run_ffprobe(path)
    streams = probe.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if video is None or audio is None:
        raise RenderError('渲染结果缺少视频流或音频流')
    problems = []
    if int(video.get('width') or 0) != OUT_WIDTH or int(video.get('height') or 0) != OUT_HEIGHT:
        problems.append(f"分辨率 {video.get('width')}x{video.get('height')}")
    if video.get('pix_fmt') != 'yuv420p':
        problems.append(f"像素格式 {video.get('pix_fmt')}")
    if video.get('codec_name') != 'h264':
        problems.append(f"视频编码 {video.get('codec_name')}")
    if audio.get('codec_name') != 'aac':
        problems.append(f"音频编码 {audio.get('codec_name')}")
    if int(audio.get('sample_rate') or 0) != OUT_SAMPLE_RATE:
        problems.append(f"采样率 {audio.get('sample_rate')}")
    if int(audio.get('channels') or 0) != 2:
        problems.append(f"声道数 {audio.get('channels')}")

    actual_duration = _stream_duration(video, probe.get('format', {}))
    if actual_duration is None:
        raise RenderError('无法读取渲染结果时长')
    if abs(actual_duration - expected_duration) > FRAME_DURATION + 1e-3:
        raise RenderError(
            f'输出时长 {actual_duration:.3f}s 与理论值 {expected_duration:.3f}s '
            f'偏差超过一帧')

    # Full decode of every frame and every audio packet; corrupt MP4s fail here.
    decode = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'null', '-'],
        capture_output=True, text=True, timeout=300,
    )
    if decode.returncode != 0 or decode.stderr.strip():
        raise RenderError(
            '输出完整性解码失败: '
            + decode.stderr.strip()[-LOG_TAIL_LIMIT:])

    # Audio must reach the same end as video (no -shortest style truncation).
    audio_duration = _stream_duration(audio, probe.get('format', {}))
    if audio_duration is not None and abs(audio_duration - actual_duration) > 0.05:
        raise RenderError(
            f'音视频末端偏差 {abs(audio_duration - actual_duration) * 1000:.0f}ms '
            '超过 50ms')
    if problems:
        raise RenderError('输出规格不符: ' + '; '.join(problems))
