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
import json
import threading
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CHUNK = 16 * 1024 * 1024  # bigger parts mean fewer requests and a warm TCP window
CHUNK_RETRIES = 6
UPLOAD_WORKERS = 4  # parallel connections: one TCP stream from a runner is slow
USER_AGENT = "pts-runner"


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
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            return download_once(url, dest)
        except Exception as exc:
            last_err = exc
            print("download attempt %d failed: %s" % (attempt, exc), file=sys.stderr)
            if attempt < DOWNLOAD_RETRIES:
                report(stage="downloading", progress=0, message="Retrying download")
                time.sleep(5 * attempt)
    _bail("download failed: %s" % last_err)


def download_once(url, dest):
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
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
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


def audio_options(streams):
    args = []
    for i, stream in enumerate(s for s in streams if s.get("codec_type") == "audio"):
        channels = int(stream.get("channels") or 2)
        target = 64000 if channels == 1 else 96000 if channels == 2 else 192000 if channels <= 6 else 256000
        try:
            bitrate = int(stream.get("bit_rate") or 0)
        except (ValueError, TypeError):
            bitrate = 0
        if stream.get("codec_name") == "aac" and (not bitrate or bitrate <= target * 1.05):
            args += ["-c:a:%d" % i, "copy"]
        else:
            if bitrate:
                target = min(target, bitrate)
            args += ["-c:a:%d" % i, "aac", "-b:a:%d" % i, str(target)]
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


def video_options(width, height, crf, preset="slow", pix_fmt="yuv420p", fps=24):
    keyint = max(12, int(round(fps * 4)))
    return ["-c:v", "libx265", "-preset", preset, "-crf", str(crf),
            "-vf", "scale=%d:%d:flags=lanczos" % (width, height), "-pix_fmt", pix_fmt,
            "-x265-params", "log-level=error:keyint=%d:min-keyint=1" % keyint]


def quality_score(encoded, reference, width, height, vmaf):
    if vmaf:
        graph = ("[0:v]scale=%d:%d:flags=lanczos,setpts=PTS-STARTPTS[d];"
                 "[1:v]setpts=PTS-STARTPTS[r];[d][r]libvmaf=n_threads=2" % (width, height))
    else:
        graph = ("[0:v]scale=%d:%d:flags=lanczos,setpts=PTS-STARTPTS,split[d1][d2];"
                 "[1:v]setpts=PTS-STARTPTS,split[r1][r2];"
                 "[d1][r1]ssim[s];[d2][r2]psnr[p]" % (width, height))
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-threads", "2", "-i", str(encoded),
           "-threads", "2", "-i", str(reference), "-filter_complex_threads", "2", "-filter_complex", graph]
    if not vmaf:
        cmd += ["-map", "[s]", "-map", "[p]"]
    result = subprocess.run(cmd + ["-an", "-f", "null", "-"], capture_output=True, text=True,
                            check=True, timeout=120)
    if vmaf:
        scores = re.findall(r"VMAF score: ([0-9.]+)", result.stderr)
        if not scores:
            raise RuntimeError("VMAF did not produce a score")
        return float(scores[-1])
    ssim = re.findall(r"All:([0-9.]+)", result.stderr)
    psnr = re.findall(r"average:([0-9.]+|inf)", result.stderr)
    if not ssim or not psnr:
        raise RuntimeError("Quality measurement did not produce a score")
    # A combined guard avoids accepting high SSIM with severe blocking.
    return min(float(ssim[-1]) * 100, 97 + (float(psnr[-1]) - 35) * 0.25)


def choose_encode(src, doc):
    video = next(s for s in doc["streams"] if s["codec_type"] == "video")
    width, height = int(video["width"]), int(video["height"])
    ref_w, ref_h = bounded_dimensions(width, height, 720)
    small_w, small_h = bounded_dimensions(width, height, 540)
    pix_fmt = "yuv420p10le" if "10" in video.get("pix_fmt", "") else "yuv420p"
    try:
        n, d = video.get("avg_frame_rate", "24/1").split("/")
        fps = float(n) / float(d)
        if not 1 <= fps <= 120:
            fps = 24
    except (ValueError, ZeroDivisionError):
        fps = 24
    duration = float(doc.get("format", {}).get("duration") or 0)
    fallback = video_options(ref_w, ref_h, 25, pix_fmt=pix_fmt, fps=fps)
    if duration < 12:
        return fallback, {"mode": "quality fallback", "height": ref_h, "crf": 25}

    work = Path("work/quality")
    work.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 8 * 60
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=True)
    vmaf = "libvmaf" in filters.stdout
    metric = "VMAF" if vmaf else "SSIM/PSNR"
    report(stage="compressing", progress=0, message="Testing storage/quality (%s)" % metric)
    print("Per-title quality selection: %s; no resolution upscaling" % metric, flush=True)
    try:
        references = []
        for i, fraction in enumerate((0.18, 0.5, 0.78)):
            reference = work / ("ref%d.mkv" % i)
            start = min(duration - 6, duration * fraction)
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", str(start), "-i", str(src), "-t", "6", "-map", "0:v:0", "-an", "-sn",
                            "-vf", "scale=%d:%d:flags=lanczos" % (ref_w, ref_h), "-pix_fmt", pix_fmt,
                            "-c:v", "ffv1", "-threads", "2", "-map_metadata", "-1", "-map_chapters", "-1",
                            str(reference)], capture_output=True, check=True, timeout=90)
            references.append(reference)

        plans = [(ref_w, ref_h, q) for q in (25, 27, 29, 31)]
        if (small_w, small_h) != (ref_w, ref_h):
            plans += [(small_w, small_h, q) for q in (25, 27, 29)]
        accepted = []
        for w, h, crf in plans:
            if time.monotonic() >= deadline:
                print("Sample-testing budget reached; using the best completed result", flush=True)
                break
            scores, sizes = [], []
            for i, reference in enumerate(references):
                if time.monotonic() >= deadline:
                    break
                candidate = work / "candidate.mkv"
                args = video_options(w, h, crf, pix_fmt=pix_fmt, fps=fps)
                subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                                "-i", str(reference)] + args + ["-an", "-map_metadata", "-1", str(candidate)],
                               capture_output=True, check=True, timeout=180)
                scores.append(quality_score(candidate, reference, ref_w, ref_h, vmaf))
                sizes.append(candidate.stat().st_size)
                if scores[-1] < (88 if vmaf else 97):
                    break
            if not scores:
                break
            mean = sum(scores) / len(scores)
            passed = len(scores) == len(references) and min(scores) >= (88 if vmaf else 97) and mean >= (92 if vmaf else 97.5)
            print("Sample %dx%d CRF %d: %s %.2f, minimum %.2f, %s" %
                  (w, h, crf, metric, mean, min(scores), "pass" if passed else "reject"), flush=True)
            if passed:
                accepted.append((sum(sizes), w, h, crf, mean))
            report(stage="compressing", progress=0, message="Quality tested: %dp / CRF %d" % (h, crf))

        if not accepted:
            print("No storage candidate passed; preserving quality with conservative CRF 23", flush=True)
            return video_options(ref_w, ref_h, 23, pix_fmt=pix_fmt, fps=fps), {"mode": "quality fallback", "height": ref_h, "crf": 23}
        size, w, h, crf, score = min(accepted)
        details = {"mode": "sample tested", "metric": metric, "score": round(score, 2), "width": w, "height": h, "crf": crf}
        print("Selected smallest passing sample: " + json.dumps(details), flush=True)
        return video_options(w, h, crf, pix_fmt=pix_fmt, fps=fps), details
    except (subprocess.SubprocessError, ValueError, KeyError, RuntimeError) as exc:
        print("Quality selection unavailable (%s); using conservative fallback" % type(exc).__name__, flush=True)
        return fallback, {"mode": "quality fallback", "height": ref_h, "crf": 25}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def compress(src, dst):
    started = time.time()
    doc = probe_media(src)
    duration = float(doc.get("format", {}).get("duration") or 0)
    index_space = str(min(1 << 20, max(4096, int(duration * 16) + 4096)))
    measure_audio_bitrates(src, doc)
    video_args, plan = choose_encode(src, doc)
    started = time.time()

    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", src,
           "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
           "-map_metadata", "0"]
    cmd += video_args + audio_options(doc["streams"]) + [
        "-c:s", "copy",
        "-max_muxing_queue_size", "4096",
        "-reserve_index_space", index_space,
    ]
    report(stage="compressing", progress=0, message="Starting encoder")
    cmd += ["-progress", "pipe:1", "-nostats", dst]

    log = open("work/ffmpeg.log", "w+")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log,
                            universal_newlines=True, bufsize=1)

    state = {}
    last = 0.0
    for line in proc.stdout:
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
    log.close()
    if proc.returncode != 0:
        _bail("ffmpeg failed (exit %s): %s" % (proc.returncode, tail.strip()))

    # If encoding grew an already compact source, try a lossless video remux
    # with the same audio policy instead. Keep the smaller valid output.
    if os.path.getsize(dst) >= os.path.getsize(src):
        alternative = dst + ".copy.mkv"
        copied = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                                 "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
                                 "-map_metadata", "0", "-c:v", "copy"] + audio_options(doc["streams"]) +
                                ["-c:s", "copy", "-reserve_index_space", index_space, alternative],
                                capture_output=True, timeout=max(300, int(duration)))
        if copied.returncode == 0 and os.path.getsize(alternative) < os.path.getsize(dst):
            os.replace(alternative, dst)
            plan = {"mode": "video copied: avoiding needless quality loss"}
        if os.path.exists(alternative):
            os.remove(alternative)

    original_compatible = all(s.get("codec_name") in ("aac", "h264", "hevc")
                              for s in doc["streams"] if s.get("codec_type") in ("video", "audio"))
    if (os.path.getsize(dst) >= os.path.getsize(src) and original_compatible and
            "matroska" in doc.get("format", {}).get("format_name", "")):
        shutil.copyfile(src, dst)
        plan = {"mode": "original retained: already smaller and compatible"}

    final = probe_media(dst)
    for kind in ("audio", "subtitle"):
        if sum(s["codec_type"] == kind for s in final["streams"]) != sum(s["codec_type"] == kind for s in doc["streams"]):
            _bail("output validation failed: missing %s tracks" % kind)
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


# ---------------------------------------------------------------------------
# stage 3: upload the compressed file to the VPS in chunks
# ---------------------------------------------------------------------------
class ChunkError(Exception):
    def __init__(self, code, body):
        super().__init__("upload rejected (%s): %s" % (code, body))
        self.code = code


class Uploader:
    """One warm HTTPS connection shared by every chunk of a file."""

    def __init__(self, base):
        parts = urllib.parse.urlsplit(base)
        self.secure = parts.scheme == "https"
        self.host = parts.hostname
        self.port = parts.port
        self.path = parts.path.rstrip("/") + "/worker/upload"
        self.conn = None

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def post(self, query, chunk):
        if self.conn is None:
            factory = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
            self.conn = factory(self.host, self.port, timeout=900)
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
        except Exception:
            self.close()
            raise


def upload(path):
    """Send the finished file in parallel chunks over a few warm connections.

    A single TCP stream from a shared runner crawls (a couple hundred KB/s),
    while the same chunks over four streams finish several times faster. Every
    part is addressed by sequence number, so the order they land in does not
    matter, and the final part is held back until all the others are in.
    """
    size = os.path.getsize(path)
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
        query = "job=%s&seq=%d&last=%d&ext=.mkv&attempt=%s" % (JOB_ID, seq, is_last, ATTEMPT)
        up = conn()
        delay = 2
        last_err = "upload failed"
        for attempt in range(1, CHUNK_RETRIES + 1):
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
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool, open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            if prev is not None:
                seq += 1
                pending.append(pool.submit(push, seq, prev, False))
                while len(pending) >= UPLOAD_WORKERS * 2:
                    pending.pop(0).result()
            prev = chunk
        for job in pending:
            job.result()
        if prev is not None:
            seq += 1
            push(seq, prev, True)
    print("uploaded %s bytes in %d chunk(s)" % (size, seq))


# ---------------------------------------------------------------------------
def main():
    if "--report-failure" in sys.argv:
        report(stage="failed", error="the runner job failed", finish=True)
        return
    print("job %s: %s" % (JOB_ID, LABEL))
    Path("work").mkdir(exist_ok=True)

    src = "work/source"
    download(SOURCE_URL, src)

    dst = "work/out.mkv"
    compress(src, dst)

    try:
        os.remove(src)  # reclaim runner disk before uploading
    except OSError:
        pass

    upload(dst)
    print("done - the VPS will move the file to the Pi")


if __name__ == "__main__":
    JOB_ID = os.environ.get("JOB_ID", "").strip()
    CALLBACK = os.environ.get("CALLBACK", "").rstrip("/")
    TOKEN = os.environ.get("WORKER_TOKEN", "")
    if not (JOB_ID and CALLBACK and TOKEN):
        print("JOB_ID, CALLBACK and WORKER_TOKEN are required", file=sys.stderr)
        sys.exit(2)
    SOURCE_URL = os.environ.get("SOURCE_URL", "")
    LABEL = os.environ.get("LABEL") or os.environ.get("MEDIA_TITLE") or "job"
    RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
    ATTEMPT = os.environ.get("ATTEMPT", "").strip()
    if "--report-failure" not in sys.argv and not SOURCE_URL:
        print("SOURCE_URL is required", file=sys.stderr)
        sys.exit(2)
    main()
