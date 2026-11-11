import json, subprocess
from pathlib import Path

def probe(path):
    p=subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)],capture_output=True,text=True,check=True)
    return json.loads(p.stdout)

def render(request,output):
    output=Path(output); output.parent.mkdir(parents=True,exist_ok=True)
    listing=output.with_suffix('.concat.txt')
    listing.write_text(''.join("file '"+str(Path(c['path']).resolve()).replace('\\','/')+"'\n" for c in request['clips']),encoding='utf-8')
    completed=subprocess.run(['ffmpeg','-hide_banner','-y','-f','concat','-safe','0','-i',str(listing),'-c','copy',str(output)],capture_output=True,text=True)
    if completed.returncode: raise RuntimeError(completed.stderr)
