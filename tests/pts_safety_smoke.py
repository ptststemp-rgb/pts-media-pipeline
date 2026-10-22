import hashlib,importlib.util,json,os,tempfile,time,urllib.parse
from pathlib import Path

spec=importlib.util.spec_from_file_location('pts_safety','scripts/pts_pipeline.py');runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
runner.JOB_ID=os.environ['JOB_ID'];runner.ATTEMPT=os.environ['ATTEMPT'];runner.TOKEN=os.environ['WORKER_TOKEN'];runner.CALLBACK='https://ptsmovies.online/api/v1'
source=runner.fetch_source_url()
assert urllib.parse.urlsplit(source).hostname=='ptsmovies.online'
print('Privacy probe: '+source,flush=True)
original=runner.Uploader.post
def nonpublishing(self,query,data):
 values=urllib.parse.parse_qs(query);values['seq']=[str(4000+int(values['seq'][0]))];values['last']=['0']
 return original(self,urllib.parse.urlencode({k:v[0] for k,v in values.items()}),data)
runner.Uploader.post=nonpublishing
block=os.urandom(8*1024*1024);digest=hashlib.sha256();path=Path(os.environ['RUNNER_TEMP'])/'safety-upload.bin'
with path.open('wb') as output:
 for _ in range(8):output.write(block);digest.update(block)
started=time.monotonic();runner.upload(path);elapsed=time.monotonic()-started;path.unlink()
print('SAFETY_RESULT '+json.dumps({'bytes':64*1024*1024,'seconds':round(elapsed,3),'chunks':8,'workers':runner.upload_worker_count(),'sha256':digest.hexdigest()}),flush=True)
