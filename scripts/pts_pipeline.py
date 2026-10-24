#!/usr/bin/env python3
"""PTS media pipeline: download -> compress -> upload to the VPS.

Runs on a GitHub Actions runner. Progress is streamed back to the PTS backend
so the Queue page can show one stage at a time (download, compress, upload)
with speed, percentage and ETA - never two overlapping processes.

Stages reported:
    downloading  pct, speed, eta, file_size
    compressing  pct, eta, message ("1.8x 45 fps")
    uploading    pct, speed, eta, out_size
The VPS owns the final stages (to_pi, done) once the upload lands.
"""
import http.client
import ipaddress
import json
import threading
import math
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CHUNK = 8 * 1024 * 1024
CHUNK_RETRIES = 6
UPLOAD_WORKERS = 8
USER_AGENT = "pts-runner"
OUTPUT_HEIGHT = 540
RUNNER_SECONDS = 320 * 60
UPLOAD_RESERVE_SECONDS = 60 * 60
MIN_FREE_BYTES = 1 << 30
BUDGET = None
ABORT_REASON = ""


class RunnerLimitError(RuntimeError):
    pass


class RunnerBudget:
    def __init__(self, seconds=RUNNER_SECONDS):
        self.deadline = time.monotonic() + seconds

    def remaining(self, reserve=0):
        return max(0, self.deadline - time.monotonic() - reserve)

    def check(self):
        if self.remaining() <= 0:
            raise RunnerLimitError("Runner safety deadline reached before GitHub's six-hour limit")
        if shutil.disk_usage(".").free < MIN_FREE_BYTES:
            raise RunnerLimitError("Runner disk safety reserve reached; stopping before disk exhaustion")


def check_resources():
    if BUDGET is not None:
        BUDGET.check()


def encode_seconds_left():
    return BUDGET.remaining(UPLOAD_RESERVE_SECONDS + 300) if BUDGET is not None else 4 * 3600


def monitor_resources(stop):
    global ABORT_REASON
    while not stop.wait(5):
        try:
            check_resources()
        except RunnerLimitError as exc:
            ABORT_REASON = str(exc)
            os.kill(os.getpid(), signal.SIGTERM)
            return


def interrupted(signum, frame):
    raise RunnerLimitError(ABORT_REASON or "Runner interrupted; no partial movie will be published")


def stop_children():
    try:
        children = Path("/proc/%d/task/%d/children" % (os.getpid(), os.getpid())).read_text().split()
        for pid in children:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
    except OSError:
        pass


def _bail(message):
    print("FAILED: " + message, file=sys.stderr)
    report(stage="failed", error=message[:500], finish=True)
    sys.exit(1)


def report(**fields):
    """Best-effort progress callback to the worker API."""
    payload = {"job_id": JOB_ID}
    if ATTEMPT:
        payload["attempt"] = ATTEMPT
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    data = json.dumps(payload).encode()
    req = urllib.request.Request(CALLBACK + "/worker/progress", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 409):
            # The job was retried, edited or removed while this runner was
            # working: stop instead of burning the rest of the run.
            print("job %s is gone or superseded, stopping" % JOB_ID, file=sys.stderr)
            sys.exit(0)
        print("progress post failed: %s" % exc, file=sys.stderr)
    except Exception as exc:  # progress must never kill the run
        print("progress post failed: %s" % exc, file=sys.stderr)


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024


def parse_hms(text):
    parts = text.split(":")
    if len(parts) != 3:
        raise ValueError(text)
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


# ---------------------------------------------------------------------------
# stage 1: download
# ---------------------------------------------------------------------------
DOWNLOAD_RETRIES = 3


def download(url, dest):
    """Fetch the source, retrying a few times before giving up."""
    last_err = None
    deadline = time.monotonic() + 40 * 60
    max_bytes = max(0, (shutil.disk_usage(".").free - (2 << 30)) * 2 // 3)
    if max_bytes < 64 << 20:
        raise RunnerLimitError("Not enough runner disk space for source, output and safety reserve")
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            return download_once(url, dest, deadline, max_bytes)
        except RunnerLimitError:
            raise
        except Exception as exc:
            last_err = exc
            print("download attempt %d failed: %s" % (attempt, exc), file=sys.stderr)
            if attempt < DOWNLOAD_RETRIES:
                report(stage="downloading", progress=0, message="Retrying download")
                time.sleep(5 * attempt)
    _bail("download failed: %s" % last_err)


def download_once(url, dest, deadline=None, max_bytes=None):
    print("downloading source")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
    })
    report(stage="downloading", progress=0, message="Connecting", runner=RUN_ID)

    started = time.time()
    last = 0.0
    done = 0
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        if max_bytes is not None and total > max_bytes:
            raise RunnerLimitError("Source is too large for the runner's safe working-disk budget")
        while True:
            check_resources()
            if deadline is not None and time.monotonic() >= deadline:
                raise RunnerLimitError("Source download exceeded its 40-minute budget")
            chunk = resp.read(262144)
            if not chunk:
                break
            if max_bytes is not None and done + len(chunk) > max_bytes:
                raise RunnerLimitError("Source exceeds the runner's safe working-disk budget")
            out.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last < 3:
                continue
            last = now
            speed = int(done / max(0.001, now - started))
            eta = int((total - done) / speed) if (total and speed) else None
            if total:
                pct = done / total * 100
            else:
                # No Content-Length: creep towards 95% on elapsed time so the
                # queue still shows a real 0-100 bar instead of a stuck one.
                pct = min(95.0, 100.0 * (1.0 - math.exp(-(now - started) / 120.0)))
            msg = human(done) + (" / " + human(total) if total else "")
            report(stage="downloading", progress=pct, speed_bps=speed,
                   eta_seconds=eta, file_size=done, message=msg)

    size = os.path.getsize(dest)
    print("downloaded %s bytes" % size)
    if size == 0:
        raise RuntimeError("the download produced an empty file")
    report(stage="downloading", progress=100, file_size=size,
           speed_bps=0, eta_seconds=0, message=human(size))
    return size


# ---------------------------------------------------------------------------
# stage 2: sample-tested HEVC, retaining every audio/subtitle track
# ---------------------------------------------------------------------------
def probe_media(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=60)
    return json.loads(out.stdout)


def bounded_dimensions(width, height, limit):
    factor = min(1.0, (limit * 16 / 9) / width, limit / height)
    return max(2, int(width * factor) // 2 * 2), max(2, int(height * factor) // 2 * 2)


AUDIO_BITRATE = 128000


def select_audio(streams):
    """One useful track per language, highest quality first.

    For each language keep the highest-bitrate track; when bitrates tie,
    keep the one with more channels. High-bitrate tracks (for example a
    640k surround mix) are downmixed and transcoded to AAC 128k stereo
    during encoding, so every language ends up as one useful stereo track
    at the target bitrate. Works for arbitrary languages.
    """
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(audio) <= 1:
        return audio

    def channels(stream):
        return int(stream.get("channels") or 2)

    def bitrate(stream):
        try:
            return int(stream.get("bit_rate") or 0)
        except (ValueError, TypeError):
            return 0

    best = {}
    for stream in audio:
        lang = str((stream.get("tags") or {}).get("language") or "und").strip().lower()
        aliases = {"en":"eng", "ta":"tam", "hi":"hin", "te":"tel", "ml":"mal", "kn":"kan"}
        lang = aliases.get(lang, lang)
        if lang in ("", "und", "unknown", "mul"):
            lang = ("unknown", stream.get("index", id(stream)))
        rank = (bitrate(stream), channels(stream))
        current = best.get(lang)
        if current is None or rank > current[0]:
            best[lang] = (rank, stream)
    selected = [entry[1] for entry in best.values()]
    selected.sort(key=lambda s: s.get("index", 0))
    return selected


def audio_options(streams):
    """Per-stream audio options.

    Compatible stereo AAC at or below the target is copied untouched
    (never up-encode to a higher bitrate). Everything else is transcoded
    to AAC 128k stereo 48 kHz; surround-only languages are downmixed.
    """
    args = []
    for i, stream in enumerate(streams):
        try:
            bitrate = int(stream.get("bit_rate") or 0)
        except (ValueError, TypeError):
            bitrate = 0
        channels = int(stream.get("channels") or 2)
        sample_rate = int(stream.get("sample_rate") or 0)
        if (stream.get("codec_name") == "aac" and channels <= 2 and
                (0 < bitrate <= AUDIO_BITRATE * 1.05) and
                (not sample_rate or sample_rate in (44100, 48000))):
            args += ["-c:a:%d" % i, "copy"]
        else:
            args += ["-c:a:%d" % i, "aac", "-b:a:%d" % i, str(AUDIO_BITRATE),
                     "-ac:a:%d" % i, "2", "-ar:a:%d" % i, "48000"]
    return args


def audio_metadata(streams):
    """Preserve language and title metadata on every selected audio track."""
    args = []
    for i, stream in enumerate(streams):
        tags = stream.get("tags") or {}
        lang = tags.get("language")
        if lang:
            args += ["-metadata:s:a:%d" % i, "language=%s" % lang]
        title = tags.get("title")
        if title:
            args += ["-metadata:s:a:%d" % i, "title=%s" % title]
    return args


def measure_audio_bitrates(src, doc):
    unknown = {s["index"]: s for s in doc["streams"]
               if s.get("codec_type") == "audio" and not s.get("bit_rate")}
    if not unknown:
        return
    duration = float(doc.get("format", {}).get("duration") or 0)
    try:
        packets = subprocess.run(["ffprobe", "-v", "error", "-read_intervals", "%s%%+8" % (duration * 0.3),
                                  "-select_streams", "a", "-show_packets", "-show_entries",
                                  "packet=stream_index,size,duration_time", "-of", "json", str(src)],
                                 capture_output=True, text=True, check=True, timeout=30)
        totals = {}
        for packet in json.loads(packets.stdout).get("packets", []):
            index = packet["stream_index"]
            size, seconds = totals.get(index, (0, 0.0))
            totals[index] = (size + int(packet["size"]), seconds + float(packet.get("duration_time") or 0))
        for index, stream in unknown.items():
            size, seconds = totals.get(index, (0, 0.0))
            if seconds > 1:
                stream["bit_rate"] = str(int(size * 8 / seconds))
    except (subprocess.SubprocessError, ValueError, KeyError):
        print("Audio bitrate measurement unavailable; avoiding blind AAC re-encoding", flush=True)


def video_options(width, height, crf, preset="slow", pix_fmt="yuv420p"):
    return ["-c:v", "libx265", "-preset", preset, "-crf", str(crf),
            "-vf", "scale=%d:%d:flags=lanczos" % (width, height), "-pix_fmt", pix_fmt,
            "-tag:v", "hvc1",
            "-force_key_frames", "expr:gte(t,n_forced*2)",
            "-x265-params", "log-level=error"]


def choose_encode(src, doc):
    """Fixed storage profile: HEVC CRF 25, slow preset, max 540p, no upscaling."""
    video = next(s for s in doc["streams"] if s["codec_type"] == "video")
    width, height = int(video["width"]), int(video["height"])
    ref_w, ref_h = bounded_dimensions(width, height, OUTPUT_HEIGHT)
    report(stage="compressing", progress=0, message="Encoding %dx%d HEVC CRF 25" % (ref_w, ref_h))
    print("Fixed profile %dx%d HEVC CRF 25 slow; no upscaling" % (ref_w, ref_h), flush=True)
    return video_options(ref_w, ref_h, 25), {"mode": "fixed profile", "width": ref_w,
                                             "height": ref_h, "crf": 25, "preset": "slow"}


def compress(src, dst):
    started = time.time()
    doc = probe_media(src)
    duration = float(doc.get("format", {}).get("duration") or 0)
    measure_audio_bitrates(src, doc)
    audio = select_audio(doc["streams"])
    if len(audio) < sum(1 for s in doc["streams"] if s.get("codec_type") == "audio"):
        kept = ["%s %sch" % ((s.get("tags") or {}).get("language") or "und", s.get("channels") or 2) for s in audio]
        print("Retaining one audio track per language: " + ", ".join(kept), flush=True)
    video_args, plan = choose_encode(src, doc)
    started = time.time()

    audio_maps = []
    for stream in audio:
        audio_maps += ["-map", "0:%d" % stream["index"]]
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", src, "-map", "0:v:0"] + audio_maps + [
        "-map_metadata", "0"]
    cmd += video_args + audio_options(audio) + audio_metadata(audio) + [
        "-max_muxing_queue_size", "4096",
        "-avoid_negative_ts", "make_zero", "-use_editlist", "0",
        "-movflags", "+faststart",
    ]
    report(stage="compressing", progress=0, message="Starting encoder")
    cmd += ["-progress", "pipe:1", "-nostats", dst]

    log = open("work/ffmpeg.log", "w+")
    check_resources()
    available = encode_seconds_left()
    if available < 30:
        raise RunnerLimitError("Not enough encoding time remains after reserving upload time")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log,
                             universal_newlines=True, bufsize=1)
    timed_out = threading.Event()
    def stop_encoder():
        timed_out.set()
        if proc.poll() is None:
            proc.kill()
    timer = threading.Timer(available, stop_encoder)
    timer.daemon = True
    timer.start()
    state = {}
    last = 0.0
    try:
        for line in proc.stdout:
            check_resources()
            line = line.strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            state[key] = value
            if key != "out_time":
                continue
            now = time.time()
            if now - last < 3:
                continue
            last = now
            try:
                done_s = parse_hms(value)
            except ValueError:
                continue
            pct = min(99.9, done_s / duration * 100) if duration else min(
                99.9, 100.0 * (1.0 - math.exp(-(time.time() - started) / 600.0)))
            try:
                factor = float(state.get("speed", "").rstrip("x"))
            except ValueError:
                factor = 0.0
            eta = int((duration - done_s) / factor) if (duration and factor > 0) else None
            bits = [state.get("speed"), state.get("fps") and state["fps"] + " fps"]
            report(stage="compressing", progress=pct, eta_seconds=eta,
                   message=" ".join(b for b in bits if b))
        proc.wait()
        log.flush()
        log.seek(0)
        tail = log.read()[-1500:]
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        log.close()
    if timed_out.is_set():
        raise RunnerLimitError("Encoding exceeded its safe time budget; upload reserve was protected")
    if proc.returncode != 0:
        _bail("ffmpeg failed (exit %s): %s" % (proc.returncode, tail.strip()))

    final = probe_media(dst)
    validate_progressive_mp4(dst, final)
    if sum(1 for s in final["streams"] if s["codec_type"] == "audio") != len(audio):
        _bail("output validation failed: missing audio tracks")
    final_duration = float(final.get("format", {}).get("duration") or 0)
    if duration > 0 and abs(final_duration - duration) > max(2, duration * 0.002):
        _bail("output validation failed: duration mismatch")
    size = os.path.getsize(dst)
    reduction = (1 - size / os.path.getsize(src)) * 100
    print("Compression report: " + json.dumps({"source_bytes": os.path.getsize(src), "output_bytes": size,
                                               "saved_percent": round(reduction, 1), "encoding": plan}), flush=True)
    report(stage="compressing", progress=100, eta_seconds=0,
           message="%s; %.1f%% saved" % (human(size), reduction), out_size=size)
    return size


def validate_progressive_mp4(file, doc=None):
    """Reject incomplete, fragmented, or non-browser-ready output before upload."""
    import struct
    file = Path(file)
    size = file.stat().st_size
    moov = None
    mdat = None
    with file.open("rb") as stream:
        offset = 0
        while offset < size:
            stream.seek(offset)
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError("Incomplete MP4 atom header")
            length, kind = struct.unpack(">I4s", header)
            minimum = 8
            if length == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    raise ValueError("Incomplete extended MP4 atom")
                length = struct.unpack(">Q", extended)[0]
                minimum = 16
            elif length == 0:
                length = size - offset
            if length < minimum or offset + length > size:
                raise ValueError("Invalid MP4 atom length")
            if kind == b"moof":
                raise ValueError("Fragmented MP4 is not the default delivery format")
            if kind == b"moov":
                moov = offset
            if kind == b"mdat" and mdat is None:
                mdat = offset
            offset += length
    if moov is None or mdat is None or moov >= mdat:
        raise ValueError("MP4 index must precede media data (faststart)")
    if doc is None:
        doc = probe_media(str(file))
    videos = [s for s in doc["streams"] if s["codec_type"] == "video"]
    audio = [s for s in doc["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or videos[0]["codec_name"] != "hevc" or videos[0].get("codec_tag_string") != "hvc1":
        raise ValueError("Output must contain one hvc1 HEVC video track")
    if any(s["codec_name"] != "aac" or int(s.get("channels", 0)) not in (1, 2) for s in audio):
        raise ValueError("Output audio must be mono/stereo AAC")
    return doc


# ---------------------------------------------------------------------------
# stage 3: upload the compressed file to the VPS in chunks
# ---------------------------------------------------------------------------
class ChunkError(Exception):
    def __init__(self, code, body):
        super().__init__("upload rejected (%s): %s" % (code, body))
        self.code = code


def upload_worker_count():
    try:
        return max(1, min(32, int(os.environ.get("UPLOAD_WORKERS", UPLOAD_WORKERS))))
    except ValueError:
        return UPLOAD_WORKERS


class VerifiedOriginConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, origin_ip, context):
        super().__init__(host, port, timeout=120, context=context)
        self.origin_ip = origin_ip

    def connect(self):
        raw = socket.create_connection((self.origin_ip, self.port), timeout=15)
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock.settimeout(self.timeout)
        except BaseException:
            raw.close()
            raise


class Uploader:
    """One warm HTTPS connection shared by every chunk of a file."""

    def __init__(self, base):
        parts = urllib.parse.urlsplit(base)
        self.secure = parts.scheme == "https"
        self.host = parts.hostname
        self.port = parts.port
        self.path = parts.path.rstrip("/") + "/worker/upload"
        self.conn = None
        self.direct_failures = 0
        self.origin_ip = os.environ.get("UPLOAD_ORIGIN_IP", "").strip()
        self.origin_context = None
        if self.origin_ip:
            ipaddress.ip_address(self.origin_ip)
            if not self.secure:
                raise ValueError("Direct uploads require HTTPS")
            ca = os.environ.get("UPLOAD_CA_PEM", "").strip()
            try:
                if not ca:
                    raise ValueError("Missing origin CA")
                self.origin_context = ssl.create_default_context(cadata=ca)
                self.origin_context.set_alpn_protocols(["http/1.1"])
            except (ValueError, ssl.SSLError):
                self.origin_ip = ""
                print("Direct upload CA unavailable; using protected HTTPS upload", file=sys.stderr)

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def post(self, query, chunk):
        if self.conn is None:
            if self.origin_ip:
                self.conn = VerifiedOriginConnection(self.host, self.port, self.origin_ip, self.origin_context)
            else:
                factory = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
                self.conn = factory(self.host, self.port, timeout=120)
        try:
            self.conn.request("POST", self.path + "?" + query, body=chunk, headers={
                "Content-Type": "application/octet-stream",
                "Authorization": "Bearer " + TOKEN,
                "User-Agent": USER_AGENT,
                "Connection": "keep-alive",
            })
            resp = self.conn.getresponse()
            body = resp.read()
            if resp.status >= 400:
                raise ChunkError(resp.status, body[:300])
        except ChunkError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            if self.origin_ip:
                self.direct_failures += 1
                if isinstance(exc, ssl.SSLCertVerificationError) or self.direct_failures >= 2:
                    self.origin_ip = ""
                    print("Direct upload unavailable; falling back to protected HTTPS upload", file=sys.stderr)
            raise


def upload(path, extension=".mp4"):
    """Send the finished file in parallel chunks over a few warm connections.

    A single TCP stream from a shared runner crawls (a couple hundred KB/s),
    while the same chunks over eight streams finish several times faster. Every
    part is addressed by sequence number, so the order they land in does not
    matter, and the final part is held back until all the others are in.
    """
    size = os.path.getsize(path)
    workers = upload_worker_count()
    started = time.time()
    report(stage="uploading", progress=0, out_size=size, message="Sending to VPS")

    local = threading.local()
    lock = threading.Lock()
    state = {"sent": 0, "seq": 0, "reported": 0.0}

    def conn():
        up = getattr(local, "up", None)
        if up is None:
            up = Uploader(CALLBACK)
            local.up = up
        return up

    def push(seq, chunk, is_last):
        query = "job=%s&seq=%d&last=%d&ext=%s&attempt=%s" % (JOB_ID, seq, is_last, extension, ATTEMPT)
        up = conn()
        delay = 2
        last_err = "upload failed"
        for attempt in range(1, CHUNK_RETRIES + 1):
            check_resources()
            try:
                up.post(query, chunk)
                last_err = None
                break
            except ChunkError as exc:
                if exc.code in (404, 409):
                    print("job superseded or removed, stopping", file=sys.stderr)
                    os._exit(0)
                last_err = str(exc)
                print("chunk %d attempt %d: %s" % (seq, attempt, last_err), file=sys.stderr)
            except Exception as exc:
                last_err = "upload failed: %s" % exc
                print("chunk %d attempt %d: %s" % (seq, attempt, last_err), file=sys.stderr)
            if attempt < CHUNK_RETRIES:
                time.sleep(delay)
                delay = min(30, delay * 2)
        if last_err:
            raise RuntimeError(last_err)

        with lock:
            state["sent"] += len(chunk)
            sent = state["sent"]
            now = time.time()
            speed = int(sent / max(0.001, now - started))
            if is_last:
                report(stage="uploading", progress=100, speed_bps=speed,
                       eta_seconds=0, out_size=size, message="Sent to VPS")
            elif now - state["reported"] >= 2:
                state["reported"] = now
                eta = int((size - sent) / speed) if speed else None
                report(stage="uploading", progress=sent / size * 100,
                       speed_bps=speed, eta_seconds=eta, out_size=size)

    seq = 0
    pending = []
    prev = None
    with ThreadPoolExecutor(max_workers=workers) as pool, open(path, "rb") as fh:
        while True:
            check_resources()
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            if prev is not None:
                seq += 1
                pending.append(pool.submit(push, seq, prev, False))
                while len(pending) >= workers * 2:
                    pending.pop(0).result()
            prev = chunk
        for job in pending:
            job.result()
        if prev is not None:
            seq += 1
            push(seq, prev, True)
    print("uploaded %s bytes in %d chunk(s)" % (size, seq))


# ---------------------------------------------------------------------------
def package_hls(source, destination):
    import hashlib
    import re
    import tarfile
    source = Path(source)
    destination = Path(destination)
    destination.mkdir()
    doc = probe_media(str(source))
    video = next(s for s in doc["streams"] if s["codec_type"] == "video")
    audio = [s for s in doc["streams"] if s["codec_type"] == "audio"]
    if len(audio) > 32:
        raise ValueError("Too many audio tracks for safe HLS packaging")
    for i in range(1 + len(audio)):
        (destination / ("v%d" % i)).mkdir()
    variants = ["v:0,agroup:audio" if audio else "v:0"]
    for i, stream in enumerate(audio):
        lang = re.sub(r"[^a-zA-Z]", "", (stream.get("tags") or {}).get("language") or "und")[:8] or "und"
        variants.append("a:%d,agroup:audio,language:%s,default:%s" % (i, lang, "yes" if i == 0 else "no"))
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0:v:0"]
    if audio:
        command += ["-map", "0:a"]
    command += ["-c", "copy", "-tag:v", "hvc1", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                "-hls_segment_type", "fmp4", "-hls_flags", "independent_segments", "-hls_fmp4_init_filename", "init_%v.mp4",
                "-hls_segment_filename", str(destination / "v%v/seg_%05d.m4s"), "-var_stream_map", " ".join(variants),
                "-master_pl_name", "master.m3u8", str(destination / "v%v/index.m3u8")]
    report(stage="compressing", progress=99, message="Packaging single-quality HLS with 128 kbps audio")
    remaining = BUDGET.remaining(UPLOAD_RESERVE_SECONDS) if BUDGET is not None else 1800
    if remaining < 30:
        raise RunnerLimitError("Not enough packaging time remains before the upload reserve")
    subprocess.run(command, check=True, timeout=min(1800, int(remaining)))
    init = (destination / "v0/init_0.mp4").read_bytes()
    offset = init.index(b"hvcC") + 4
    config = init[offset:offset + 13]
    compatibility = int(format(int.from_bytes(config[2:6], "big"), "032b")[::-1], 2)
    constraints = list(config[6:12])
    while constraints and constraints[-1] == 0:
        constraints.pop()
    codec = "hvc1." + ("", "A", "B", "C")[config[1] >> 6] + str(config[1] & 31) + "." + format(compatibility, "X") + "." + ("H" if config[1] & 32 else "L") + str(config[12]) + "".join("." + format(value, "X") for value in constraints)
    master = destination / "master.m3u8"
    master.write_text(re.sub(r'hvc1[^,\"]+', codec, master.read_text(), count=1))
    files = {}
    for file in sorted(destination.rglob("*")):
        if not file.is_file():
            continue
        check_resources()
        digest = hashlib.sha256()
        with file.open("rb") as incoming:
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                digest.update(block)
        files[str(file.relative_to(destination))] = {"size": file.stat().st_size, "sha256": digest.hexdigest()}
    metadata = {"version": 1, "duration": float(doc["format"]["duration"]), "video_codec": "hevc",
                "video_mime": 'video/mp4; codecs="' + codec + '"', "width": video["width"], "height": video["height"],
                "size": sum(file["size"] for file in files.values()), "audio_bitrate_target": AUDIO_BITRATE,
                "audio": [{"i": i + 1, "lang": (stream.get("tags") or {}).get("language") or "und", "codec": "aac",
                           "ch": stream.get("channels", 2), "title": (stream.get("tags") or {}).get("title", "")} for i, stream in enumerate(audio)],
                "files": files}
    (destination / "pts-hls.json").write_text(json.dumps(metadata, separators=(",", ":")))
    summary = {key: metadata[key] for key in ("version", "size", "duration", "video_codec", "video_mime", "width", "height", "audio")}
    (destination / "pts-info.json").write_text(json.dumps(summary, separators=(",", ":")))
    archive = destination.with_suffix(".hls.tar")
    with tarfile.open(archive, "w") as output:
        for file in sorted(destination.rglob("*")):
            output.add(file, arcname=str(file.relative_to(destination)), recursive=False)
    print("HLS package: one HEVC video rendition, %d AAC languages, %d bytes" % (len(audio), metadata["size"]), flush=True)
    return archive


def main():
    if "--report-failure" in sys.argv:
        report(stage="failed", error="the runner job failed", finish=True)
        return
    print("job %s: %s" % (JOB_ID, LABEL))
    Path("work").mkdir(exist_ok=True)

    src = "work/source"
    download(SOURCE_URL, src)

    dst = "work/out.mp4"
    compress(src, dst)

    try:
        os.remove(src)  # reclaim runner disk before uploading
    except OSError:
        pass

    # The validated, faststart MP4 is the final artifact. Keep it intact if the
    # upload fails; VPS retries and legacy HLS transfers remain supported.
    upload(dst, ".mp4")
    print("done - the VPS will move the file to the Pi")


def fetch_source_url():
    query = urllib.parse.urlencode({"job": JOB_ID, "attempt": ATTEMPT})
    req = urllib.request.Request(CALLBACK + "/worker/source?" + query,
                                 headers={"Authorization": "Bearer " + TOKEN, "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read(65536))
    url = data.get("url", "")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("The source link is invalid")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::add-mask::" + url.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"), flush=True)
    return url


if __name__ == "__main__":
    JOB_ID = os.environ.get("JOB_ID", "").strip()
    CALLBACK = os.environ.get("CALLBACK", "").rstrip("/")
    TOKEN = os.environ.get("WORKER_TOKEN", "")
    if not (JOB_ID and CALLBACK and TOKEN):
        print("JOB_ID, CALLBACK and WORKER_TOKEN are required", file=sys.stderr)
        sys.exit(2)
    SOURCE_URL = ""
    LABEL = os.environ.get("LABEL") or os.environ.get("MEDIA_TITLE") or "job"
    RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
    ATTEMPT = os.environ.get("ATTEMPT", "").strip()
    stop = threading.Event()
    if "--report-failure" not in sys.argv:
        BUDGET = RunnerBudget()
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        threading.Thread(target=monitor_resources, args=(stop,), daemon=True).start()
    try:
        if "--report-failure" not in sys.argv:
            SOURCE_URL = fetch_source_url()
        main()
    except Exception as exc:
        stop_children()
        _bail(str(exc))
    finally:
        stop.set()
