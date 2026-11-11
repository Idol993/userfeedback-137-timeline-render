# Timeline Export

Python 3.12+, FFmpeg/ffprobe on PATH.

```text
python -m pip install -r requirements.txt
python tools/make_fixtures.py
python -m uvicorn media.api:app --host 127.0.0.1 --port 8237
python -m pytest -q
```

POST `/exports` takes `clips: [{path, in, out}, ...]` and `transition_seconds: [0.4, 0, ...]`.
Transitions describe boundaries between consecutive clips; omitted entries mean hard cuts.
Times are seconds relative to the presentation beginning of each source.
GET `/exports/{id}` returns persisted state. GET `/exports/{id}/download` returns the successful MP4.
`MEDIA_ROOT` controls all runtime data (default `var/`).

The baseline concatenates complete sources and copies streams; it does not yet honor timeline trimming or transitions.
The generated assets contain moving test patterns, different tones, one portrait source, one silent source and a non-zero PTS source.
The test media is synthetic and may be regenerated locally; it is not committed to Git.
