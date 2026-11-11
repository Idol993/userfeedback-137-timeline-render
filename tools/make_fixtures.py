import argparse, subprocess
from pathlib import Path

def make(root):
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    specs=[('landscape24.mp4','640x360',24,44100,440,False,0),
           ('portrait25.mp4','360x640',25,48000,880,False,0),
           ('silent30.mp4','800x450',30,48000,0,True,0),
           ('offset30.mp4','640x360',30,48000,660,False,2)]
    for name,size,fps,rate,hz,silent,offset in specs:
        cmd=['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i',f'testsrc2=size={size}:rate={fps}:duration=3']
        if not silent: cmd+=['-f','lavfi','-i',f'sine=frequency={hz}:sample_rate={rate}:duration=3']
        cmd+=['-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-threads','2']
        if not silent: cmd+=['-c:a','aac','-ar',str(rate)]
        if offset: cmd+=['-output_ts_offset',str(offset)]
        cmd += [str(root/name)]
        subprocess.run(cmd,check=True)
    (root/'broken.mp4').write_bytes(b'not an mp4')
    return root
if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--output',default='fixtures/generated')
    print(make(parser.parse_args().output))
