"""Failure handling, cancellation, concurrency limits and restart recovery."""

import time
from pathlib import Path

from tests._media_helpers import wait_state
from tests.conftest import submit


def clip(fixtures, name, start=0.0, end=1.0):
    return {'path': str(Path(fixtures) / name), 'in': start, 'out': end}


def test_bad_inputs_report_reasons(client, fixtures):
    cases = [
        # Missing file.
        ([{'path': str(Path(fixtures) / 'nope.mp4'), 'in': 0, 'out': 1}], []),
        # Undecodable file.
        ([clip(fixtures, 'broken.mp4')], []),
        # Trim beyond source duration (sources are ~3 s long).
        ([clip(fixtures, 'silent30.mp4', 0.0, 9.0)], []),
        # Inverted trim window.
        ([clip(fixtures, 'silent30.mp4', 2.0, 1.0)], []),
        # Transition as long as the shorter adjacent clip.
        ([clip(fixtures, 'silent30.mp4', 0.0, 1.0),
          clip(fixtures, 'landscape24.mp4', 0.0, 1.0)], [1.0]),
        # Transition longer than the timeline itself.
        ([clip(fixtures, 'silent30.mp4', 0.0, 1.0),
          clip(fixtures, 'landscape24.mp4', 0.0, 1.0)], [1.5]),
    ]
    for clips, transitions in cases:
        job_id = submit(client, clips, transitions)
        job = wait_state(client, job_id, 'failed')
        assert job['state'] == 'failed'
        assert job['error'] and len(job['error']) > 5
        assert job['output'] is None
        # A failed job must not be downloadable and leaves no published file.
        resp = client.get(f'/exports/{job_id}/download')
        assert resp.status_code == 409


def test_transition_list_too_long_rejected(client, fixtures):
    body = {'clips': [clip(fixtures, 'silent30.mp4', 0, 1)],
            'transition_seconds': [0.2]}
    resp = client.post('/exports', json=body)
    job_id = resp.json()['id']
    job = wait_state(client, job_id, 'failed')
    assert 'transition' in job['error']


def test_cancel_concurrent_job_keeps_other_running(client, fixtures, media_root):
    _, jobs, _ = media_root
    # Long jobs so several ffmpeg processes are alive at the same time.
    long_clips = [
        clip(fixtures, 'landscape24.mp4', 0.0, 2.9),
        clip(fixtures, 'vfr.mp4', 0.0, 2.9),
        clip(fixtures, 'silent30.mp4', 0.0, 2.9),
        clip(fixtures, 'mono30.mp4', 0.0, 2.9),
    ]
    ids = [submit(client, long_clips, [0.3, 0.3, 0.3]) for _ in range(3)]

    # Wait until at least one job has a live ffmpeg process, then cancel it.
    deadline = time.time() + 20
    while time.time() < deadline and not jobs.active:
        time.sleep(0.05)
    assert jobs.active, 'no ffmpeg process ever became active'
    target = next(iter(jobs.active))
    process = jobs.active[target]
    resp = client.post(f'/exports/{target}/cancel')
    assert resp.status_code == 200

    cancelled = wait_state(client, target, 'cancelled', timeout=30)
    assert cancelled['state'] == 'cancelled'
    assert process.poll() is not None, 'ffmpeg subprocess was not terminated'

    # No output file, no temp directory left for the cancelled job.
    root = Path(jobs.get(target)['output']) if jobs.get(target)['output'] else None
    assert root is None or not root.exists()
    assert not (jobs.root() / 'tmp' / target).exists()

    # The other jobs are untouched and at least one succeeds.
    others = [i for i in ids if i != target]
    succeeded = 0
    for other in others:
        job = wait_state(client, other, 'succeeded', timeout=90)
        if job['state'] == 'succeeded':
            succeeded += 1
            dl = client.get(f'/exports/{other}/download')
            assert dl.status_code == 200
    assert succeeded >= 1


def test_ffmpeg_concurrency_is_limited(client, fixtures, media_root):
    _, jobs, _ = media_root
    long_clips = [
        clip(fixtures, 'landscape24.mp4', 0.0, 2.9),
        clip(fixtures, 'silent30.mp4', 0.0, 2.9),
        clip(fixtures, 'mono30.mp4', 0.0, 2.9),
    ]
    peak = 0
    ids = [submit(client, long_clips, [0.2, 0.2]) for _ in range(5)]
    deadline = time.time() + 30
    while time.time() < deadline:
        peak = max(peak, len(jobs.active))
        states = [client.get(f'/exports/{i}').json()['state'] for i in ids]
        if all(s in ('succeeded', 'failed', 'cancelled') for s in states):
            break
        time.sleep(0.05)
    assert peak <= 2, f'peak concurrent ffmpeg was {peak}'
    for job_id in ids:
        assert wait_state(client, job_id, 'succeeded')['state'] == 'succeeded'


def test_restart_recovers_stale_running_jobs(media_root, fixtures):
    root, jobs, api = media_root
    # Simulate a job that was mid-flight when the previous process died,
    # including a half-written output that must never be treated as success.
    job_id = jobs.submit(
        {'clips': [clip(fixtures, 'silent30.mp4', 0.0, 2.0)],
         'transition_seconds': []})
    with jobs._connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
    stale_output = jobs._output_path(job_id)
    stale_output.parent.mkdir(parents=True, exist_ok=True)
    stale_output.write_bytes(b'partial mp4 data')
    stale_tmp = jobs._temp_dir(job_id)
    stale_tmp.mkdir(parents=True, exist_ok=True)
    (stale_tmp / 'export.mp4').write_bytes(b'partial')

    # Re-import the service as a fresh process would.
    import importlib
    importlib.reload(jobs)
    recovered = jobs.recover_stale_jobs()
    assert recovered == 1
    job = jobs.get(job_id)
    assert job['state'] == 'failed'
    assert '重启' in job['error']
    assert job['output'] is None
    assert not stale_output.exists()
    assert not stale_tmp.exists()

    # A brand-new service instance still serves new work correctly.
    from fastapi.testclient import TestClient
    with TestClient(api.app) as new_client:
        new_id = submit(new_client, [clip(fixtures, 'silent30.mp4', 0.0, 1.5)])
        done = wait_state(new_client, new_id, 'succeeded')
        assert done['state'] == 'succeeded'
        assert new_client.get(f'/exports/{new_id}/download').status_code == 200
        # The stale job is still rejected after restart.
        assert new_client.get(f'/exports/{job_id}/download').status_code == 409


def test_cancel_unknown_and_terminal_job(client, fixtures):
    assert client.post('/exports/does-not-exist/cancel').status_code == 404
    job_id = submit(client, [clip(fixtures, 'silent30.mp4', 0.0, 0.5)])
    done = wait_state(client, job_id, 'succeeded')
    resp = client.post(f'/exports/{job_id}/cancel')
    assert resp.status_code == 200
    assert resp.json()['state'] == 'succeeded'
