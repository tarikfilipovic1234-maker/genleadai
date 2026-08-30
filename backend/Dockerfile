FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so code edits do not invalidate the pip cache.
COPY pyproject.toml ./
# Base set only - deliberately not ".[agent]". The Claude client libraries are
# absent from this image, so it cannot call a model regardless of how it is
# configured. Together with the config guard and the missing Node runtime that
# is three independent mechanisms enforcing the same rule: the deployed
# application serves recorded runs and holds no credentials.
RUN pip install --upgrade pip && pip install .

COPY . .

EXPOSE 8000

# Respects $PORT so the same image runs on platforms that assign one.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
