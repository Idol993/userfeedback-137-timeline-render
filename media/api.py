from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from . import jobs

app=FastAPI(title='Timeline Export')
class Clip(BaseModel):
    path:str
    start:float=Field(0,alias='in')
    end:float=Field(alias='out')
class ExportRequest(BaseModel):
    clips:list[Clip]
    transition_seconds:list[float]=[]
@app.post('/exports',status_code=202)
def create_export(body:ExportRequest):
    ident=jobs.submit(body.model_dump(by_alias=True))
    return {'id':ident,'state':'queued'}
@app.get('/exports/{job_id}')
def get_export(job_id:str):
    job=jobs.get(job_id)
    if not job: raise HTTPException(404,'unknown export')
    return job
@app.get('/exports/{job_id}/download')
def download(job_id:str):
    job=jobs.get(job_id)
    if not job or job['state']!='succeeded': raise HTTPException(409,'export is not ready')
    return FileResponse(job['output'],media_type='video/mp4',filename=job_id+'.mp4')
