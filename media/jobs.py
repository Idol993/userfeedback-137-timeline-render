"""Persistent export job management.

Jobs live in SQLite; render work runs in a bounded thread pool and the
number of concurrently running ffmpeg subprocesses is limited separately.
Each job renders into its own temporary directory which is removed once the
job reaches a terminal state. Cancellation terminates only that job's
ffmpeg process. On startup, jobs left ``queued``/``running`` by a previous
process are failed explicitly so they never hang and partial MP4s are never
published.
"""

import json
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .render import RenderCancelled, RenderError, render

MAX_WORKERS = int(os.environ.get('MEDIA_WORKERS', '2'))
MAX_FFMPEG = int(os.environ.get('MEDIA_MAX_FFMPEG', '2'))
ERROR_LOG_LIMIT = 4000

_TERMINAL_STATES = {'succeeded', 'failed', 'cancelled'}

# Job id -> running ffmpeg Popen (used by cancellation and tests).
active = {}
_active_lock = threading.Lock()
# Job id -> cancellation event.
_cancellers = {}
_canceller_lock = threading.Lock()
# Bounds actual ffmpeg subprocesses independently of queued work.
_ffmpeg_slots = threading.Semaphore(MAX_FFMPEG)
_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix='export')
_log = logging.getLogger('media.jobs')


def root():
    path = Path(os.environ.get('MEDIA_ROOT', 'var'))
    path.mkdir(parents=True, exist_ok=True)
    (path / 'tmp').mkdir(parents=True, exist_ok=True)
    (path / 'outputs').mkdir(parents=True, exist_ok=True)
    return path


def _connect():
    db = sqlite3.connect(root() / 'jobs.db', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=30000')
    db.execute(
        'CREATE TABLE IF NOT EXISTS jobs('
        'id TEXT PRIMARY KEY, state TEXT, request TEXT, output TEXT, '
        'error TEXT, created_at REAL DEFAULT (strftime(\'%s\',\'now\')))')
    return db


def _configure_file_log():
    if _log.handlers:
        return
    handler = RotatingFileHandler(
        root() / 'errors.log', maxBytes=256 * 1024, backupCount=2,
        encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    _log.addHandler(handler)
    _log.setLevel(logging.INFO)


def get(job_id):
    with _connect() as db:
        row = db.execute(
            'SELECT id,state,request,output,error FROM jobs WHERE id=?',
            (job_id,)).fetchone()
        return dict(row) if row else None


def _update(job_id, **values):
    if not values:
        return
    keys = ','.join(f'{key}=?' for key in values)
    with _connect() as db:
        db.execute(
            f'UPDATE jobs SET {keys} WHERE id=?',
            [*values.values(), job_id])


def _output_path(job_id):
    return root() / 'outputs' / f'{job_id}.mp4'


def _temp_dir(job_id):
    return root() / 'tmp' / job_id


def _cleanup(job_id):
    shutil.rmtree(_temp_dir(job_id), ignore_errors=True)


def _set_active(job_id, process):
    with _active_lock:
        active[job_id] = process


def _clear_active(job_id):
    with _active_lock:
        active.pop(job_id, None)


def _execute(job_id):
    job = get(job_id)
    if job is None or job['state'] in _TERMINAL_STATES:
        return
    event = threading.Event()
    with _canceller_lock:
        # Registered before the state flips to running so cancel() never sees
        # a running job without a cancellation event.
        _cancellers[job_id] = event
    if get(job_id)['state'] in _TERMINAL_STATES:
        # cancel() may have finalised the queued job in the meantime.
        with _canceller_lock:
            _cancellers.pop(job_id, None)
        return
    _update(job_id, state='running', error=None)
    work = _temp_dir(job_id)
    work.mkdir(parents=True, exist_ok=True)
    try:
        with _ffmpeg_slots:
            if event.is_set():
                raise RenderCancelled('任务已取消')
            expected = render(
                json.loads(job['request']),
                _output_path(job_id),
                work,
                is_cancelled=event.is_set,
                register_process=lambda proc: _set_active(job_id, proc),
            )
        if event.is_set():
            raise RenderCancelled('任务已取消')
        _update(
            job_id, state='succeeded',
            output=str(_output_path(job_id).resolve()), error=None)
        _log.info('job %s succeeded, expected duration %.3fs', job_id, expected)
    except RenderCancelled:
        _safe_unlink(_output_path(job_id))
        _update(job_id, state='cancelled', error='任务已取消', output=None)
        _log.info('job %s cancelled', job_id)
    except RenderError as exc:
        _safe_unlink(_output_path(job_id))
        message = str(exc)[:ERROR_LOG_LIMIT]
        _update(job_id, state='failed', error=message, output=None)
        _log.error('job %s failed: %s', job_id, message)
    except Exception as exc:  # pragma: no cover - defensive boundary
        _safe_unlink(_output_path(job_id))
        message = f'内部错误: {exc}'[:ERROR_LOG_LIMIT]
        _update(job_id, state='failed', error=message, output=None)
        _log.exception('job %s crashed', job_id)
    finally:
        _clear_active(job_id)
        with _canceller_lock:
            _cancellers.pop(job_id, None)
        _cleanup(job_id)


def _safe_unlink(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        _log.warning('could not remove %s', path)


def submit(request):
    _configure_file_log()
    ident = str(uuid.uuid4())
    with _connect() as db:
        db.execute(
            'INSERT INTO jobs(id,state,request,output,error) VALUES(?,?,?,?,?)',
            (ident, 'queued', json.dumps(request, ensure_ascii=False), None, None))
    _pool.submit(_execute, ident)
    return ident


def cancel(job_id):
    """Request cancellation. Returns the job record or None if unknown."""
    job = get(job_id)
    if job is None:
        return None
    if job['state'] in _TERMINAL_STATES:
        return job
    with _canceller_lock:
        event = _cancellers.get(job_id)
    if event is not None:
        # Running (or about to run): the watchdog terminates its ffmpeg and
        # the worker itself flips the state to cancelled.
        event.set()
    elif job['state'] == 'queued':
        # Still queued with no worker attached; mark directly.
        _update(job_id, state='cancelled', error='任务已取消', output=None)
        _cleanup(job_id)
    return get(job_id)


def recover_stale_jobs():
    """Fail jobs left non-terminal by a previous process and clean their files."""
    _configure_file_log()
    with _connect() as db:
        rows = db.execute(
            "SELECT id FROM jobs WHERE state IN ('queued','running')"
        ).fetchall()
        for row in rows:
            job_id = row['id']
            db.execute(
                'UPDATE jobs SET state=?, error=?, output=? WHERE id=?',
                ('failed', '服务重启，任务未完成，已终止', None, job_id))
    for row in rows:
        job_id = row['id']
        _safe_unlink(_output_path(job_id))
        shutil.rmtree(_temp_dir(job_id), ignore_errors=True)
        _log.warning('recovered stale job %s as failed', job_id)
    return len(rows)


def shutdown():
    _pool.shutdown(wait=False, cancel_futures=True)
