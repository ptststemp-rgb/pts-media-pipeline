import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.parse

WORK = Path(os.environ['RUNNER_TEMP']) / 'pts-hls-trial'
WORK.mkdir()
PACKAGE = WORK / 'package'
PACKAGE.mkdir()
spec = importlib.util.spec_from_file_location('pts_upload', 'scripts/pts_pipeline.py')
uploader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uploader)
uploader.JOB_ID = os.environ['JOB_ID']
uploader.ATTEMPT = os.environ['ATTEMPT']
uploader.TOKEN = os.environ['WORKER_TOKEN']
uploader.CALLBACK = 'https://ptsmovies.online/api/v1'
source = uploader.CALLBACK + '/worker/hls-trial-source?' + urllib.parse.urlencode({'attempt': uploader.ATTEMPT})
baseline = WORK / 'reference-10min.mp4'
started = time.monotonic()
command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning', '-user_agent', 'pts-runner', '-headers', 'Authorization: Bearer ' + uploader.TOKEN + '\r\n', '-i', source, '-t', '600', '-map', '0:v:0', '-map', '0:a', '-c', 'copy', '-tag:v', 'hvc1', '-movflags', '+faststart', str(baseline)]
completed = subprocess.run(command, capture_output=True, text=True, timeout=1200)
if completed.returncode:
    raise RuntimeError('Source excerpt could not be copied: ' + completed.stderr.replace(uploader.TOKEN, '[redacted]')[-1500:])
probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(baseline)]))
video = next(stream for stream in probe['streams'] if stream['codec_type'] == 'video')
audio = [stream for stream in probe['streams'] if stream['codec_type'] == 'audio']
assert video['codec_name'] == 'hevc' and video['codec_tag_string'] == 'hvc1'
assert max(video['width'], video['height']) <= 960 and min(video['width'], video['height']) <= 540
assert len(audio) == 4 and all(stream['codec_name'] == 'aac' and stream['channels'] == 2 for stream in audio)
languages = [stream.get('tags', {}).get('language', 'und') for stream in audio]
assert languages == ['eng', 'hin', 'tam', 'tel'], languages
duration = float(probe['format']['duration'])
assert 599 <= duration <= 601, duration
for number in range(5):
    (PACKAGE / ('v' + str(number))).mkdir()
variants = ['v:0,agroup:audio'] + ['a:' + str(i) + ',agroup:audio,language:' + lang + ',default:' + ('yes' if i == 0 else 'no') for i, lang in enumerate(languages)]
subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning', '-i', str(baseline), '-map', '0:v:0', '-map', '0:a', '-c', 'copy', '-tag:v', 'hvc1', '-f', 'hls', '-hls_time', '2', '-hls_playlist_type', 'vod', '-hls_segment_type', 'fmp4', '-hls_flags', 'independent_segments', '-hls_fmp4_init_filename', 'init_%v.mp4', '-hls_segment_filename', str(PACKAGE / 'v%v/seg_%05d.m4s'), '-var_stream_map', ' '.join(variants), '-master_pl_name', 'master.m3u8', str(PACKAGE / 'v%v/index.m3u8')], check=True, timeout=300)
assert (PACKAGE / 'master.m3u8').is_file()
master = (PACKAGE / 'master.m3u8').read_text()
initialization = (PACKAGE / 'v0/init_0.mp4').read_bytes()
offset = initialization.index(b'hvcC') + 4
configuration = initialization[offset:offset + 13]
compatibility = int(format(int.from_bytes(configuration[2:6], 'big'), '032b')[::-1], 2)
constraints = list(configuration[6:12])
while constraints and constraints[-1] == 0:
    constraints.pop()
codec = 'hvc1.' + ('', 'A', 'B', 'C')[configuration[1] >> 6] + str(configuration[1] & 31) + '.' + format(compatibility, 'X') + '.' + ('H' if configuration[1] & 32 else 'L') + str(configuration[12]) + ''.join('.' + format(value, 'X') for value in constraints)
master = re.sub(r'hvc1[^,\"]+', codec, master, count=1)
(PACKAGE / 'master.m3u8').write_text(master)
assert master.count('#EXT-X-MEDIA:TYPE=AUDIO') == 4
assert master.count('#EXT-X-STREAM-INF:') == 1
assert 'hvc1' in master

def stream_hash(file, stream):
    return subprocess.check_output(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(file), '-map', stream, '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'], text=True, timeout=120).strip().split('=', 1)[1]

hashes = {}
for number in range(5):
    source_stream = '0:v:0' if number == 0 else '0:a:' + str(number - 1)
    target_stream = '0:v:0' if number == 0 else '0:a:0'
    expected = stream_hash(baseline, source_stream)
    actual = stream_hash(PACKAGE / ('v' + str(number)) / 'index.m3u8', target_stream)
    assert actual == expected, 'Copied media packets differ for variant ' + str(number)
    hashes['video' if number == 0 else languages[number - 1]] = actual
assets = [file for file in PACKAGE.rglob('*') if file.is_file()]
hls_bytes = sum(file.stat().st_size for file in assets)
report = {
    'title': 'Moana — HLS Test (10 min)',
    'duration': duration,
    'width': video['width'], 'height': video['height'],
    'video_codec': 'hevc', 'audio_codec': 'aac',
    'video_mime': 'video/mp4; codecs="' + codec + '"',
    'languages': languages, 'video_renditions': 1,
    'segment_seconds': 2,
    'mp4_bytes': baseline.stat().st_size, 'hls_bytes': hls_bytes,
    'overhead_percent': (hls_bytes / baseline.stat().st_size - 1) * 100,
    'file_count': len(assets),
    'stream_copy_verified': True, 'encoded_packet_sha256': hashes,
    'media_file_sha256': {str(file.relative_to(PACKAGE)): hashlib.sha256(file.read_bytes()).hexdigest() for file in assets},
    'package_seconds': round(time.monotonic() - started, 3)
}
(PACKAGE / 'report.json').write_text(json.dumps(report, indent=2))
archive = WORK / 'hls-trial.tar'
with tarfile.open(archive, 'w') as output:
    for file in sorted(PACKAGE.rglob('*')):
        output.add(file, arcname=str(file.relative_to(PACKAGE)), recursive=False)
assert archive.stat().st_size < 256 * 1024 * 1024
original_post = uploader.Uploader.post
def nonpublishing(self, query, data):
    values = urllib.parse.parse_qs(query)
    values['last'] = ['0']
    return original_post(self, urllib.parse.urlencode({key: value[0] for key, value in values.items()}), data)
uploader.Uploader.post = nonpublishing
uploader.upload(archive)
summary = {key: value for key, value in report.items() if key not in ('media_file_sha256', 'encoded_packet_sha256')}
summary.update({'archive_bytes': archive.stat().st_size, 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'chunks': (archive.stat().st_size + uploader.CHUNK - 1) // uploader.CHUNK})
print('HLS_TRIAL_RESULT ' + json.dumps(summary), flush=True)
