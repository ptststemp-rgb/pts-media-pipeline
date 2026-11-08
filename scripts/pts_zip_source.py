"""Read one video member of a public, range-capable ZIP without staging the ZIP.

Used for admin preflight on the VPS and for extraction on the GitHub runner.
Never pass signed source URLs on the command line or into workflow inputs.
"""
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import zipfile
from urllib.parse import urljoin, urlsplit

import requests

ZIP_LIMIT = 100 * (1 << 30)
MEMBER_LIMIT = 8 * (1 << 30)
INDEX_LIMIT = 8 * (1 << 20)
FILE_LIMIT = 500
VIDEO = re.compile(r"\.(mp4|m4v|mkv|mov|webm|avi|ts)$", re.I)
CHUNK = 1 << 20


class SourceError(ValueError):
    pass


def public_host(url):
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.fragment or len(url) > 4096):
        raise SourceError("ZIP source must be a public HTTPS link without a fragment")
    for item in socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(item[4][0]).is_global:
            raise SourceError("ZIP source must resolve only to public addresses")


class RangedZip(io.RawIOBase):
    """Seekable range reader; refuses HTTP 200 fallbacks and changed source size."""

    def __init__(self, url, session=None):
        self.url = url
        self.session = session or requests.Session()
        self.owned_session = session is None
        self.size = 0
        self.pos = 0
        self.cache_start = -1
        self.cache = b""
        self._probe()

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=os.SEEK_SET):
        if whence not in (os.SEEK_SET, os.SEEK_CUR, os.SEEK_END):
            raise ValueError("invalid whence")
        pos = (0, self.pos, self.size)[whence] + offset
        if pos < 0 or pos > self.size:
            raise SourceError("ZIP seek outside source")
        self.pos = pos
        return pos

    def close(self):
        if self.owned_session:
            self.session.close()
        super().close()

    def _range(self, start, end):
        url = self.url
        for _ in range(6):
            public_host(url)
            response = self.session.get(url, headers={"Range": f"bytes={start}-{end}",
                             "Accept-Encoding": "identity"}, stream=True,
                             allow_redirects=False, timeout=(10, 30))
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise SourceError("ZIP redirect has no location")
            url = urljoin(url, location)
        else:
            raise SourceError("ZIP redirected too many times")
        try:
            if response.status_code != 206:
                raise SourceError(f"ZIP requires HTTP ranges (received {response.status_code})")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if not match or (int(match[1]), int(match[2])) != (start, end):
                raise SourceError("Invalid ZIP Content-Range")
            size = int(match[3])
            if size < 1 or size > ZIP_LIMIT or (self.size and size != self.size) or end >= size:
                raise SourceError("ZIP is too large or changed while reading")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise SourceError("ZIP HTTP response must not be encoded")
            length = response.headers.get("Content-Length")
            if length and int(length) != end - start + 1:
                raise SourceError("ZIP range length mismatch")
            self.size = size
            # Stop at the requested length even if a server sends extra data.
            data = response.raw.read(end - start + 1)
            if len(data) != end - start + 1:
                raise SourceError("Incomplete ZIP range")
            self.url = url
            return data
        finally:
            response.close()

    def _probe(self):
        self._range(0, 0)

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        if n > INDEX_LIMIT:
            raise SourceError("ZIP reader requested an oversized range")
        n = min(n, self.size - self.pos)
        out = bytearray()
        while len(out) < n:
            if not self.cache_start <= self.pos < self.cache_start + len(self.cache):
                self.cache_start = self.pos
                self.cache = self._range(self.pos, min(self.size - 1, self.pos + max(CHUNK, n - len(out)) - 1))
            count = min(n - len(out), self.cache_start + len(self.cache) - self.pos)
            out.extend(self.cache[self.pos - self.cache_start:self.pos - self.cache_start + count])
            self.pos += count
        return bytes(out)


def inspect(archive):
    # zipfile reads the central directory via ranges, not the archive body.
    files = archive.infolist()
    if not files or len(files) > FILE_LIMIT:
        raise SourceError("ZIP contains too many entries (maximum 500)")
    if sum(len(f.filename.encode("utf-8")) + 46 for f in files) > INDEX_LIMIT:
        raise SourceError("ZIP index exceeds 8 MiB")
    seen = set()
    videos = []
    for info in files:
        name = info.filename
        parts = name[:-1] if info.is_dir() else name
        if (not parts or name.startswith("/") or "\\" in name or "\x00" in name
                or any(p in ("", ".", "..") for p in parts.split("/"))
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or len(name.encode("utf-8")) > 1000):
            raise SourceError("ZIP contains an unsafe filename")
        if name in seen:
            raise SourceError("ZIP contains duplicate filenames")
        seen.add(name)
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise SourceError("ZIP contains a symbolic link")
        if info.flag_bits & 1:
            raise SourceError("Encrypted ZIP entries are not supported")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise SourceError("Unsupported ZIP compression method")
        if info.is_dir():
            continue
        if info.file_size > MEMBER_LIMIT:
            raise SourceError("ZIP member exceeds the 8 GiB runner safety cap")
        if VIDEO.search(name):
            videos.append({"name": name, "size": info.file_size})
    if not videos:
        raise SourceError("ZIP has no supported video files")
    return videos


def extract(url, member, dest, report=None):
    if not member or len(member.encode("utf-8")) > 1000:
        raise SourceError("Invalid ZIP member name")
    with RangedZip(url) as source, zipfile.ZipFile(source) as archive:
        videos = inspect(archive)
        selected = next((v for v in videos if v["name"] == member), None)
        if selected is None:
            raise SourceError("Selected episode is missing from the ZIP")
        size = selected["size"]
        if size < 1:
            raise SourceError("Selected episode is empty")
        if shutil.disk_usage(os.path.dirname(os.path.abspath(dest))).free < size + (2 << 30):
            raise SourceError("Runner does not have enough space for this episode and its output")
        # Failure leaves only a partial file on temporary runner disk.
        with archive.open(member) as incoming, open(dest, "wb") as outgoing:
            done = 0
            while True:
                block = incoming.read(CHUNK)
                if not block:
                    break
                done += len(block)
                if done > size:
                    raise SourceError("ZIP member exceeded its declared size")
                outgoing.write(block)
                if report:
                    report(done, size)
            if done != size:
                raise SourceError("ZIP member is incomplete")


if __name__ == "__main__":
    # Source URL comes from stdin, never argv; stdout is metadata only.
    try:
        data = json.load(sys.stdin)
        if data.get("action") != "inspect":
            raise SourceError("Unsupported action")
        with RangedZip(data["url"]) as source, zipfile.ZipFile(source) as archive:
            videos = inspect(archive)
            print(json.dumps({"members": videos, "size": source.size}))
    except (ValueError, KeyError, OSError, requests.RequestException, zipfile.BadZipFile) as exc:
        # URLs from underlying requests errors can contain signed tokens; never echo them.
        print(json.dumps({"error": str(exc) if isinstance(exc, SourceError) else "Cannot read ZIP index or source link"}))
        sys.exit(1)
