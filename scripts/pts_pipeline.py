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
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CHUNK = 64 * 1024 * 1024  # 64 MiB - safely under Cloudflare's 100 MB body cap
USER_AGENT = "pts-runner"


def _bail(message):
    print("FAILED: " + message, file=sys.stderr)
    report(stage="failed", error=message[:500], finish=True)
    sys.exit(1)


def report(**fields):
    """Best-effort progress callback to the worker API."""
    payload = {"job_id": JOB_ID}
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
def download(url, dest):
    print("downloading: %s" % url)
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
        _bail("the download produced an empty file")
    report(stage="downloading", progress=100, file_size=size,
           speed_bps=0, eta_seconds=0, message=human(size))
    return size


# ---------------------------------------------------------------------------
# stage 2: compress to 540p HEVC + 256k AAC (all audio tracks kept)
# ---------------------------------------------------------------------------
def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def compress(src, dst):
    started = time.time()
    try:
        duration = probe_duration(src)
    except Exception as exc:
        print("duration probe failed: %s" % exc, file=sys.stderr)
        duration = 0.0

    cmd = [
        "ffmpeg", "-hide_banner", "-y", "-i", src,
        "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
        "-map_metadata", "0",
        "-c:v", "libx265", "-preset", "medium", "-crf", "26",
        "-vf", "scale=-2:min(540\\,ih)",
        "-c:a", "aac", "-b:a", "256k",
        "-c:s", "copy",
        "-max_muxing_queue_size", "4096",
        "-progress", "pipe:1", "-nostats",
        dst,
    ]
    report(stage="compressing", progress=0, message="Starting encoder")

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

    size = os.path.getsize(dst)
    print("compressed to %s bytes" % size)
    report(stage="compressing", progress=100, eta_seconds=0,
           message=human(size), out_size=size)
    return size


# ---------------------------------------------------------------------------
# stage 3: upload the compressed file to the VPS in chunks
# ---------------------------------------------------------------------------
def upload(path):
    size = os.path.getsize(path)
    started = time.time()
    sent = 0
    seq = 0
    report(stage="uploading", progress=0, out_size=size, message="Sending to VPS")
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            seq += 1
            end = fh.tell()
            is_last = 1 if end >= size else 0
            url = "%s/worker/upload?job=%s&seq=%d&last=%d&ext=.mkv" % (
                CALLBACK, JOB_ID, seq, is_last)
            req = urllib.request.Request(url, data=chunk, method="POST")
            req.add_header("Content-Type", "application/octet-stream")
            req.add_header("Authorization", "Bearer " + TOKEN)
            req.add_header("User-Agent", USER_AGENT)
            try:
                with urllib.request.urlopen(req, timeout=900) as resp:
                    resp.read()
            except urllib.error.HTTPError as exc:
                _bail("upload rejected (%s): %s" % (exc.code, exc.read()[:300]))
            except Exception as exc:
                _bail("upload failed: %s" % exc)
            sent = end
            speed = int(sent / max(0.001, time.time() - started))
            if is_last:
                # Everything is on the VPS: report a full bar and hand over.
                report(stage="uploading", progress=100, speed_bps=speed,
                       eta_seconds=0, out_size=size, message="Sent to VPS")
                break  # the VPS takes over from here (to_pi -> done)
            eta = int((size - sent) / speed) if speed else None
            report(stage="uploading", progress=sent / size * 100,
                   speed_bps=speed, eta_seconds=eta, out_size=size)
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
    if "--report-failure" not in sys.argv and not SOURCE_URL:
        print("SOURCE_URL is required", file=sys.stderr)
        sys.exit(2)
    main()
