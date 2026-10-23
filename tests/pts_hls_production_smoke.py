import importlib.util,json,os,subprocess,threading
from pathlib import Path

spec=importlib.util.spec_from_file_location('pipeline','scripts/pts_pipeline.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
p.JOB_ID=os.environ['JOB_ID'];p.ATTEMPT=os.environ['ATTEMPT'];p.CALLBACK='https://ptsmovies.online/api/v1';p.TOKEN=os.environ['WORKER_TOKEN'];p.RUN_ID=os.environ['GITHUB_RUN_ID'];p.LABEL='Disposable HLS verification';p.BUDGET=p.RunnerBudget()
Path('work').mkdir()
command=['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=960x540:rate=24']
for hz in (330,440,550,660):command+=['-f','lavfi','-i','sine=frequency='+str(hz)+':sample_rate=48000']
command+=['-t','24','-map','0:v']
for i in range(1,5):command+=['-map',str(i)+':a']
command+=['-c:v','libx264','-preset','ultrafast','-crf','26','-g','48','-c:a','aac','-ac','2','-b:a','160k']
for i,lang in enumerate(('eng','hin','tam','tel')):command+=['-metadata:s:a:'+str(i),'language='+lang]
command+=['-movflags','+faststart','work/source.mp4']
subprocess.run(command,check=True,timeout=120)
p.compress('work/source.mp4','work/encoded.mp4')
doc=p.probe_media('work/encoded.mp4')
video=next(s for s in doc['streams'] if s['codec_type']=='video');audio=[s for s in doc['streams'] if s['codec_type']=='audio']
assert video['codec_name']=='hevc' and video['height']==540
assert len(audio)==4 and all(s['codec_name']=='aac' and s['channels']==2 and 118000<=int(s['bit_rate'])<=135000 for s in audio)
archive=p.package_hls('work/encoded.mp4','work/hls')
metadata=json.loads(Path('work/hls/pts-hls.json').read_text())
assert len(metadata['audio'])==4 and metadata['audio_bitrate_target']==128000
assert Path('work/hls/master.m3u8').read_text().count('#EXT-X-STREAM-INF:')==1
p.upload(str(archive),'.hls.tar')
print('HLS_SMOKE_RESULT '+json.dumps({'height':video['height'],'codec':video['codec_name'],'audio_bitrates':[int(s['bit_rate']) for s in audio],'languages':[s['tags']['language'] for s in audio],'bytes':metadata['size']}))
