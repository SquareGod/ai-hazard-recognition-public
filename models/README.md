# Local model directory

Do not commit model weights to this repository. Put private weights in this
directory only on the machine that runs inference, for example:

```text
models/
  your-safety-model.pt
  labels.yaml
  model-manifest.yaml
```

Set `DETECTOR_PROVIDER=yolo` and
`DETECTOR_MODEL_PATH=/app/models/your-safety-model.pt` in `.env` when using Docker.
See `docs/MODEL-INTEGRATION.md` for required labels, versioning,
checksums and the external model-service contract.
