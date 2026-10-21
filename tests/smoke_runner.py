import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


signal.alarm(240)
spec = importlib.util.spec_from_file_location('pipeline', Path(sys.argv[1]).resolve())
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)
pipeline.report = lambda **fields: print(fields.get('message', ''), flush=True)

with tempfile.TemporaryDirectory(prefix='pts-quality-smoke-') as directory:
    os.chdir(directory)
    Path('work').mkdir()
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=24',
        '-f', 'lavfi', '-i', 'sine=frequency=500:sample_rate=48000',
        '-f', 'lavfi', '-i', 'anullsrc=channel_layout=5.1:sample_rate=48000',
        '-t', '18', '-map', '0:v', '-map', '1:a', '-map', '2:a', '-ac:a:0', '2',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '16', '-threads', '2',
        '-c:a:0', 'aac', '-b:a:0', '96000', '-c:a:1', 'eac3', '-b:a:1', '192000',
        '-metadata:s:a:0', 'language=eng', '-metadata:s:a:1', 'language=tam', 'source.mkv'],
        check=True, timeout=45)
    pipeline.compress('source.mkv', 'output.mkv')
    doc = pipeline.probe_media('output.mkv')
    video = next(s for s in doc['streams'] if s['codec_type'] == 'video')
    audio = [s for s in doc['streams'] if s['codec_type'] == 'audio']
    assert video['codec_name'] == 'hevc'
    assert (video['width'], video['height']) == (1280, 720)
    assert [s['codec_name'] for s in audio] == ['aac', 'aac']
    assert [s['channels'] for s in audio] == [2, 6]
    assert [s['tags']['language'] for s in audio] == ['eng', 'tam']
    assert Path('output.mkv').stat().st_size < Path('source.mkv').stat().st_size
    print('SMOKE VERIFIED ' + json.dumps({'resolution': '1280x720', 'video': 'HEVC',
        'audio': 'AAC stereo + AAC 5.1', 'source_bytes': Path('source.mkv').stat().st_size,
        'output_bytes': Path('output.mkv').stat().st_size}), flush=True)
