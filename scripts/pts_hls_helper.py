import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

LIBRARY_ROOTS = ('/DATA/Movie', '/DATA/Series')
DOWNLOAD_ROOT = Path('/DATA/PTSDownloads')
ASSET = re.compile(r'^(master\.m3u8|v\d{1,2}/(index\.m3u8|init_\d{1,2}\.mp4|seg_\d{5,8}\.m4s))$')
IDENTIFIER = re.compile(r'^[a-f0-9]{32}$')
MAX_BYTES = 64 * 1024 ** 3
MAX_FILES = 200000


def summary(doc):
    return {key: doc[key] for key in ('version', 'size', 'duration', 'video_codec', 'video_mime', 'width', 'height', 'audio') if key in doc}


def digest(file):
    h = hashlib.sha256()
    with open(file, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def metadata(root):
    file = Path(root) / 'pts-hls.json'
    if file.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('HLS metadata is too large')
    doc = json.loads(file.read_text())
    return validate_metadata(doc)


def validate_metadata(doc):
    files = doc.get('files', {})
    if doc.get('version') != 1 or not 0 < len(files) <= MAX_FILES:
        raise ValueError('Invalid HLS package metadata')
    if doc.get('video_codec') != 'hevc' or not 0 < doc.get('duration', 0) < 86400:
        raise ValueError('Invalid HLS video metadata')
    if not files.get('master.m3u8') or any(not ASSET.fullmatch(name) for name in files):
        raise ValueError('Unsafe HLS asset name')
    if not 0 < doc.get('size', 0) <= MAX_BYTES or len(doc.get('audio', [])) > 32:
        raise ValueError('HLS package exceeds safe limits')
    return doc


def inspect_archive(archive):
    doc = None
    info = None
    actual = {}
    total = 0
    playlists = {}
    with tarfile.open(archive, 'r|') as source:
        count = 0
        for member in source:
            count += 1
            name = member.name.rstrip('/')
            if count > MAX_FILES + 100 or not (ASSET.fullmatch(name) or name in ('pts-hls.json', 'pts-info.json') or (member.isdir() and re.fullmatch(r'v\d{1,2}', name))):
                raise ValueError('Unsafe HLS archive entry')
            if member.isdir():
                continue
            if not member.isfile() or name in actual or member.size < 0:
                raise ValueError('Unsupported or repeated HLS archive entry')
            total += member.size
            if total > MAX_BYTES:
                raise ValueError('HLS archive exceeds safe size')
            with source.extractfile(member) as incoming:
                if name == 'pts-info.json':
                    if info is not None or member.size > 65536:
                        raise ValueError('Invalid HLS summary')
                    info = json.loads(incoming.read())
                elif name == 'pts-hls.json':
                    if doc is not None or member.size > 32 * 1024 * 1024:
                        raise ValueError('Invalid HLS metadata')
                    doc = validate_metadata(json.loads(incoming.read()))
                else:
                    h = hashlib.sha256()
                    text = bytearray()
                    for block in iter(lambda: incoming.read(1024 * 1024), b''):
                        h.update(block)
                        if name.endswith('.m3u8'):
                            text.extend(block)
                            if len(text) > 4 * 1024 * 1024:
                                raise ValueError('HLS playlist exceeds safe size')
                    actual[name] = {'size': member.size, 'sha256': h.hexdigest()}
                    if text:
                        playlists[name] = text.decode()
    if doc is None or actual != doc['files'] or sum(f['size'] for f in actual.values()) != doc['size']:
        raise ValueError('HLS archive integrity check failed')
    if info is not None and info != summary(doc):
        raise ValueError('HLS summary differs from verified metadata')
    for name, text in playlists.items():
        for line in text.splitlines():
            references = re.findall(r'URI="([^"]+)"', line) if line.startswith('#') else ([line] if line else [])
            for reference in references:
                if reference.startswith('/') or '\\' in reference or '..' in reference.split('/') or ':' in reference or '?' in reference or str(Path(name).parent / reference) not in actual:
                    raise ValueError('Unsafe HLS playlist reference')
    return doc


def verify(root):
    root = Path(root)
    doc = metadata(root)
    total = 0
    actual = set()
    for file in root.rglob('*'):
        if file.is_symlink() or not (file.is_dir() or file.is_file()):
            raise ValueError('HLS package contains a special file')
        if not file.is_file():
            continue
        name = str(file.relative_to(root))
        if name in ('pts-hls.json', 'pts-info.json'):
            continue
        actual.add(name)
        expected = doc['files'].get(name)
        if not expected or file.stat().st_size != expected['size'] or digest(file) != expected['sha256']:
            raise ValueError('HLS asset verification failed: ' + name)
        total += file.stat().st_size
    if actual != set(doc['files']) or total != doc['size']:
        raise ValueError('HLS package inventory differs')
    if (root / 'pts-info.json').exists() and json.loads((root / 'pts-info.json').read_text()) != summary(doc):
        raise ValueError('HLS summary differs from verified metadata')
    for name in actual:
        if not name.endswith('.m3u8'):
            continue
        for line in (root / name).read_text().splitlines():
            references = re.findall(r'URI="([^"]+)"', line) if line.startswith('#') else ([line] if line else [])
            for reference in references:
                if reference.startswith('/') or '\\' in reference or '..' in reference.split('/') or ':' in reference or '?' in reference:
                    raise ValueError('Unsafe HLS playlist reference')
                target = str((Path(name).parent / reference))
                if target not in actual:
                    raise ValueError('Missing HLS playlist reference')
    return doc


def extract(archive, destination):
    target = Path(destination)
    if target.exists():
        return verify(target)
    with tarfile.open(archive, 'r:') as source:
        members = source.getmembers()
        total = sum(member.size for member in members)
        if len(members) > MAX_FILES + 100 or total > MAX_BYTES:
            raise ValueError('HLS archive exceeds safe limits')
        if shutil.disk_usage(target.parent).free < total + 256 * 1024 ** 2:
            raise ValueError('Not enough space to validate HLS package')
        for member in members:
            name = member.name.rstrip('/')
            valid = ASSET.fullmatch(name) or name in ('pts-hls.json', 'pts-info.json') or (member.isdir() and re.fullmatch(r'v\d{1,2}', name))
            if not valid or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe HLS archive entry')
        temporary = Path(tempfile.mkdtemp(prefix='.pts-hls-', dir=target.parent))
        try:
            for member in members:
                file = temporary / member.name
                if member.isdir():
                    file.mkdir(mode=0o755, exist_ok=True)
                else:
                    file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    with source.extractfile(member) as incoming, file.open('xb') as output:
                        shutil.copyfileobj(incoming, output, 1024 * 1024)
                    file.chmod(0o644)
            doc = verify(temporary)
            temporary.chmod(0o755)
            rename_new(temporary, target)
            return doc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def library_source(file):
    original = Path(file)
    real = original.resolve(strict=True)
    if original.is_symlink() or not any(str(real).startswith(root + '/') for root in LIBRARY_ROOTS):
        raise ValueError('Source is outside the media library')
    return real


def rename_no_replace(source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def rename_new(source, target):
    try:
        rename_no_replace(source, target)
        return
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise
    source, target = Path(source), Path(target)
    if not source.is_dir() or source.is_symlink():
        raise ValueError('Directory publication requires a real directory')
    target.mkdir(mode=source.stat().st_mode & 0o777)
    moved = []
    try:
        children = sorted(source.iterdir(), key=lambda file: (file.name == 'pts-hls.json', file.name))
        for file in children:
            os.rename(file, target / file.name)
            moved.append(file.name)
        source.rmdir()
    except BaseException:
        for name in reversed(moved):
            os.rename(target / name, source / name)
        target.rmdir()
        raise


def scan(roots):
    items = []
    for base in roots:
        if not Path(base).is_dir():
            raise ValueError('Media storage is not mounted')
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(name for name in dirs if not name.startswith('.') and not (Path(directory) / name).is_symlink())
            if directory.endswith('.hls'):
                dirs[:] = []
                try:
                    info = Path(directory) / 'pts-info.json'
                    if info.exists():
                        if info.stat().st_size > 65536:
                            raise ValueError('HLS summary is too large')
                        doc = json.loads(info.read_text())
                        if doc.get('version') != 1 or doc.get('video_codec') != 'hevc' or not 0 < doc.get('size', 0) <= MAX_BYTES or not 0 < doc.get('duration', 0) < 86400:
                            raise ValueError('Invalid HLS summary')
                    else:
                        doc = summary(metadata(directory))
                    master = Path(directory) / 'master.m3u8'
                    stat = (Path(directory) / 'pts-hls.json').stat()
                    items.append({'path': str(master), 'size': doc['size'], 'mtime': int(stat.st_mtime), 'hls': doc})
                except (OSError, ValueError, KeyError):
                    pass
                continue
            for name in sorted(files):
                file = Path(directory) / name
                if file.suffix.lower() in ('.mp4', '.mkv', '.m4v', '.avi', '.webm') and not file.is_symlink():
                    stat = file.stat()
                    items.append({'path': str(file), 'size': stat.st_size, 'mtime': int(stat.st_mtime)})
    return {'items': items}


def download_dir(identifier):
    if not IDENTIFIER.fullmatch(identifier):
        raise ValueError('Invalid download identifier')
    DOWNLOAD_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    return DOWNLOAD_ROOT / identifier


def save_state(directory, state):
    temporary = directory / 'state.new'
    temporary.write_text(json.dumps(state))
    temporary.chmod(0o600)
    os.replace(temporary, directory / 'state.json')


def emit(state):
    print(json.dumps(state), flush=True)


def prepare_download(req):
    source = library_source(req['source'])
    if source.name != 'master.m3u8' or not source.parent.name.endswith('.hls'):
        raise ValueError('Only prepared HLS sources can be remuxed')
    doc = metadata(source.parent)
    directory = download_dir(req['id'])
    expires = min(int(req['expires']), int(time.time()) + 4 * 3600 - 60)
    if expires <= time.time():
        raise ValueError('Download request expired')
    directory.mkdir(mode=0o700)
    state = {'id': req['id'], 'created': int(time.time()), 'expires': expires, 'state': 'preparing', 'progress': 0, 'eta_seconds': None}
    save_state(directory, state)
    output = directory / 'movie.partial.mp4'
    proc = None
    try:
        signal.alarm(max(1, min(45 * 60, expires - int(time.time()))))
        if shutil.disk_usage(directory).free < doc['size'] * 1.15 + 256 * 1024 ** 2:
            raise ValueError('Not enough Pi space for a temporary MP4')
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,crypto,data', '-show_streams', '-of', 'json', str(source)], timeout=45))
        video = [s for s in probe['streams'] if s['codec_type'] == 'video']
        audio = [s for s in probe['streams'] if s['codec_type'] == 'audio']
        if len(video) != 1 or len(audio) != len(doc['audio']):
            raise ValueError('HLS source is incomplete')
        command = ['nice', '-n', '10', 'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-protocol_whitelist', 'file,crypto,data', '-i', str(source), '-map', '0:' + str(video[0]['index'])]
        for track in audio:
            command += ['-map', '0:' + str(track['index'])]
        command += ['-c', 'copy', '-tag:v', 'hvc1', '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', '-n', str(output)]
        started = time.monotonic()
        with (directory / 'prepare.log').open('wb') as log:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, text=True)
            state['pid'] = proc.pid
            save_state(directory, state)
            last = 0
            for line in proc.stdout:
                if time.time() >= expires or time.monotonic() - started > 45 * 60:
                    raise TimeoutError('MP4 preparation exceeded its time limit')
                if line.startswith('out_time_us='):
                    try:
                        seconds = max(0, int(line.split('=', 1)[1]) / 1000000)
                    except ValueError:
                        continue
                    now = time.monotonic()
                    if now - last < 1:
                        continue
                    last = now
                    fraction = min(1, seconds / doc['duration'])
                    elapsed = now - started
                    eta = max(1, int(elapsed * (1 - fraction) / fraction + max(2, elapsed * .08))) if fraction > .01 else None
                    state.update(progress=round(min(95, fraction * 95), 1), eta_seconds=eta, message='Preparing MP4' if fraction < .99 else 'Finalizing MP4')
                    save_state(directory, state)
                    emit(state)
            if proc.wait(timeout=30):
                raise ValueError('MP4 preparation failed')
        state.update(progress=97, eta_seconds=None, message='Checking MP4')
        save_state(directory, state)
        emit(state)
        final = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(output)], timeout=45))
        tracks = final['streams']
        if len([s for s in tracks if s['codec_type'] == 'audio']) != len(doc['audio']) or abs(float(final['format']['duration']) - doc['duration']) > 2:
            raise ValueError('Prepared MP4 did not pass validation')
        if next(s for s in tracks if s['codec_type'] == 'video')['codec_name'] != 'hevc':
            raise ValueError('Prepared video codec changed')
        with output.open('rb') as file:
            os.fsync(file.fileno())
        os.rename(output, directory / 'movie.mp4')
        state.pop('pid', None)
        state.update(state='ready', progress=100, eta_seconds=0, size=(directory / 'movie.mp4').stat().st_size, message='Ready to download')
        save_state(directory, state)
        emit(state)
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        signal.alarm(0)


def stop_download_process(directory, state):
    pid = state.get('pid')
    if not isinstance(pid, int) or pid <= 1:
        return
    try:
        command = Path('/proc/' + str(pid) + '/cmdline').read_bytes().split(b'\x00')
        if os.fsencode(str(directory / 'movie.partial.mp4')) in command and any(Path(os.fsdecode(arg)).name == 'ffmpeg' for arg in command if arg):
            os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass


def clean_downloads():
    if not DOWNLOAD_ROOT.exists():
        return {}
    removed = 0
    for directory in DOWNLOAD_ROOT.iterdir():
        if not directory.is_dir() or directory.is_symlink() or not IDENTIFIER.fullmatch(directory.name):
            continue
        state = {}
        try:
            state = json.loads((directory / 'state.json').read_text())
            deadline = min(state['expires'], state['created'] + 4 * 3600 - 60)
        except (OSError, ValueError, KeyError):
            deadline = directory.stat().st_mtime + 4 * 3600 - 60
        if time.time() >= deadline:
            stop_download_process(directory, state)
            shutil.rmtree(directory)
            removed += 1
    return {'removed': removed}


def run(req):
    global LIBRARY_ROOTS
    if 'roots' in req:
        roots = req['roots']
        if not isinstance(roots, list) or len(roots) != 2 or any(not isinstance(root, str) or not root.startswith('/DATA/') or '..' in root.split('/') for root in roots):
            raise ValueError('Invalid configured library roots')
        LIBRARY_ROOTS = tuple(root.rstrip('/') for root in roots)
    op = req['op']
    if op == 'extract':
        return extract(req['archive'], req['target'])
    if op == 'inspect_archive':
        return inspect_archive(req['archive'])
    if op == 'verify':
        return verify(req['root'])
    if op == 'scan':
        return scan(req.get('roots', LIBRARY_ROOTS))
    if op == 'publish':
        incoming = library_source(req['incoming'])
        target = Path(req['target'])
        if not target.name.endswith('.hls') or target.parent.resolve() != incoming.parent:
            raise ValueError('Invalid HLS destination')
        doc = verify(incoming)
        if target.exists():
            existing = verify(target)
            if existing['files'] != doc['files']:
                raise ValueError('A different title already exists at the destination')
            shutil.rmtree(incoming)
        else:
            rename_new(incoming, target)
        return doc
    if op == 'discard_incoming':
        candidate = Path(req['incoming'])
        if not candidate.exists():
            return {}
        incoming = library_source(candidate)
        if not re.fullmatch(r'\.pts-hls-[A-Za-z0-9_-]{1,100}\.incoming', incoming.name):
            raise ValueError('Invalid incoming package cleanup path')
        doc = verify(incoming)
        if doc['size'] != int(req['size']):
            raise ValueError('Incoming package differs; preserving it')
        shutil.rmtree(incoming)
        return {}
    if op == 'prepare':
        prepare_download(req)
        return None
    if op == 'download_status':
        return json.loads((download_dir(req['id']) / 'state.json').read_text())
    if op == 'download_expiry':
        directory = download_dir(req['id'])
        state = json.loads((directory / 'state.json').read_text())
        state['expires'] = min(state['expires'], int(req['expires']))
        save_state(directory, state)
        return state
    if op == 'download_remove':
        directory = download_dir(req['id'])
        try:
            state = json.loads((directory / 'state.json').read_text())
        except (OSError, ValueError):
            state = {}
        stop_download_process(directory, state)
        shutil.rmtree(directory, ignore_errors=True)
        return {}
    if op == 'cleanup':
        return clean_downloads()
    raise ValueError('Unknown HLS operation')


def interrupted(signum, frame):
    raise InterruptedError('Operation interrupted')


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    try:
        if '--cleanup' in sys.argv:
            emit(clean_downloads())
        else:
            result = run(json.loads(sys.stdin.readline()))
            if result is not None:
                emit(summary(result) if 'files' in result and 'version' in result else result)
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
