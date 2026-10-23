# HLS storage and downloads

Future jobs are encoded on GitHub Actions at one resolution: HEVC CRF 23, medium preset, bounded to 960×540 without upscaling, with two-second keyframes. One useful stereo track per language is retained. New audio is AAC 128 kbps; compatible AAC at or below that target is copied to avoid unnecessary loss. Unlabelled tracks remain separate rather than being discarded.

The runner packages the encoded streams without another encode into two-second fMP4 HLS segments. Video is stored once, with separate audio renditions, an RFC 6381 codec declaration derived from the initialization record, and SHA-256 inventories. The temporary transfer archive is uncompressed because the media is already compressed. The VPS validates it; the Pi verifies every extracted segment before publication. Permanent storage contains only HLS, not a second MP4.

## Pi installation

Install `internal/hls/assets/hls.py` as `/DATA/PTSLibraryTools/hls.py` (root-owned, mode 0644), and create `/DATA/PTSDownloads` with mode 0700. Install `pts-download-cleanup.service` and `pts-download-cleanup.timer` into `/etc/systemd/system/`, reload systemd, and enable the timer. This timer runs independently of the VPS every 30 seconds.

Linux filesystems supporting `RENAME_NOREPLACE` use a single no-clobber rename. On mergerfs, which rejects that flag, publication exclusively claims a new directory and installs its inventory last as the commit marker. Existing destinations are never overwritten. Library scanning ignores hidden incoming packages and packages without their final inventory.

The authenticated player uses Safari native HLS when available, otherwise the pinned self-hosted hls.js library. The main nginx CSP must include `media-src 'self' blob:`; scripts remain restricted to approved sources. hls.js workers are disabled. HEVC support is required: HLS playback never triggers a live encode or an audio-only fallback.

## Complete-movie downloads

The watch page requests a temporary MP4 containing the video and every retained audio language. The Pi stream-copies the HLS streams; it does not re-encode. Only one preparation runs at a time, with a maximum of two live temporary download requests. Preparation exposes measured percentage and estimated time until downloading can begin. Finalization and validation remain below 100% until the MP4 is ready. The page attempts to start the download and also provides a Save MP4 link if browser restrictions prevent automatic download.

Temporary disk space approximately equal to the movie size is needed. Downloads support HTTP ranges and retries. Once all byte ranges have been served, the file is scheduled for deletion after a five-minute grace period. This measures server delivery, not successful saving by the browser. Incomplete downloads expire within four hours of the initial request, including preparation time. Deadlines reserve one minute so the independent Pi cleanup timer can enforce the four-hour limit. At expiry, unfinished preparation is stopped and temporary files are removed. Very slow transfers must finish before that deadline; otherwise a new preparation may be necessary.

Requests and ownership persist in `stream_downloads`. A backend restart marks interrupted preparations failed and removes their Pi files; queued preparations resume. An expired download is not served. Original HLS media is never removed by download cleanup.

## Deployment

FFmpeg setup uses visible package-manager output, two installation attempts, 15-second HTTP/HTTPS acquisition timeouts, a 20-second package-lock wait, and outer limits of 90 seconds for updating package indexes and 150 seconds for installing FFmpeg. FFmpeg, ffprobe and libx265 are verified before downloading a movie. The complete setup step remains limited to ten minutes. A failed setup can be retried on a fresh GitHub runner; changing the workflow does not change an already-running workflow attempt.

Back up the database, binary, web assets, and nginx configuration. Install the Pi helper before starting the new backend; migrate `stream_downloads` and `stream_probes.video_mime`. Deploy backend source, embedded pipeline assets, schema, web assets, vendor library and license, and cleanup units together. Publish `.github/workflows/pts-download.yml` and `scripts/pts_pipeline.py` to the pipeline repository. Bump web asset and service-worker versions. Validate nginx, restart the backend, reload nginx, purge Cloudflare when a purge token is available, and verify public headers/assets plus authenticated HLS/range/download behavior.

Tests use an isolated loopback staging backend and disposable media. A synthetic GitHub Actions clip verifies the production encoder, 128 kbps audio, HLS packaging, authenticated upload, Pi extraction, and publication. A test-only H.264 fixture exercises MSE playback in headless browsers lacking HEVC; production output remains HEVC. Physical iPhone/Safari verification still requires an actual supported device.
