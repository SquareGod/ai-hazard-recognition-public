# Architecture and model integration

## 1. System overview

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
                                             VLM / external verifier
                                                        │
                          hazard group → notification → rectification → closure
```

The browser never receives an NVR password. MediaMTX is the video data plane;
the FastAPI backend owns project permissions, stream sessions, sampling, AI tasks,
evidence, hazard groups, audit records and notifications.

## 2. Repository layout

| Directory | Responsibility | Do not place here |
| --- | --- | --- |
| `frontend/` | React/Vite console and authenticated API client | passwords, camera URLs or model keys |
| `backend/` | API, database, scheduling, rules, model adapters and tests | production `.env`, weights, uploads or databases |
| `hikvision_bridge/` | Optional Windows HCNetSDK-to-standard-stream bridge | SDK DLLs, NVR credentials or DPAPI runtime files |
| `deploy/` | MediaMTX configuration and Docker support files | server-specific secrets |
| `models/` | Local-only private weights, ignored by Git | public model binaries |
| `demo/` | Four anonymised UI demonstration images | real site imagery or ground truth |
| `docs/` | Public architecture, integration and deployment documentation | incident records or private contacts |

## 3. Runtime modes

The public `.env.example` starts `DETECTOR_PROVIDER=mock` and `VLM_PROVIDER=mock`.
This mode makes no paid cloud request and is only for deployment validation.

For production, real-time cameras normally use a small detector and rule engine at
a controlled frame rate. A visual large model verifies selected frames at a longer
interval or on detected candidates. The frame scheduler, event store and closure
workflow remain unchanged when models are replaced.

## 4. Connect a fine-tuned local YOLO model

1. Deliver the `.pt` weight, `labels.yaml`, a model manifest and SHA-256 checksum
   through a private channel.
2. Place them in the ignored `models/` directory.
3. In local `.env`, set:

```dotenv
DETECTOR_PROVIDER=yolo
DETECTOR_MODEL_PATH=/app/models/your-safety-model.pt
DETECTOR_DEVICE=auto
```

4. Keep the hazard label catalog in `backend/config/hazard_catalog.xlsx` aligned
   with the model label mapping. A model class is an object class; the rule engine
   and visual verifier decide whether it proves a hazard.
5. Start the system and confirm the health endpoint reports the expected provider,
   model version and readiness before enabling camera AI.

The model handoff must contain: version, training-data scope, supported classes,
label mapping, inference size, confidence threshold, expected device, dependency
versions, checksum, limitations and a small authorised validation set.

## 5. Connect an external algorithm service

Use an HTTP service when the model team packages its own runtime, GPU dependencies
or combined small-model/rule/VLM pipeline. Configure only local `.env`:

```dotenv
ALGORITHM_ADAPTER_PROVIDER=http
ALGORITHM_ADAPTER_SCOPE=whole
ALGORITHM_ADAPTER_URL=http://model-service:8080
ALGORITHM_ADAPTER_TOKEN=replace-with-local-secret
```

The service must provide:

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness and version status |
| `GET /capabilities` | Supported labels, scopes and cancellation support |
| `POST /analyze` | Structured inference request and hazard response |

`POST /analyze` receives `frames` with base64 image data, project/work-area/camera/
task/frame context, the allowed hazard label list, detector candidates and advisory
rule metadata. It returns the existing structured `VLMResponse`: scene summary and
findings with label ID, status, visible evidence, source frames, severity and
severity reason. The service must return no finding when the image does not provide
visible evidence.

Never put an external service token in source code, screenshots, examples or Git.

## 6. Deployment placement

| Place | Recommended responsibility |
| --- | --- |
| Edge box | RTSP decoding, low-rate small model, rule prefilter, encrypted outbound event/frame transfer |
| Linux server | frontend, backend, MediaMTX, database, VLM/external model service, notifications and closure |
| Windows bridge host | HCNetSDK/PlayCtrl/FFmpeg only, when an NVR requires the proprietary SDK |

The bridge publishes an H.264 standard stream to MediaMTX over a private VPN or
controlled LAN. It must use a new read-only NVR account. Do not move its encrypted
runtime directory to another machine; configure the new bridge host again.

## 7. Common integration failures

- **Weight not found:** verify the container path begins with `/app/models/` and
  the host file is mounted under `./models/`.
- **Wrong labels:** compare model classes, prompt catalog and hazard label IDs;
  never silently map unknown classes to a hazard.
- **No GPU:** set `DETECTOR_DEVICE=cpu` for a functional check, then add the proper
  vendor runtime and Docker GPU configuration for production.
- **External service timeout:** expose `/health`, use bounded request timeouts and
  return a clear unavailable state instead of creating a hazard record.
- **Camera plays but AI is idle:** check the AI switch for that camera, first-frame
  timestamp, sampler counters and algorithm-provider health separately.
