FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so code edits do not invalidate the pip cache.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY . .

EXPOSE 8000

# This image is for the replay runtime and for deployment. It intentionally
# does NOT contain Node or the Claude Code CLI, so the live "sdk" runtime
# cannot run here - which is exactly the guarantee we want in production.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
