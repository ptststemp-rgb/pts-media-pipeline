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

## Where files end up

The VPS moves each finished file to the Pi (`PI_MOVIE_DIR` / `PI_SERIES_DIR`,
`/DATA/Movie` and `/DATA/Series` by default):

```
/DATA/Movie/<Title> (<Year>)/<Title> (<Year>).mkv
/DATA/Series/<Title>/Season <NN>/<Title> S<NN>E<NN>.mkv
```

Movie vs. series, title and year all come from the metadata saved when the job
was queued. The encoder command is:

```
ffmpeg -i source \
  -map 0:v:0 -map 0:a? -map 0:s? -map_metadata 0 \
  -c:v libx265 -preset medium -crf 26 -vf "scale=-2:min(540,ih)" \
  -c:a aac -b:a 256k -c:s copy \
  -max_muxing_queue_size 4096 out.mkv
```
