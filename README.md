# CEM Setup Guide - From Clone to Running App

This is a start-to-finish guide for standing up the CEM Toolkit: frontend and backend, both as Docker containers, plus the optional local watcher for machine-side analysis.
Follow it top to bottom on a clean machine and you end up with a working app.

## What you end up with

Two independent Docker containers, each from its own repo:

- **`cem-frontend`** - the static SPA (vanilla JS), served by nginx on port 8080.
- **`cem-backend`** - the FastAPI + BirdNET analysis API, on port 8000.

They talk to each other over plain HTTP: the frontend calls the backend's REST API, and both talk to Google (OAuth login + Drive storage) directly from the browser.
Nothing else is required to get a working local setup; Earth Engine stratification and the local watcher are both optional add-ons, covered near the end.

## Prerequisites

- Docker Desktop (or Docker Engine + Compose) - both containers build and run through it.
- Git.
- A Google account, to create the OAuth client and API key in the next step.
- Python 3.10+ - only needed if you also want to run the local watcher (Step 6).

## Step 1: Clone both repos

```bash
git clone https://github.com/xHrid/cem-frontend.git
git clone https://github.com/xHrid/cem-backend.git
```

They are independent repos with independent git history; nothing assumes a shared parent folder, though this guide's paths assume they sit side by side.

## Step 2: Create a Google OAuth Client ID and Picker API Key

CEM uses Google for two things: signing users in, and storing all project data in the user's own Google Drive (no CEM-hosted database).
Both need credentials from a Google Cloud project.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a project (or pick an existing one).
2. **Enable APIs.** Under "APIs & Services" > "Library", enable:
   - Google Picker API
   - Google Drive API
3. **OAuth consent screen.** Under "APIs & Services" > "OAuth consent screen", set it up (External is fine for testing) and add these scopes:
   ```
   openid email profile https://www.googleapis.com/auth/drive.file
   ```
   `openid email profile` identifies the user (name, email, a stable account ID).
   `drive.file` is what lets CEM read/write only the files and folders it creates in the user's Drive, nothing else in their account.
4. **Create the OAuth Client ID.** Under "APIs & Services" > "Credentials" > "Create Credentials" > "OAuth client ID", type **Web application**.
   Add every origin the app will actually be served from under "Authorized JavaScript origins", for example:
   ```
   http://localhost:8080
   https://your-production-domain.example
   ```
   Save it. You get a client ID that looks like `1234567890-abc123xyz.apps.googleusercontent.com`.
5. **Create the Picker API key.** Still under "Credentials", "Create Credentials" > "API key".
   Restrict it to the Google Picker API (and Drive API if prompted), and restrict it to the same HTTP referrers as your OAuth origins above, so the key can't be reused elsewhere.

Keep both values, `GOOGLE_CLIENT_ID` and `PICKER_API_KEY`, at hand for Step 4.

## Step 3: Backend

```bash
cd cem-backend
cp .env.example .env
```

Open `.env` and set `ALLOWED_ORIGINS` to the frontend origin you'll actually use, for example:

```
ALLOWED_ORIGINS=http://localhost:8080
```

This is the one setting most setups get wrong: the backend rejects browser requests from any origin not in this list, silently, as a CORS failure in the browser console rather than a clear error.
It must be the frontend's exact scheme+host+port, comma-separated if there's more than one (no trailing slash).

The other `.env` values (`HOST_DATA_DIR`, `BIRDNET_MAX_WORKERS`, `MAX_UPLOAD_MB`, retention, STAC, Airflow) all have working defaults; see the comments in `.env.example` if you need to change them.

Build and start:

```bash
docker compose up --build -d
curl localhost:8000/health
```

`{"status":"ok"}` means it's up.
Interactive API docs are at `http://localhost:8000/docs`.

The compose file bind-mounts `./pipeline` and `./server/app` into the container, so editing a script only needs `docker compose restart`, no rebuild.
Rebuild (`--build`) only when `requirements.txt` or `server/requirements-server.txt` changes.

## Step 4: Frontend

```bash
cd ../cem-frontend
GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com" \
PICKER_API_KEY="your-picker-api-key" \
SERVER_BASE_URL="http://localhost:8000" \
bash generate_config.sh
```

This writes `js/core/Config.js`, which is git-ignored and never baked into the Docker image, so this step is required once per machine and again any time a value changes.

- `GOOGLE_CLIENT_ID` / `PICKER_API_KEY` - from Step 2.
- `SERVER_BASE_URL` - the backend's URL from the browser's point of view.
  Leave unset entirely to run frontend-only with local analysis (watcher) instead of the server.
- `AIRFLOW_TRIGGER_URL` - leave unset unless you're routing through Airflow (Step 7 territory, not needed for a first setup).

Now build and start the frontend container:

```bash
docker compose up --build -d
```

Open `http://localhost:8080`.
The compose file bind-mounts the source (including the freshly generated `Config.js`) over the image's baked-in copy, so from here on, editing any frontend file and running `docker compose restart` is enough, no rebuild.

At this point sign-in and Drive-backed storage should work, and any analysis you run from the UI dispatches to the backend from Step 3.

## Step 5: Earth Engine (optional, only for stratification)

Stratification (splitting a study area into strata for recorder placement) calls Google Earth Engine, which needs its own credentials, separate from the OAuth client above.

On the **host machine running Docker**:

```bash
pip install earthengine-api
earthengine authenticate
```

This opens a browser, you sign in, and it writes credentials to `~/.config/earthengine/credentials`.
Your Google account's GEE access must be tied to a Cloud project with the Earth Engine API enabled (the default `GEE_PROJECT` is `ee-geeapi`; set your own if different).

In `cem-backend/.env`, add:

```
GEE_PROJECT=ee-geeapi
EARTHENGINE_CREDENTIALS=/absolute/path/to/.config/earthengine
```

Use an absolute path; `~` does not reliably expand inside a compose volume mount.
Leave `GEE_SERVICE_ACCOUNT*` blank (service-account auth is a separate path, only needed for unattended/server accounts).

```bash
docker compose up -d
docker compose exec api ls -l /root/.config/earthengine   # confirm the credentials landed inside the container
```

## Step 6: The local watcher (optional, for local-machine analysis)

The backend (Steps 3-5) is one of two ways to run analysis; the other is the **watcher**, a small Python daemon that runs on a user's own machine and uses the local file system as the queue instead of the network.
It's useful for a researcher who wants to run BirdNET locally without uploading audio anywhere.

From a folder the app has been given local storage access to (via "Select Storage" in the UI, which also copies `watcher.py` into that folder):

```bash
python watcher.py
```

On first run it creates its own virtual environment under `system/.venv` and installs pipeline dependencies (numpy, pandas, librosa, tensorflow-cpu, birdnetlib, ...), which takes a few minutes and needs outbound internet access to PyPI and to `raw.githubusercontent.com/xHrid/cem-backend` (it pulls the pipeline scripts from there, tracking the `master` branch).
After that it polls for job files the UI writes and runs them.

The UI's watcher indicator reads a heartbeat file the daemon writes; **online** means it saw a heartbeat recently, **stale/offline** means it hasn't (either the watcher isn't running, or it's busy on something slower than the poll interval, like a first-time dependency install).

**Troubleshooting:**
- *"Another watcher instance is already running (PID N)"* on a fresh start, with no watcher actually running: a previous run crashed without releasing `system/watcher.lock`.
  Delete that file and restart.
  This is more likely on Windows, where a crashed watcher's PID can later be reused by an unrelated process, fooling the staleness check.
- *`ImportError: DLL load failed ... An Application Control policy has blocked this file`* (seen on locked-down/managed Windows machines, e.g. institutional IT-managed laptops): a Windows Application Control policy is blocking one of the pipeline's native dependencies (`numba`, pulled in by `librosa`) from loading.
  This is a machine security policy, not a bug in the app; ask IT to allow the blocked file under the watcher's venv, or run the watcher on an unmanaged machine.
- `BrokenProcessPool` during BirdNET runs: usually memory pressure from too many parallel workers, each loading its own TensorFlow model.
  Lower `BIRDNET_MAX_WORKERS` (same variable as the backend's) or close other memory-heavy apps.

## Everything in one place: troubleshooting summary

| Symptom | Cause | Fix |
|---|---|---|
| Analysis requests fail from the browser console with a CORS error | Backend `ALLOWED_ORIGINS` doesn't include the frontend's exact origin | Set it in `cem-backend/.env`, restart the backend |
| App loads but sign-in/Drive storage silently does nothing | `Config.js` was never generated, or has stale values | Re-run `generate_config.sh` (Step 4), restart the frontend container |
| `Please authorize access to your Earth Engine account` | GEE credentials missing/not mounted | Step 5 |
| Watcher stuck "Offline (stale)" right after starting | First-run dependency install can take several minutes; heartbeat only resumes once it's done | Wait, or check the watcher's console output for real errors |
| Watcher won't start: "Another watcher instance is already running" | Stale lock file from a crash | Delete `system/watcher.lock` |

## Repo layout, for orientation

```
cem-frontend/
├── index.html, js/, styles/, leaflet/, images/   ← the SPA
├── watcher.py               ← local-analysis daemon; also served to the browser for "Select Storage"
├── generate_config.sh       ← writes js/core/Config.js (git-ignored)
├── Dockerfile, docker-compose.yml, nginx.conf
└── cloudflare-proxy/        ← separate Cloudflare Worker, only relevant if replacing the default CORS_PROXY_URL

cem-backend/
├── pipeline/                 ← analysis scripts; single source of truth (watcher pulls these from GitHub too)
├── server/app/                ← FastAPI server code
├── requirements.txt           ← pipeline dependencies
├── Dockerfile, docker-compose.yml
└── .env.example
```
