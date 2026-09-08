FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 libgl1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements.lock.txt ./
# Prefer the pinned lock file for reproducible builds; requirements.txt
# (supported ranges) remains the fallback.
RUN if [ -f requirements.lock.txt ]; then \
        pip install --no-cache-dir -r requirements.lock.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi
COPY . ./
EXPOSE 8010
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
