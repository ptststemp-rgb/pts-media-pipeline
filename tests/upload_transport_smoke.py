import hashlib,importlib.util,json,os,urllib.parse
from pathlib import Path

spec=importlib.util.spec_from_file_location('pts_live_upload','scripts/pts_pipeline.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
runner.JOB_ID=os.environ['JOB_ID'];runner.ATTEMPT=os.environ['ATTEMPT'];runner.TOKEN=os.environ['WORKER_TOKEN'];runner.CALLBACK='https://ptsmovies.online/api/v1'
assert os.environ.get('UPLOAD_ORIGIN_IP') and os.environ.get('UPLOAD_CA_PEM'),'Direct upload configuration is missing'
check=runner.Uploader(runner.CALLBACK)
assert check.origin_ip and check.origin_context.check_hostname and check.origin_context.verify_mode!=0
original=runner.Uploader.post
def diagnostic_post(self,query,chunk):
 params=urllib.parse.parse_qs(query)
 params['seq']=[str(1000+int(params['seq'][0]))]
 params['last']=['0']
 return original(self,urllib.parse.urlencode({k:v[0] for k,v in params.items()}),chunk)
runner.Uploader.post=diagnostic_post
block=os.urandom(8*1024*1024);path=Path(os.environ['RUNNER_TEMP'])/'pts-upload-transport-smoke.bin'
digest=hashlib.sha256()
with path.open('wb') as f:
 for _ in range(33):f.write(block);digest.update(block)
import time
started=time.monotonic()
runner.upload(path)
seconds=time.monotonic()-started
path.unlink()
runner.Uploader.post=original
fallback=runner.Uploader(runner.CALLBACK);fallback.origin_ip='127.0.0.1'
query=urllib.parse.urlencode({'job':runner.JOB_ID,'attempt':runner.ATTEMPT,'seq':2001,'last':0,'ext':'.mp4'})
errors=0
for _ in range(2):
 try:fallback.post(query,block)
 except (OSError,TimeoutError):errors+=1
 if not fallback.origin_ip:break
assert errors and not fallback.origin_ip,'Direct-route failure did not trigger safe fallback'
fallback.post(query,block)
print('SMOKE_RESULT '+json.dumps({'bytes':33*len(block),'seconds':round(seconds,3),'MiB_per_second':round(264/seconds,3),'workers':runner.upload_worker_count(),'first_seq':1001,'last_seq':1033,'file_sha256':digest.hexdigest(),'chunk_sha256':hashlib.sha256(block).hexdigest(),'fallback_seq':2001,'fallback_verified':True,'tls_verified':True}),flush=True)
