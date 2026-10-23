import collections,concurrent.futures,hashlib,json,math,os,random,re,shutil,statistics,subprocess,sys,tarfile,threading,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path

WORK=Path('work');RESULTS=WORK/'results';WORK.mkdir(exist_ok=True);RESULTS.mkdir(exist_ok=True)
BASE='https://ptsmovies.online/api/v1';TOKEN=os.environ['WORKER_TOKEN'];JOB=os.environ['AUDIT_JOB'];ATTEMPT=os.environ['AUDIT_ATTEMPT']
STATE={'progress':0,'message':'Starting read-only quality audit'};STOP=threading.Event();LENGTH=12;FRACTIONS=(.06,.18,.30,.42,.54,.66,.78,.90)
VMAF=Path('vmaf/libvmaf/build/tools/vmaf').resolve()

def save(name,doc):
    (RESULTS/name).write_text(json.dumps(doc,indent=2,allow_nan=False))

def report():
    data={'job_id':JOB,'attempt':ATTEMPT,'stage':'compressing','progress':STATE['progress'],'message':STATE['message'],'runner':os.environ.get('GITHUB_RUN_ID','')}
    request=urllib.request.Request(BASE+'/worker/progress',data=json.dumps(data).encode(),headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json','User-Agent':'PTS-quality-audit'})
    try:
        with urllib.request.urlopen(request,timeout=20) as response:response.read()
    except Exception:pass

def heartbeat():
    while not STOP.wait(15):report()

def stage(progress,message):
    STATE.update(progress=progress,message=message);report();print('AUDIT_STAGE',progress,message,flush=True)

def endpoint(asset):
    return BASE+'/worker/source?'+urllib.parse.urlencode({'job':JOB,'attempt':ATTEMPT,'asset':asset})

def access(asset):
    request=urllib.request.Request(endpoint(asset),headers={'Authorization':'Bearer '+TOKEN,'User-Agent':'PTS-quality-audit'})
    with urllib.request.urlopen(request,timeout=60) as response:return json.load(response)

def download(role,destination,url=None):
    if url is None:url=endpoint(role);headers={'Authorization':'Bearer '+TOKEN,'User-Agent':'PTS-quality-audit'}
    else:
        print('::add-mask::'+url.replace('%','%25').replace('\r','%0D').replace('\n','%0A'),flush=True)
        headers={'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36','Accept':'*/*'}
    started=time.monotonic();h=hashlib.sha256();n=0
    try:
        request=urllib.request.Request(url,headers=headers)
        with urllib.request.urlopen(request,timeout=120) as response,destination.open('wb') as output:
            expected=int(response.headers.get('Content-Length') or 0)
            for block in iter(lambda:response.read(1024*1024),b''):
                if time.monotonic()-started>1800:raise TimeoutError('Source download exceeded audit budget')
                if shutil.disk_usage('.').free<1024**3:raise RuntimeError('Runner disk reserve reached')
                output.write(block);h.update(block);n+=len(block)
        if expected and n!=expected:raise RuntimeError('Downloaded byte count differs from content length')
    except urllib.error.HTTPError as error:raise RuntimeError(role+' download rejected with HTTP '+str(error.code)) from None
    except urllib.error.URLError:raise RuntimeError(role+' download encountered a network error') from None
    result={'bytes':n,'sha256':h.hexdigest(),'seconds':round(time.monotonic()-started,3)}
    print('AUDIT_DOWNLOADED',role,json.dumps(result),flush=True);return result

def run(command,timeout=600,check=True):
    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if check and result.returncode:raise RuntimeError('Local analysis command failed: '+result.stderr.decode(errors='replace')[-1500:])
    return result

def ff(args,timeout=600):return run(['ffmpeg','-nostdin','-hide_banner','-v','error','-threads','2',*map(str,args)],timeout)

def probe(file):
    doc=json.loads(run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(file)],120).stdout)
    fields=('index','codec_type','codec_name','profile','width','height','pix_fmt','sample_aspect_ratio','display_aspect_ratio','r_frame_rate','avg_frame_rate','bit_rate','sample_rate','channels','channel_layout','color_range','color_space','color_transfer','color_primaries','start_time','duration','nb_frames')
    streams=[]
    for stream in doc['streams']:
        item={key:stream[key] for key in fields if key in stream}
        tags=stream.get('tags') or {}
        item['tags']={key:re.sub(r'https?://\S+','[redacted]',str(value)) for key,value in tags.items() if key.lower() in ('language','title','encoder','bps','number_of_frames','duration')}
        streams.append(item)
    return {'streams':streams,'format':{key:doc['format'][key] for key in ('format_name','duration','size','bit_rate','start_time') if key in doc['format']}}

def packets(file):
    command=['ffprobe','-v','error','-show_packets','-show_entries','packet=stream_index,size,pts_time,duration_time,flags','-of','compact=p=0',str(file)]
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    counts={};keys={}
    for line in proc.stdout:
        values=dict(part.split('=',1) for part in line.strip().split('|') if '=' in part)
        if 'stream_index' not in values or 'size' not in values:continue
        index=int(values['stream_index']);entry=counts.setdefault(index,{'payload_bytes':0,'packets':0,'packet_duration':0.0})
        entry['payload_bytes']+=int(values['size']);entry['packets']+=1
        try:entry['packet_duration']+=float(values.get('duration_time') or 0)
        except ValueError:pass
        if 'K' in values.get('flags',''):
            try:keys.setdefault(index,[]).append(float(values['pts_time']))
            except (ValueError,KeyError):pass
    if proc.wait(timeout=30):raise RuntimeError('Packet inventory failed')
    for index,entry in counts.items():
        if entry['packet_duration']>0:entry['payload_kbps']=entry['payload_bytes']*8/entry['packet_duration']/1000
        intervals=[b-a for a,b in zip(keys.get(index,[]),keys.get(index,[])[1:]) if b>a]
        if intervals:entry['keyframe_intervals']={'median':statistics.median(intervals),'max':max(intervals),'p95':sorted(intervals)[int((len(intervals)-1)*.95)]}
    return counts

def encoder_settings(file,codec):
    if codec not in ('hevc','h264'):return {}
    bitstream='hevc' if codec=='hevc' else 'h264'
    raw=ff(['-i',file,'-t','2','-map','0:v:0','-c','copy','-bsf:v',bitstream+'_mp4toannexb','-f',bitstream,'pipe:1'],60)
    text=raw.stdout.decode('latin1',errors='ignore')
    offset=text.find('x265 (build')
    if offset<0:offset=text.find('x265' if codec=='hevc' else 'x264')
    if offset<0:return {}
    text=text[offset:offset+16000].split('\x00')[0]
    allowed={'crf','bitrate','keyint','min-keyint','bframes','b-adapt','ref','rc-lookahead','scenecut','rd','rdoq-level','aq-mode','aq-strength','qcomp','psy-rd','psy-rdoq','vbv-maxrate','vbv-bufsize','deblock','sao','open-gop','cutree','frame-threads','wpp','range','colorprim','transfer','colormatrix','bit-depth','ctu','tu-intra-depth','tu-inter-depth','rect','amp','me','subme','merange'}
    found={key:value.strip() for key,value in re.findall(r'([a-z0-9-]+)=([^/\x00]+)',text) if key in allowed}
    found['version']=text.split(' - ',1)[0][:160]
    return found

def extract_archive(file,destination,metadata):
    allowed=set(metadata['files'])|{'pts-hls.json','pts-info.json'}
    destination.mkdir()
    with tarfile.open(file) as source:
        for member in source:
            name=member.name
            while name.startswith('./'):name=name[2:]
            if name in ('','.') and member.isdir():continue
            path=Path(name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):raise RuntimeError('Unsafe Pi archive member')
            if member.isdir():
                if not re.fullmatch(r'v\d{1,2}',name.rstrip('/')):raise RuntimeError('Unexpected archive folder')
                (destination/path).mkdir(parents=True,exist_ok=True);continue
            if name not in allowed:raise RuntimeError('Unexpected Pi archive file')
            target=destination/path;target.parent.mkdir(parents=True,exist_ok=True)
            with source.extractfile(member) as incoming,target.open('xb') as output:shutil.copyfileobj(incoming,output,1024*1024)
    for name,expected in metadata['files'].items():
        actual=destination/name
        if actual.stat().st_size!=expected['size']:raise RuntimeError('Pi archive size mismatch')
        h=hashlib.sha256()
        with actual.open('rb') as incoming:
            for block in iter(lambda:incoming.read(1024*1024),b''):h.update(block)
        if h.hexdigest()!=expected['sha256']:raise RuntimeError('Pi archive checksum mismatch')

def video(doc):return next(stream for stream in doc['streams'] if stream['codec_type']=='video')
def fps(doc):
    value=video(doc)['avg_frame_rate'];n,d=value.split('/');return float(n)/float(d)

def dimensions(width,height,limit):
    factor=min(1,(limit*16/9)/width,limit/height)
    return max(2,int(width*factor)//2*2),max(2,int(height*factor)//2*2)

def cropdetect(file,duration):
    crops=[]
    for fraction in (.2,.5,.8):
        command=['ffmpeg','-nostdin','-hide_banner','-v','info','-threads','2','-ss',str(duration*fraction),'-i',str(file),'-t','3','-an','-vf','cropdetect=24:2:0','-f','null','-']
        output=run(command,90,check=False).stderr.decode(errors='replace')
        matches=re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)',output)
        if matches:crops.append(collections.Counter(matches).most_common(1)[0][0])
    if not crops:return None
    chosen,count=collections.Counter(crops).most_common(1)[0]
    return list(map(int,chosen)) if count>=2 else None

def normalized_filter(crop,width,height):
    prefix='crop='+':'.join(map(str,crop))+',' if crop else ''
    return prefix+'scale='+str(width)+':'+str(height)+':flags=lanczos,setsar=1,format=yuv420p'

def gray(file,position,crop=None,seconds=None,rate=1):
    args=['-ss',max(0,position),'-i',file]
    if seconds is None:args+=['-frames:v','1']
    else:args+=['-t',seconds]
    prefix='crop='+':'.join(map(str,crop))+',' if crop else ''
    args+=['-an','-vf',prefix+'scale=96:40,format=gray'+(',fps='+str(rate) if seconds is not None else ''),'-f','rawvideo','pipe:1']
    return ff(args,180).stdout

def ncc(a,b):
    if not a or len(a)!=len(b):return -1
    ma=sum(a)/len(a);mb=sum(b)/len(b);aa=[v-ma for v in a];bb=[v-mb for v in b]
    scale=math.sqrt(sum(v*v for v in aa)*sum(v*v for v in bb))
    return sum(x*y for x,y in zip(aa,bb))/scale if scale else -1

def alignment(original,other,position,crop=None):
    center=position+LENGTH/2;anchor=gray(original,center)
    start=max(0,center-20);data=gray(other,start,crop,seconds=40,rate=2);frame_bytes=96*40
    candidates=[(ncc(anchor,data[i:i+frame_bytes]),start+i/frame_bytes/2) for i in range(0,len(data)-frame_bytes+1,frame_bytes)]
    if not candidates:return {'offset':0,'ncc':-1,'usable':False}
    coarse=max(candidates)
    start=max(0,coarse[1]-.75);data=gray(other,start,crop,seconds=1.5,rate=24)
    fine=[(ncc(anchor,data[i:i+frame_bytes]),start+i/frame_bytes/24) for i in range(0,len(data)-frame_bytes+1,frame_bytes)]
    score,found=max(fine or [coarse])
    return {'offset':round(found-center,6),'ncc':round(score,6),'usable':score>=.88}

def clip(file,start,destination,length=LENGTH):
    ff(['-ss',max(0,start),'-i',file,'-t',length,'-map','0:v:0','-an','-sn','-vf','setpts=PTS-STARTPTS','-c:v','ffv1','-level','3','-g','1','-threads','2','-y',destination],300)

def metric_input(file,destination,crop,width,height,rate):
    ff(['-i',file,'-t',LENGTH,'-an','-vf',normalized_filter(crop,width,height)+',fps='+str(rate),'-pix_fmt','yuv420p','-f','yuv4mpegpipe','-y',destination],180)

def metric(reference,distorted,path):
    command=[str(VMAF),'-r',str(reference),'-d',str(distorted),'--model','version=vmaf_v0.6.1:name=vmaf','--model','version=vmaf_v0.6.1neg:name=vmaf_neg','--feature','psnr','--feature','float_ssim','--feature','cambi','--threads','2','--subsample','2','--json','--output',str(path),'--quiet']
    run(command,240);doc=json.loads(path.read_text());pooled=doc.get('pooled_metrics',{})
    return {name:{key:values[key] for key in ('mean','min','max') if key in values} for name,values in pooled.items() if name in ('vmaf','vmaf_neg','psnr_y','float_ssim','cambi')}

def encode(source,destination,profile,source_doc):
    stream=video(source_doc);width,height=dimensions(stream['width'],stream['height'],profile['height'])
    filt='scale='+str(width)+':'+str(height)+':flags=lanczos'
    if profile.get('denoise'):filt='hqdn3d=0.7:0.7:2:2,'+filt
    command=['-i',source,'-an','-sn','-c:v','libx265','-preset',profile['preset'],'-crf',profile['crf'],'-vf',filt,'-pix_fmt',profile.get('pix_fmt','yuv420p'),'-force_key_frames','expr:gte(t,n_forced*'+str(profile.get('gop',2))+')','-x265-params','log-level=error:pools=2:frame-threads=2','-tag:v','hvc1','-movflags','+faststart','-y',destination]
    started=time.monotonic();ff(command,600)
    stats=packets(destination);payload=sum(entry['payload_bytes'] for entry in stats.values())
    return {'payload_bytes':payload,'container_bytes':destination.stat().st_size,'seconds':time.monotonic()-started,'width':width,'height':height}

def mean_metric(samples,name):
    values=[entry['metrics'][name]['mean'] for entry in samples if name in entry.get('metrics',{})]
    return statistics.mean(values) if values else None

def contact_sheet(clips,rows,selected,width,height):
    folder=WORK/'contact';folder.mkdir(exist_ok=True)
    columns=[('original','Original 1080 source'),('pi','Current Pi 540'),('reference','Internet reference'),(selected,selected)]
    number=0
    for index in range(len(rows)):
        for key,label in columns:
            file=clips.get(key,[])[index] if key in clips else WORK/'variants'/key/('scene-'+str(index)+'.mp4')
            crop=rows[index].get(key+'_crop') if key in ('reference','pi') else None
            filt=normalized_filter(crop,width,height)+',scale=320:134,pad=320:180:0:23:black,drawtext=text=\''+label+'\':fontcolor=white:fontsize=13:x=5:y=3'
            ff(['-ss',LENGTH/2,'-i',file,'-frames:v','1','-vf',filt,'-y',folder/('%03d.jpg'%number)],90);number+=1
    ff(['-framerate','1','-i',folder/'%03d.jpg','-vf','tile=4x'+str(len(rows)),'-frames:v','1','-q:v','2','-y',RESULTS/'contact-sheet.jpg'],90)
    shutil.rmtree(folder)

def main():
    threading.Thread(target=heartbeat,daemon=True).start()
    stage(1,'Downloading original, reference and read-only Pi HLS audit inputs')
    info=access('info');metadata=info['hls'];save('pi-storage.json',{'size':metadata['size'],'duration':metadata['duration'],'width':metadata['width'],'height':metadata['height'],'audio':metadata['audio']})
    downloads={};paths={'original':WORK/'original.src','reference':WORK/'reference.mkv','pi':WORK/'pi/master.m3u8'}
    for role in ('original','reference'):downloads[role]=download(role,paths[role],access(role)['url'])
    archive=WORK/'pi.tar';downloads['pi_archive']=download('pi',archive);extract_archive(archive,WORK/'pi',metadata);archive.unlink()
    save('downloads.json',downloads)
    stage(15,'Inventorying streams, packet bitrates, keyframes and encoder settings')
    docs={name:probe(file) for name,file in paths.items()};inventories={name:packets(file) for name,file in paths.items()}
    settings={name:encoder_settings(file,video(docs[name])['codec_name']) for name,file in paths.items()}
    duration=float(docs['original']['format']['duration']);rate=fps(docs['original'])
    storage={'pi_media_bytes':metadata['size'],'reference_file_bytes':downloads['reference']['bytes'],'original_file_bytes':downloads['original']['bytes'],'renditions':{}}
    for name,entry in metadata['files'].items():
        group=name.split('/')[0];storage['renditions'][group]=storage['renditions'].get(group,0)+entry['size']
    save('inventory.json',{'media':docs,'packets':inventories,'encoder_settings':settings,'storage':storage})
    stream=video(docs['original']);eval_w=1280;eval_h=max(2,round(stream['height']/stream['width']*eval_w/2)*2)
    crops={name:cropdetect(file,float(docs[name]['format']['duration'])) for name,file in paths.items()}
    for name,crop in list(crops.items()):
        s=video(docs[name]);area=s['width']*s['height']
        if crop and (crop[0]*crop[1]>.95*area or abs(crop[0]/crop[1]-stream['width']/stream['height'])>.08):crops[name]=None
    positions=[math.floor(duration*fraction/2)*2 for fraction in FRACTIONS]
    rows=[];clips={name:[] for name in paths};folder=WORK/'clips';folder.mkdir()
    stage(22,'Aligning eight scenes and extracting lossless audit-only clips')
    for index,position in enumerate(positions):
        row={'index':index,'source_start':position}
        for name,file in paths.items():
            align={'offset':0,'ncc':1,'usable':True} if name=='original' else alignment(paths['original'],file,position,crops[name])
            row[name+'_alignment']=align;row[name+'_crop']=crops[name]
            destination=folder/(name+'-'+str(index)+'.mkv');clip(file,position+align['offset'],destination);clips[name].append(destination)
        rows.append(row);save('alignment.json',{'scenes':rows,'evaluation_width':eval_w,'evaluation_height':eval_h,'metric_sampling':'Every second native frame, using VMAF subsample=2','source_fps':rate})
    for name,file in paths.items():
        if name=='pi':shutil.rmtree(WORK/'pi')
        else:file.unlink()
    profiles=[
        {'name':'540_medium23','height':540,'preset':'medium','crf':23},
        {'name':'540_slow23','height':540,'preset':'slow','crf':23},
        {'name':'540_slow24','height':540,'preset':'slow','crf':24},
        {'name':'540_slow25','height':540,'preset':'slow','crf':25},
        {'name':'540_slow26','height':540,'preset':'slow','crf':26},
        {'name':'540_slower25','height':540,'preset':'slower','crf':25},
        {'name':'540_slow25_10bit','height':540,'preset':'slow','crf':25,'pix_fmt':'yuv420p10le'},
        {'name':'540_slow25_denoise','height':540,'preset':'slow','crf':25,'denoise':True},
        {'name':'540_slow25_gop4','height':540,'preset':'slow','crf':25,'gop':4},
        {'name':'720_slow26','height':720,'preset':'slow','crf':26},
        {'name':'720_slow28','height':720,'preset':'slow','crf':28},
    ]
    output={'profiles':[],'current_samples':[],'reference_samples':[],'scene_count':len(rows),'scene_seconds':LENGTH,'evaluation_resolution':[eval_w,eval_h],'duration':duration,'storage':storage,'media':docs,'encoder_settings':settings,'alignment':rows,'metric_frame_subsample':2}
    variants=WORK/'variants';variants.mkdir();metric_folder=WORK/'metrics';metric_folder.mkdir()
    original_eval=[]
    for index,file in enumerate(clips['original']):
        dest=metric_folder/('original-'+str(index)+'.y4m');metric_input(file,dest,None,eval_w,eval_h,rate);original_eval.append(dest)
    for label,result_key in [('pi','current_samples'),('reference','reference_samples')]:
        for index,file in enumerate(clips[label]):
            if not rows[index][label+'_alignment']['usable']:continue
            distorted=metric_folder/'distorted.y4m';metric_input(file,distorted,crops[label],eval_w,eval_h,rate)
            scores=metric(original_eval[index],distorted,RESULTS/(label+'-scene-'+str(index)+'.json'));distorted.unlink()
            output[result_key].append({'scene':index,'metrics':scores})
    for profile_index,profile in enumerate(profiles):
        stage(30+int(profile_index/len(profiles)*60),'Benchmarking '+profile['name']+' against original frames')
        location=variants/profile['name'];location.mkdir();samples=[]
        for index,source in enumerate(clips['original']):
            destination=location/('scene-'+str(index)+'.mp4');entry=encode(source,destination,profile,docs['original'])
            distorted=metric_folder/'distorted.y4m';metric_input(destination,distorted,None,eval_w,eval_h,rate)
            entry['metrics']=metric(original_eval[index],distorted,RESULTS/(profile['name']+'-scene-'+str(index)+'.json'));distorted.unlink();entry['scene']=index;samples.append(entry)
        entry={'profile':profile,'samples':samples,'sample_payload_bytes':sum(s['payload_bytes'] for s in samples),'encoding_seconds':sum(s['seconds'] for s in samples),'vmaf_mean':mean_metric(samples,'vmaf'),'vmaf_neg_mean':mean_metric(samples,'vmaf_neg'),'psnr_y_mean':mean_metric(samples,'psnr_y'),'ssim_mean':mean_metric(samples,'float_ssim')}
        output['profiles'].append(entry);save('result-partial.json',output)
    baseline=output['profiles'][0];pi_video_index=video(docs['pi'])['index'];current_payload=inventories['pi'][pi_video_index]['payload_bytes'];constant=metadata['size']-current_payload
    random.seed(20261023)
    for entry in output['profiles']:
        ratio=entry['sample_payload_bytes']/baseline['sample_payload_bytes'];entry['projected_full_bytes']=round(current_payload*ratio+constant);entry['projected_saved_percent']=(1-entry['projected_full_bytes']/metadata['size'])*100
        projections=[]
        for _ in range(1000):
            indices=[random.randrange(len(rows)) for _ in rows];candidate=sum(entry['samples'][i]['payload_bytes'] for i in indices);reference=sum(baseline['samples'][i]['payload_bytes'] for i in indices);projections.append(current_payload*candidate/reference+constant)
        projections.sort();entry['paired_sample_projection_p05_p95_bytes']=[round(projections[50]),round(projections[950])]
        entry['encoding_time_relative_to_medium23']=entry['encoding_seconds']/baseline['encoding_seconds']
    output['current_vmaf_mean']=mean_metric(output['current_samples'],'vmaf');output['current_vmaf_neg_mean']=mean_metric(output['current_samples'],'vmaf_neg');output['reference_vmaf_mean']=mean_metric(output['reference_samples'],'vmaf');output['reference_vmaf_neg_mean']=mean_metric(output['reference_samples'],'vmaf_neg')
    viable=[entry for entry in output['profiles'] if entry['profile'].get('gop',2)==2 and not entry['profile'].get('denoise') and entry['profile'].get('pix_fmt','yuv420p')=='yuv420p' and entry['profile']['height']==540 and entry['vmaf_neg_mean'] is not None and output['current_vmaf_neg_mean'] is not None and entry['vmaf_neg_mean']>=output['current_vmaf_neg_mean']-1.5]
    selected=min(viable,key=lambda entry:entry['projected_full_bytes']) if viable else baseline
    output['conservative_candidate']=selected['profile']['name'];output['projection_warning']='Sample-based projections, calibrated to the current full movie. Paired bootstrap spread is not a guaranteed full-movie size or a formal whole-film confidence interval.'
    save('result.json',output)
    stage(95,'Rendering visual comparisons and writing audit report')
    contact_sheet(clips,rows,selected['profile']['name'],eval_w,eval_h)
    lines=['# The Paradise compression audit','',f"Original: {storage['original_file_bytes']:,} bytes. Current Pi HLS: {storage['pi_media_bytes']:,} bytes. Internet reference: {storage['reference_file_bytes']:,} bytes.",'',f'Evaluation grid: {eval_w}x{eval_h}; {len(rows)} scenes x {LENGTH}s; every second native frame scored; VMAF and VMAF NEG plus PSNR/SSIM/banding metrics.','', '| Profile | Projected GB | Save % | VMAF | VMAF NEG | Relative encode time |','|---|---:|---:|---:|---:|---:|']
    for entry in output['profiles']:lines.append('| '+entry['profile']['name']+' | '+f"{entry['projected_full_bytes']/1e9:.3f} | {entry['projected_saved_percent']:.1f} | {entry['vmaf_mean']:.2f} | {entry['vmaf_neg_mean']:.2f} | {entry['encoding_time_relative_to_medium23']:.2f}x |")
    lines+=['',output['projection_warning'],'','Production media and compression settings were not changed. Denoise, 10-bit and four-second GOP cases are experiments requiring separate approval/compatibility checks.','', 'Frame alignment confidence is recorded per scene; unaligned reference/current samples are excluded from comparative quality scores. Pixel count is not a guarantee of perceptual detail.']
    (RESULTS/'summary.md').write_text('\n'.join(lines));stage(100,'Quality audit complete; results ready for review')
    print('AUDIT_RESULT_SUMMARY '+json.dumps({'current_bytes':metadata['size'],'reference_bytes':downloads['reference']['bytes'],'candidate':output['conservative_candidate'],'profiles':len(profiles)}),flush=True)

if __name__=='__main__':
    try:main()
    except BaseException as error:
        safe=re.sub(r'https?://\S+','[redacted]',str(error));save('failure.json',{'type':type(error).__name__,'message':safe[:3000]});print('AUDIT_FAILURE',type(error).__name__,safe[:1500],file=sys.stderr);raise
    finally:STOP.set()
