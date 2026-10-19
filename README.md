# PTS Library — download pipeline runner

This repo is the GitHub Actions half of the PTS Library download pipeline. The
PTS backend on the VPS dispatches `pts-download.yml` with `workflow_dispatch`
whenever a job is added to the queue.

For each job the runner:

1. downloads the source URL,
2. compresses it to **540p HEVC (H.265)** with **256 kbps AAC** on every audio
   track (subtitles copied, metadata preserved) in an `.mkv` container,
3. uploads the result to the VPS in 64 MiB chunks,
4. exits. The VPS then transfers the file to the Pi and deletes its own copy.

Progress is posted to the backend after every stage (`downloading`,
`compressing`, `uploading`) so the Queue page shows percentage, speed and ETA
for the stage that is running.

## Required secret

| Secret         | Value                                                        |
| -------------- | ------------------------------------------------------------ |
| `WORKER_TOKEN` | The same value as `WORKER_TOKEN` in `/DATA/PTSlibrary/.env`. |

`scripts/pts_pipeline.py` uses only the Python standard library, so there is
nothing to install.
