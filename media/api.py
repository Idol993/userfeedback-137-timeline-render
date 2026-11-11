from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from . import jobs

app = FastAPI(title='Timeline Export')


class Clip(BaseModel):
    path: str
    start: float = Field(alias='in')
    end: float = Field(alias='out')

    @field_validator('start', 'end')
    @classmethod
    def _finite(cls, value):
        if value != value or value in (float('inf'), float('-inf')):
            raise ValueError('time values must be finite')
        return value


class ExportRequest(BaseModel):
    clips: list[Clip]
    transition_seconds: list[float] = []

    @field_validator('transition_seconds')
    @classmethod
    def _finite_transitions(cls, values):
        for value in values:
            if value != value or value in (float('inf'), float('-inf')) or value < 0:
                raise ValueError('transition durations must be finite and non-negative')
        return values


@asynccontextmanager
async def lifespan(_app):
    # Jobs left queued/running by a previous process must not hang forever.
    jobs.recover_stale_jobs()
    yield
    jobs.shutdown()


app.router.lifespan_context = lifespan


@app.post('/exports', status_code=202)
def create_export(body: ExportRequest):
    ident = jobs.submit(body.model_dump(by_alias=True))
    return {'id': ident, 'state': 'queued'}


@app.get('/exports/{job_id}')
def get_export(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, 'unknown export')
    return job


@app.post('/exports/{job_id}/cancel')
def cancel_export(job_id: str):
    job = jobs.cancel(job_id)
    if not job:
        raise HTTPException(404, 'unknown export')
    return job


@app.get('/exports/{job_id}/download')
def download(job_id: str):
    job = jobs.get(job_id)
    if not job or job['state'] != 'succeeded' or not job['output']:
        raise HTTPException(409, 'export is not ready')
    from pathlib import Path
    if not Path(job['output']).is_file():
        raise HTTPException(409, 'export is not ready')
    return FileResponse(
        job['output'], media_type='video/mp4', filename=job_id + '.mp4')
