# AI Hazard Recognition Public

An extensible construction-safety system for video monitoring, AI inspection,
hazard verification, notification, rectification and closure. The public edition
contains source code and four anonymised demonstration images only. It contains
no camera credentials, production databases, model weights, cloud API keys or
site data.

## Start the public demo

1. Install Docker Desktop on Windows, or Docker Engine with Compose on Linux.
2. Copy `.env.example` to `.env`.
3. Windows: run `start-public.cmd`. Linux/macOS: run `sh scripts/start.sh`.
4. Create the first administrator in a second terminal: `docker compose exec backend python create_admin.py`.
5. Open `http://localhost:3000`, log in, and upload a file from `demo/assets/`.

The default configuration deliberately uses Mock detector and Mock visual model.
It validates the user interface, task lifecycle and rectification workflow without
generating cloud-model charges. Change only local `.env` when you are ready to
enable a fine-tuned model, email, an external visual model or a camera.

## Documentation

- [Architecture and model integration](docs/MODEL_INTEGRATION_AND_ARCHITECTURE.md)
- [Server and private handoff guide](docs/DEPLOYMENT_AND_PRIVATE_HANDOFF.md)
- [Demonstration assets](demo/README.md)
- [Local model directory](models/README.md)
- [Security policy](SECURITY.md)

## Main capabilities

- Project-isolated users, cameras, hazards, notifications and rectification ledgers.
- Real-time viewing through MediaMTX with WebRTC-first and HLS fallback.
- Independent AI start/stop per camera; detection, rules, VLM verification and
  notification are decoupled tasks.
- One evidence image creates one hazard group, while its individual hazards can be
  verified, corrected, rectified and closed independently.
- Pluggable local YOLO, Qwen-compatible visual model, and external HTTP algorithm
  service interfaces.

## Important boundaries

This project is a software foundation, not a certified safety decision system.
Use authorised, read-only camera accounts, retain human verification before any
safety action, and do not expose MediaMTX control ports or the Hikvision bridge to
the public Internet.
