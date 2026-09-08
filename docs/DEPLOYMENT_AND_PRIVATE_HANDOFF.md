# Deployment and private handoff

## Local development

Use Docker Compose for local development and Linux deployment. It fixes the
frontend, backend, MediaMTX and system-library versions together, avoiding machine
specific Python, Node, FFmpeg and path differences.

```text
copy .env.example .env       # Windows CMD
start-public.cmd
```

On Linux, use `cp .env.example .env` then `sh scripts/start.sh`. Before using a
server, replace every development password in `.env`, set the server host in
`MEDIAMTX_WEBRTC_ADDITIONAL_HOSTS`, and put the public application behind HTTPS.

## Production server

Run the frontend, backend, MediaMTX and database volumes on Linux. Keep only the
HTTPS application entry point public. Keep MediaMTX API, RTSP, publish and Bridge
interfaces inside Docker, a VPN or a controlled private network. Back up the
database and evidence volume before upgrades.

Use a reverse proxy with TLS, a domain name, strong per-deployment passwords,
regular image updates and a separate production `.env` managed outside Git.

## Private delivery package

Share the following outside GitHub in an AES-256 encrypted archive, with the
archive password transmitted through a different channel:

- signed model weight, label mapping, manifest, checksum and runtime requirements;
- server-only `.env` template with values entered by the recipient, not a copied
  production `.env`;
- NVR endpoint and a newly created read-only preview account;
- private validation images and their handling restrictions;
- bridge-host installation notes.

Do not include cloud API keys, historical `.env` files, SQLite databases, email
authorisation codes or Windows DPAPI bridge runtime files. The recipient must enter
the NVR account again on their own bridge host so its credentials are encrypted for
that machine.
