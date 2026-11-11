import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import make_fixtures  # noqa: E402


@pytest.fixture(scope='session')
def fixtures(tmp_path_factory):
    directory = tmp_path_factory.mktemp('fixtures')
    make_fixtures.make(str(directory))
    return directory


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    root = tmp_path / 'var'
    monkeypatch.setenv('MEDIA_ROOT', str(root))
    # Fresh module state (thread pool, semaphore, DB connection) per test.
    for name in ('media.jobs', 'media.api', 'media.render'):
        sys.modules.pop(name, None)
    import media.jobs as jobs
    import media.api as api
    jobs.recover_stale_jobs()
    yield root, jobs, api
    jobs.shutdown()


@pytest.fixture
def client(media_root):
    _, _, api = media_root
    with TestClient(api.app) as test_client:
        yield test_client


def submit(client, clips, transitions=None):
    body = {'clips': clips}
    if transitions is not None:
        body['transition_seconds'] = transitions
    response = client.post('/exports', json=body)
    assert response.status_code == 202, response.text
    return response.json()['id']
