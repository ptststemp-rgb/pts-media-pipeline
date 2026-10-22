import concurrent.futures,hashlib,http.client,json,os,socket,ssl,threading,time
from pathlib import Path

HOST='ptsmovies.online'
ORIGIN='150.230.136.95'
root=Path(__file__).with_name('cloudflare-origin-ca.pem').read_text()
context=ssl.create_default_context(cadata=root)
class DirectHTTPS(http.client.HTTPSConnection):
 def connect(self):
  raw=socket.create_connection((ORIGIN,443),timeout=self.timeout)
  try:self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
  except BaseException:raw.close();raise
def connection(route):
 return DirectHTTPS(HOST,timeout=120,context=context) if route=='direct' else http.client.HTTPSConnection(HOST,timeout=120)
for route in ['cloudflare','direct']:
 conn=connection(route)
 try:
  conn.request('POST','/api/v1/worker/upload?job=not-a-real-job',body=b'x',headers={'Authorization':'Bearer invalid-test-token','Content-Type':'application/octet-stream','User-Agent':'pts-upload-route-check'})
  response=conn.getresponse();response.read()
  if response.status!=401:raise RuntimeError(route+' unauthenticated status '+str(response.status))
  print('AUTH '+json.dumps({'route':route,'status':response.status,'tls_verified':True}),flush=True)
 finally:conn.close()
payload=os.urandom(8*1024*1024)
digest=hashlib.sha256(payload).hexdigest()
wide=os.environ.get('MODE')=='wide'
sequence=100 if wide else 0
results=[]
cases=[('cloudflare',1,2),('direct',1,2),('cloudflare',8,1),('direct',8,1),('direct',16,1),('cloudflare',16,1),('direct',8,1),('cloudflare',8,1),('cloudflare',16,1),('direct',16,1)]
if wide:cases=[('direct',8,1),('direct',16,1),('direct',32,1),('cloudflare',8,1),('cloudflare',32,1),('direct',16,1),('direct',32,1)]
for route,workers,parts in cases:
 first=sequence+1
 sequence+=workers*parts
 started=time.monotonic()
 def push(worker):
  conn=connection(route)
  try:
   for part in range(parts):
    seq=first+worker*parts+part
    path='/api/v1/worker/upload?job='+os.environ['JOB_ID']+'&attempt='+os.environ['ATTEMPT']+'&seq='+str(seq)+'&last=0&ext=.mp4'
    conn.request('POST',path,body=payload,headers={'Authorization':'Bearer '+os.environ['WORKER_TOKEN'],'Content-Type':'application/octet-stream','User-Agent':'pts-upload-route-check'})
    response=conn.getresponse();body=response.read()
    if response.status!=200 or json.loads(body).get('received')!=len(payload):raise RuntimeError(route+' upload status '+str(response.status))
  finally:conn.close()
 with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:list(pool.map(push,range(workers)))
 elapsed=time.monotonic()-started
 size=len(payload)*workers*parts
 result={'route':route,'streams':workers,'MiB':size//1048576,'seconds':round(elapsed,3),'MiB_per_second':round(size/1048576/elapsed,3),'first_seq':first,'last_seq':sequence,'chunk_sha256':digest}
 print('RESULT '+json.dumps(result),flush=True)
 results.append(result)
print('SUMMARY '+json.dumps(results),flush=True)
