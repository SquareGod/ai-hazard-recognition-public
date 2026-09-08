# Deployment guide

The whole stack (frontend, FastAPI backend, MediaMTX) ships as one Docker
Compose project, so the machine only needs Docker; Python, Node, FFmpeg and
model runtimes stay inside images. Local demo and cloud server use the same
`compose.yaml`.

## 1. Prerequisites

- Windows: Docker Desktop (WSL2 backend). Linux: Docker Engine 24+ with the
  Compose plugin (`docker compose version`).
- Roughly 4 GB free disk for images (the backend image includes PyTorch).
- No GPU and no cloud API key are required for the default Mock mode.

## 2. Local start in about five minutes

1. Copy the safe template and review it:

   ```text
   Windows (CMD):  copy .env.example .env
   Linux / macOS:  cp .env.example .env
   ```

2. Start the stack:

   ```text
   Windows: start-public.cmd          (or: powershell -File scripts/start.ps1)
   Linux:   sh scripts/start.sh
   ```

   Both scripts check that the Docker engine is running, create `.env` from the
   template when missing, then run `docker compose up --build -d`.

3. Create the first administrator in a second terminal:

   ```sh
   docker compose exec backend python create_admin.py
   ```

   The command only works while no account exists yet. The password must be at
   least 10 characters and must be changed after the first login.

4. Open `http://localhost:3000`, log in, and upload an image from `demo/assets/`.

The template defaults to `DETECTOR_PROVIDER=mock` and `VLM_PROVIDER=mock`: the
full task lifecycle runs without any model weight or paid cloud call, and every
hazard finding is simulated. Real models are connected later through local
`.env` only; see [MODEL-INTEGRATION.md](MODEL-INTEGRATION.md).

## 3. Linux cloud server deployment

### 3.1 Host and firewall

Open only what the public entry needs; keep everything else internal:

| Port | Exposure | Purpose |
| --- | --- | --- |
| 22 | SSH, restrict to your office IP if possible | host administration |
| 80 / 443 | public | HTTPS entry (reverse proxy to the frontend on `127.0.0.1:3000`) |

Do **not** expose 8010 (backend), 8554 (RTSP), 8888 (HLS), 8889 (WebRTC),
8189/UDP, 9997 (MediaMTX API) or 8020 (Hikvision bridge). Compose already binds
them to `127.0.0.1` on the host; the backend and MediaMTX talk over the
internal Docker network. Example firewall rules (Ubuntu `ufw`):

```sh
ufw allow 22/tcp
ufw allow 80,443/tcp
ufw enable
```

### 3.2 Configure .env

Copy `.env.example` to `.env` on the server and change before the first start:

- every `development-*` password for MediaMTX users, and `CONTROL_API_KEY`
  (a long random string, e.g. `openssl rand -hex 32`). The backend refuses to
  boot when it listens on a non-loopback interface without `CONTROL_API_KEY`;
  inside the compose setup the frontend proxy injects it as `X-Control-Key`
  automatically.
- `MEDIAMTX_WEBRTC_ADDITIONAL_HOSTS` to the server's public IP or domain, so
  remote browsers can receive WebRTC.
- Optional SMTP settings; leave them blank to keep notifications disabled.

Keep `.env` out of Git (already covered by `.gitignore`) and back it up
separately from the code.

### 3.3 Start

```sh
docker compose up --build -d
docker compose ps
docker compose logs -f backend      # watch startup, Ctrl+C to stop watching
docker compose exec backend python create_admin.py
```

### 3.4 HTTPS reverse proxy (placeholder)

Terminate TLS at a reverse proxy that forwards to the frontend. The frontend
proxy then forwards `/api` calls to the backend, so the browser needs only one
origin. Minimal nginx server block — replace `hazards.example.com` and the
certificate paths with your own values:

```nginx
server {
    listen 80;
    server_name hazards.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name hazards.example.com;

    ssl_certificate     /etc/letsencrypt/live/hazards.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hazards.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 3600s;
    }
}
```

Issue certificates with the tool of your choice, for example
`certbot --nginx -d hazards.example.com`. Any reverse proxy (Caddy, Traefik,
cloud load balancer) works the same way: 443 → `127.0.0.1:3000`.

### 3.5 Data persistence and backups

| Data | Location | Persistence |
| --- | --- | --- |
| SQLite databases (accounts, hazards, rectification, jobs, NVR profiles) | named volume `backend_data` → `/app/data` | survives `down`/`up` |
| Logs, evidence frames, annotated images | named volume `backend_logs` → `/app/logs` | survives `down`/`up` |
| Model weights | bind mount `./models` → `/app/models` | host directory, ignored by Git |

Backup before upgrades:

```sh
docker compose exec backend tar -cz -C /app data logs | ssh backup-host "cat > hazard-$(date +%F).tar.gz"
```

Upgrades: pull the new code, then `docker compose up --build -d`. Volumes are
reused; remove them only when you intend to erase all records.

## 4. Common failures

### 4.1 Port already in use

Symptom: `docker compose up` fails with `port is already allocated` or
`bind: address already in use`. Find and stop the process that occupies the
port, or change the **host** side of the mapping in `compose.yaml` (left value
only), for example `"3000:3000"` → `"3001:3000"`, then adapt the reverse proxy
target.

```sh
netstat -ltnp | grep 3000        # Linux
netstat -ano | findstr :3000     # Windows
sudo lsof -i :3000               # macOS / Linux
```

### 4.2 Docker installed but not running

`start-public.cmd` / `scripts/start.sh` stop early with an engine error. Start
Docker Desktop (Windows) or `sudo systemctl start docker` (Linux), wait until
`docker info` succeeds, then rerun the start script.

### 4.3 Forgot the administrator password

Preferred path: another `system_admin` resets the account in 人员管理 (Personnel
management). If no administrator can log in, reset only the account tables —
hazard, rectification and audit records live in separate tables and survive:

```sh
docker compose exec backend python - <<'PY'
from app.auth import auth_store
with auth_store._connect() as db:
    db.execute("DELETE FROM auth_sessions")
    db.execute("DELETE FROM users")
print("accounts cleared")
PY
docker compose exec backend python create_admin.py
```

All previous accounts are removed; create the new administrator and recreate the
other accounts afterwards. `create_admin.py` alone refuses to run while any
account exists — that is expected.

### 4.4 Protected endpoints answer 401 “控制接口未授权”

A client reached the backend directly from a non-loopback address without the
`X-Control-Key` header. Use the frontend origin (port 3000 through the reverse
proxy) instead of calling `127.0.0.1:8010` services from outside, and keep
`CONTROL_API_KEY` consistent in `.env`.

### 4.5 Live view works locally but not from another network

Set `MEDIAMTX_WEBRTC_ADDITIONAL_HOSTS` to the public IP/domain (see 3.2) and
make sure UDP 8189 is reachable *from inside the deployment network*; for
strictly public deployments rely on HTTPS + HLS, which needs no extra ports.

## 5. Handling private artifacts

Model weights, validation images, NVR endpoints and server credentials must
never enter this repository. Share them outside Git in an encrypted archive
with the password sent through a different channel, enter secrets only in the
server-side `.env`, and use a newly created read-only NVR account on the bridge
host. See [MODEL-INTEGRATION.md](MODEL-INTEGRATION.md) for the expected model
delivery manifest, and [SECURITY.md](../SECURITY.md) for the full policy.
