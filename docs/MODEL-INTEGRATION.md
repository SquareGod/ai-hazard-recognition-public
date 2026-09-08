# Model integration guide

This guide is for the model team. It describes the two supported ways to plug a
trained model into the system, the exact HTTP contract of the external
algorithm service, and how to verify each mode without touching source code.
All configuration happens in the local `.env`; no code changes are needed.

```
               mode ①: local weights                  mode ②: HTTP algorithm service
.env: DETECTOR_PROVIDER=yolo                  .env: ALGORITHM_ADAPTER_PROVIDER=http
      DETECTOR_MODEL_PATH=/app/models/x.pt          ALGORITHM_ADAPTER_URL=http://svc:8080
              │                                             │
      local YOLO detector + VLM stage        your service implements the contract in §3
```

Both modes are read-only integrations: the frame scheduler, evidence storage,
hazard groups, notification and rectification workflow stay unchanged.

## 0. Where the models sit

```text
Camera / RTSP / Hikvision NVR
        │
        ├─ optional Windows Hikvision Bridge ──> MediaMTX
        │                                         │
Browser <── WebRTC or HLS <── API playback ticket ┤
                                                  └─ frame sampler
                                                        │
                                              detector + rule engine
                                                        │
                                             VLM / external verifier   ← your model plugs in here
                                                        │
                          hazard group → notification → rectification → closure
```

The browser never receives an NVR password. MediaMTX is the video data plane;
the FastAPI backend owns project permissions, stream sessions, sampling, AI
tasks, evidence, hazard groups, audit records and notifications. Repository
placement: model weights belong in the Git-ignored `models/` directory, external
service addresses and tokens in the local `.env`, and nothing private in
`frontend/`, `backend/`, `hikvision_bridge/` or `docs/`.

## 1. Mode ① — local weight file (YOLO)

1. Put the weight on the server host under `models/` (this directory is
   Git-ignored and bind-mounted to `/app/models` inside the backend container):

   ```text
   models/
     your-safety-model.pt
     labels.yaml              # model class → hazard label mapping (your record)
     model-manifest.yaml      # version, checksum, training scope, limits
   ```

2. Verify the file before deployment (SHA-256 must match the manifest you
   received):

   ```sh
   sha256sum models/your-safety-model.pt        # Linux
   certutil -hashfile models\your-safety-model.pt SHA256   # Windows
   ```

3. Point the backend at it in local `.env` (the path is the path **inside the
   backend container**, i.e. under `/app/models/...`):

   ```dotenv
   DETECTOR_PROVIDER=yolo
   DETECTOR_MODEL_PATH=/app/models/your-safety-model.pt
   DETECTOR_DEVICE=auto            # auto | cpu | cuda:0 ...
   ```

   `DETECTOR_MODEL_PATH` overrides the `detector.model_path` key in
   `backend/config/default.yaml`.

4. Verify startup:

   ```sh
   curl -s http://127.0.0.1:8010/api/v1/health
   ```

   Expect `"detector_provider": "yolo"`, your model path, and
   `"detector_model_ready": true` (false means the file is missing at the
   configured path — remember the container-side path).

5. Keep `backend/config/hazard_catalog.xlsx` (the 65-entry hazard label
   catalog) aligned with the model label mapping. A detector class is an object
   class only; the rule engine and the VLM stage decide whether it proves a
   hazard, so unknown classes must never be mapped to a hazard silently.

Downloading the zero-shot base model (no fine-tuned weight yet):
`docker compose exec backend python scripts/download_models.py` fetches the
model referenced by `DETECTOR_MODEL_PATH` through the Ultralytics downloader
into `/app/models`. For fine-tuned weights, skip this script and place your own
`.pt` file as described above.

GPU note: the stock image runs on CPU. For GPU inference add the NVIDIA
container runtime, `deploy.resources.reservations.devices` in `compose.yaml`
and matching torch wheels — verify with a functional CPU run first
(`DETECTOR_DEVICE=cpu`).

## 2. Mode ② — external HTTP algorithm service

Use this when the model team packages its own runtime, GPU stack or a combined
detector + rule + VLM pipeline behind HTTP.

### 2.1 Configuration (local `.env` only)

```dotenv
ALGORITHM_ADAPTER_PROVIDER=http     # local | none | mock | http | external_http
ALGORITHM_ADAPTER_URL=http://model-service:8080
ALGORITHM_ADAPTER_SCOPE=vlm         # vlm (default) | whole
ALGORITHM_ADAPTER_TOKEN=            # optional; sent as "Authorization: Bearer ..."
ALGORITHM_ADAPTER_TIMEOUT_SEC=180
```

`ALGORITHM_ADAPTER_URL` is required whenever the provider is `http`; the
backend fails fast at first request otherwise. To reach a service running on
the Docker host, use `http://host.docker.internal:8080`; to reach another
compose service, attach it to the `hazard_net` network and use its service
name.

### 2.2 Scope semantics

| `ALGORITHM_ADAPTER_SCOPE` | Local YOLO stage | External service answers for |
| --- | --- | --- |
| `vlm` (default) | runs locally; its candidates are shipped in `detections` | visual verification of frames/candidates only (replaces the VLM stage) |
| `whole` | skipped entirely | detection + decision, including the realtime path; it receives frames without local detections |

Rule-engine output is always advisory metadata. The external service remains
the authority for its own positive or negative visual verification, and it must
return an empty `findings` list when an image shows no visible evidence.

### 2.3 Service contract

The service must expose three endpoints. Requests carry
`Content-Type: application/json`; responses must be a JSON object.

**`GET /health`** — liveness/version probe. Any JSON object, e.g.
`{"status": "ok", "provider": "your-team", "version": "1.2.0"}`.

**`GET /capabilities`** — declared abilities, e.g.

```json
{"structured_output": true, "cancellation": false, "labels": "all", "scope": "whole"}
```

**`POST /analyze`** — request (fields generated by
`backend/app/algorithm_adapters.py`):

```json
{
  "frames": [
    {
      "project_id": "p-1", "work_area": "A1", "camera_id": "cam-1",
      "task_id": "job-123", "frame_timestamp": 1730000000.0,
      "context": {},
      "image_base64": "<JPEG bytes, base64>",
      "width": 1920, "height": 1080
    }
  ],
  "labels": [
    {"id": "H001", "category": "个体防护", "name": "未佩戴安全帽", "inspection_method": "..."}
  ],
  "detections": [
    {"object_id": "f1-0", "label": "helmet", "display_name": "安全帽", "score": 0.83,
     "bbox_xyxy": [120, 88, 340, 400], "frame_id": "f1"}
  ],
  "metadata": {},
  "cancellation": {"supported": false, "task_id": "job-123"}
}
```

- `frames[]` carries base64 JPEG images plus sampling context; `frames[].context`
  merges the caller's extra context map.
- `labels[]` is the allowed hazard catalog slice (`id/category/name/inspection_method`).
- `detections[]` is present in `scope=vlm` (local YOLO candidates; `score` in
  [0,1], `bbox_xyxy` = `[x1, y1, x2, y2]` integers) and empty in `scope=whole`.

Response — must validate against the backend `VLMResponse` schema
(`backend/app/schemas.py`):

```json
{
  "scene_summary": "one sentence describing the scene",
  "findings": [
    {
      "label_id": "H001",
      "status": "confirmed_hazard",
      "evidence": "what is visibly wrong in the frame",
      "visible_objects": ["person", "helmet"],
      "source_frame_ids": ["f1"],
      "inspection_visibility": "clear",
      "missing_external_evidence": [],
      "severity": "major",
      "severity_reason": "why this severity applies",
      "severity_source": "model"
    }
  ]
}
```

Field constraints enforced by the schema:

| Field | Allowed values |
| --- | --- |
| `status` | `confirmed_hazard` \| `review_required` |
| `inspection_visibility` | `clear` \| `partial` \| `unclear` (default `unclear`) |
| `severity` | `general` \| `major` |
| `severity_source` | `model` \| `realtime_rule` \| `catalog_rule` (default `model`) |
| `evidence`, `severity_reason` | non-empty strings |
| `scene_summary` | string, may be empty |

Errors and timeouts surface as a failed VLM stage; keep request handling bounded
and return a clear unavailable state instead of inventing findings.

### 2.4 Mock mode for contract testing

Your team can validate against the backend without any real model:
`DETECTOR_PROVIDER=mock` + `VLM_PROVIDER=mock` (the shipped defaults) run the
whole pipeline locally, and `ALGORITHM_ADAPTER_PROVIDER=mock` replaces the
external service with an in-process stub that answers the same `/analyze`
contract (`provider_name = "mock_algorithm_adapter"`).

## 3. Health self-check

| Check | Command | Healthy result |
| --- | --- | --- |
| Backend + provider status | `curl -s http://127.0.0.1:8010/api/v1/health` | `ok: true`, expected `detector_provider` / `vlm_provider`, `detector_model_ready: true` |
| Algorithm service reachability (no auth needed) | `docker compose exec backend python -c "import os,urllib.request as u; r=u.urlopen(os.environ['ALGORITHM_ADAPTER_URL'].rstrip('/')+'/health', timeout=5); print(r.status, r.read())"` | `200 {...}` |
| End-to-end adapter self-check | log into the web console, then `curl -s -H "Cookie: <session>" http://127.0.0.1:8010/api/v1/algorithm/capabilities` | `{"provider": "external_http", "health": {...}, "capabilities": {...}}` |

`GET /api/v1/algorithm/capabilities` requires a login session (it is a normal
authenticated API). When the external service is unreachable it answers
`503 外部算法服务不可用`; without an adapter configured it reports
`"provider": "local"` plus the active detector/VLM providers.

## 4. Delivery checklist for the model team

- weight file + SHA-256 checksum + manifest (version, training-data scope,
  supported classes, label mapping, inference size, confidence threshold,
  expected device, dependency versions, known limitations);
- a small authorised validation image set with ground truth;
- for mode ②: service image or deployment notes, `/health` `/capabilities`
  `/analyze` conformance to §2.3, and expected latency per frame.

Never ship or commit service tokens, NVR accounts or production `.env` files;
keep them in the server-side `.env` or a secret manager. See
[SECURITY.md](../SECURITY.md) and [DEPLOYMENT.md](DEPLOYMENT.md).
