import os, sqlite3, uuid, json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from .render import render

pool=ThreadPoolExecutor(max_workers=2)
def root():
    p=Path(os.environ.get('MEDIA_ROOT','var')); p.mkdir(parents=True,exist_ok=True); return p
def connect():
    db=sqlite3.connect(root()/'jobs.db',timeout=10); db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,state TEXT,request TEXT,output TEXT,error TEXT)')
    return db
def get(job_id):
    with connect() as db:
        row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        return dict(row) if row else None
def update(job_id,**values):
    with connect() as db: db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',[*values.values(),job_id])
def execute(job_id):
    job=get(job_id); update(job_id,state='running')
    output=root()/(job_id+'.mp4')
    try:
        render(json.loads(job['request']),output)
        update(job_id,state='succeeded',output=str(output.resolve()))
    except Exception as exc: update(job_id,state='failed',error=str(exc))
def submit(request):
    ident=str(uuid.uuid4())
    with connect() as db: db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',(ident,'queued',json.dumps(request),None,None))
    pool.submit(execute,ident)
    return ident
