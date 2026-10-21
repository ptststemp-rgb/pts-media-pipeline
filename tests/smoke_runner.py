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
        '-f', 'lavfi', '-i', 'sine=frequency=800:sample_rate=48000',
        '-t', '18', '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:a',
        '-ac:a:0', '2', '-ac:a:2', '2',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '16', '-threads', '2',
        '-c:a:0', 'aac', '-b:a:0', '128000',
        '-c:a:1', 'eac3', '-b:a:1', '384000',
        '-c:a:2', 'aac', '-b:a:2', '128000',
        '-metadata:s:a:0', 'language=eng',
        '-metadata:s:a:1', 'language=eng',
        '-metadata:s:a:2', 'language=tam',
        'source.mkv'],
        check=True, timeout=60)
    pipeline.compress('source.mkv', 'output.mp4')
    doc = pipeline.probe_media('output.mp4')
    video = next(s for s in doc['streams'] if s['codec_type'] == 'video')
    audio = [s for s in doc['streams'] if s['codec_type'] == 'audio']
    assert video['codec_name'] == 'hevc', 'video codec'
    assert video.get('codec_tag_string') == 'hvc1' or (video.get('tags') or {}).get('codec_tag_string') == 'hvc1', 'hvc1 tag'
    assert (video['width'], video['height']) == (960, 540), 'resolution %dx%d' % (video['width'], video['height'])
    assert len(audio) == 2, 'expected 2 audio tracks (one per language), got %d' % len(audio)
    assert [s['codec_name'] for s in audio] == ['aac', 'aac'], 'audio codecs'
    assert [s['channels'] for s in audio] == [2, 2], 'all tracks must be stereo'
    langs = sorted((s.get('tags') or {}).get('language', 'und') for s in audio)
    assert langs == ['eng', 'tam'], 'languages preserved: %s' % langs
    assert Path('output.mp4').stat().st_size < Path('source.mkv').stat().st_size
    with open('output.mp4', 'rb') as f:
        header = f.read(12)
    assert b'ftyp' in header, 'MP4 ftyp box at start'
    print('SMOKE VERIFIED ' + json.dumps({'resolution': '960x540', 'video': 'HEVC hvc1',
        'audio': 'AAC stereo x2 languages', 'container': 'MP4 faststart',
        'source_bytes': Path('source.mkv').stat().st_size,
        'output_bytes': Path('output.mp4').stat().st_size}), flush=True)
