# Timeline Export

一个可可靠导出多素材时间线的视频拼接服务。Python 3.12+，需要 `ffmpeg` /
`ffprobe` 在 PATH 上。所有渲染都由服务真正调用 ffmpeg 子进程完成，并在
发布前对成品做完整解码校验。

## 能力

- 按 `clips` 顺序拼接，每段用 `in` / `out`（素材呈现时间，秒）裁切，PTS
  正确归零，支持非零起始时间戳的素材。
- 相邻片段可在 `transition_seconds` 中按边界顺序设置过渡：`0` 为硬切，
  `>0` 为视频 `xfade` 交叉淡化 + 音频 `acrossfade`（等功率 qsin 曲线），
  过渡时长从总时间线中扣除。
- 混合输入自动归一化：24/25/30fps 与 VFR、44.1/48kHz、单声道/无音轨、
  横竖不同分辨率。无音轨片段用等长静音补齐，不会因 shortest 提前结束。
- 固定输出：1280×720、30fps、H.264 yuv420p、48kHz 双声道 AAC；等比例缩放
  后补黑边（letterbox/pillarbox），绝不拉伸。
- 任务持久化在 SQLite；先写临时文件，校验通过后原子发布，不发布半成品。
- 任务并行执行，ffmpeg 并发数有上限；支持取消（结束对应 ffmpeg 并清理该
  任务临时文件，不影响其他任务）；服务重启后旧的 queued/running 任务被
  显式置为 failed，残缺 MP4 不会被当作成功。
- 有界错误日志（滚动文件），失败原因写入任务状态。

## 安装与启动

```bash
python -m pip install -r requirements.txt

# 1) 本地生成测试素材（全部由 ffmpeg lavfi 合成，不下载外部视频）
python tools/make_fixtures.py --output fixtures/generated

# 2) 启动服务（MEDIA_ROOT 控制数据库、临时文件、成品和日志，默认 var/）
python -m uvicorn media.api:app --host 127.0.0.1 --port 8237
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MEDIA_ROOT` | `var` | 任务库、`tmp/`、`outputs/`、`errors.log` 根目录 |
| `MEDIA_WORKERS` | `2` | 工作线程（队列并行度） |
| `MEDIA_MAX_FFMPEG` | `2` | 同时运行的 ffmpeg 子进程上限 |

## 提交任务

`transition_seconds` 长度为“相邻片段边界数”（clips 数减 1），可只给部分
边界，省略的边界按硬切处理。

```bash
curl -s -X POST http://127.0.0.1:8237/exports \
  -H 'Content-Type: application/json' \
  -d '{
    "clips": [
      {"path": "fixtures/generated/landscape24.mp4", "in": 0.2, "out": 2.0},
      {"path": "fixtures/generated/portrait25.mp4",  "in": 0.0, "out": 1.5},
      {"path": "fixtures/generated/silent30.mp4",    "in": 0.5, "out": 2.5}
    ],
    "transition_seconds": [0.4, 0]
  }'
# -> {"id":"<job-id>","state":"queued"}
```

理论成片时长 = 各段 (out-in) 之和 − 过渡时长之和。

## 查询、取消与下载

```bash
# 查询状态：queued / running / succeeded / failed / cancelled
curl -s http://127.0.0.1:8237/exports/<job-id>

# 取消（仅作用于该任务的 ffmpeg 与临时目录）
curl -s -X POST http://127.0.0.1:8237/exports/<job-id>/cancel

# 仅 succeeded 可下载，否则返回 409
curl -s -o result.mp4 http://127.0.0.1:8237/exports/<job-id>/download
```

失败任务的 `error` 字段给出具体原因，例如：

- `片段 2 的文件不存在: ...`
- `输入不可解码 ...: ...`
- `片段 1 裁切越界: out=99s 超过素材时长 3.000s (...)`
- `转场 1 时长 1.5s 不短于其相邻片段（1s / 1s）`
- `服务重启，任务未完成，已终止`

## 验证产物

```bash
ffprobe -v error -show_format -show_streams result.mp4

# 完整解码，检查是否有损坏帧/音频包
ffmpeg -v error -i result.mp4 -f null -

# 抽帧人工/程序检查（硬切、交叉淡化、黑边）
ffmpeg -v error -ss 1.75 -i result.mp4 -frames:v 1 frame.png
```

## 测试

集成测试用 ffprobe 与真实解码（rawvideo 帧、PCM/WAV 音频）断言实际输出，
而不是只看退出码或文件是否存在。每个用例使用独立的临时 `MEDIA_ROOT`。

```bash
python -m pytest -q
```

覆盖场景：

- 混合 24/25/30fps 与 VFR、44.1/48kHz、非零 PTS、无音轨、单声道、横竖屏；
- 硬切与 xfade/acrossfade 交叉淡化（检查过渡前后素材来源、混合帧、无黑闪、
  无冻结帧）、静音补齐、等比缩放补黑边、固定输出规格；
- 时长与理论值偏差 ≤ 1 帧、音视频末端偏差 ≤ 50ms；
- 越界裁切、过长转场、缺失文件、不可解码输入均 failed 且带具体原因；
- 并发任务中取消一个：对应 ffmpeg 被结束、临时文件清理、其他任务成功；
- ffmpeg 并发数不超过上限；
- 服务重启后旧 running 任务（含残缺 MP4）被置为 failed 且不可下载，新任务
  仍可正常导出。

## 目录结构

```
media/api.py     FastAPI 接口（提交/查询/取消/下载）与启动恢复
media/jobs.py    SQLite 持久化、有界线程池与 ffmpeg 信号量、取消、清理
media/render.py  ffprobe 校验、filter_complex 构建、子进程管理、成品校验与原子发布
tools/make_fixtures.py  合成带颜色标识带和测试音的素材（含 VFR、非零 PTS、静音、竖屏）
tests/           可重复执行的集成测试
```
