# SnipTube

Download clipped sections of YouTube and Twitter/X videos without making the
user download the full file locally first.

## Architecture

SnipTube supports two deployment shapes:

- `public/index.html`: frontend
- `api/*.py`: Vercel Python functions for config, info, preview, download, and health
- `worker/app.py`: long-running Flask worker for local development or external hosting

On Vercel, the frontend can now use same-origin `/api/*` routes directly. If
`WORKER_URL` is set, the frontend still supports an external worker host.

## Why this shape

The original prototype routed downloads through a Vercel Python proxy. That is
not a good production path for media files because serverless functions are not
where you want long-running, high-bandwidth clip generation to happen.

## Deploy

### 1. Deploy on Vercel only

Deploy the root of this repo to Vercel. The frontend will use the same-origin
`/api/*` routes by default.

Optional Vercel environment variables:

- `WORKER_URL`: override the worker host instead of using same-origin routes
- `YTDLP_COOKIES_PATH`: path to a Netscape-format cookies file
- `YTDLP_COOKIES_B64`: base64-encoded cookies file contents

The cookies options matter because Twitter/X extraction may require authenticated
cookies depending on the video and the platform's current restrictions.

### 2. Optional external worker

You can still deploy `worker/app.py` separately on Railway, Render, Fly.io, or
any VPS that can run Docker. The `worker/Dockerfile` already installs ffmpeg and
starts Gunicorn.

### 3. Quick override for testing

You can temporarily point the frontend at any worker by opening:

```text
https://your-frontend.example.com/?worker=https://your-worker-host.example.com
```

## Local development

Start the worker:

```bash
cd worker
pip install -r requirements.txt
python app.py
```

Serve the frontend in another terminal:

```bash
cd public
python3 -m http.server 3000
```

Open `http://localhost:3000`.

When the frontend runs on localhost, it automatically expects the worker at
`http://localhost:8000`.

## Worker API

### `GET /api/info?url=<video-url>`

Returns metadata used by the editor UI.

Example response:

```json
{
  "id": "dQw4w9WgXcQ",
  "title": "Example title",
  "channel": "Example channel",
  "duration": 212,
  "thumb": "https://...",
  "platform": "Youtube"
}
```

### `GET /api/download`

Parameters:

- `url`: source video URL
- `format`: `mp4`, `mp3`, `webm`, or `gif`
- `start`: optional start time in seconds
- `end`: optional end time in seconds

Notes:

- GIFs are capped at 30 seconds
- Invalid ranges return `400`
- Output files are cleaned up automatically after the response

### `GET /health`

Returns worker health information:

```json
{
  "status": "ok",
  "ffmpeg": true,
  "cookies": false
}
```

## Current limits

- The worker still downloads source media server-side before trimming. That is
  acceptable for an MVP, but not yet optimized for very large source videos.
- Twitter/X support depends on what `yt-dlp` can access at request time. Cookies
  are the fallback when public extraction is blocked.
- There is no queueing or rate limiting yet, so this should not be treated as
  internet-scale infrastructure.
