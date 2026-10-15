FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./

RUN pip install --no-cache-dir ".[test]"

COPY . .

ENV PYTHONPATH=/app \
    HOME=/home/appuser

RUN adduser --system --home "${HOME}" appuser
USER appuser

EXPOSE 80

CMD ["bash", "-c", "alembic upgrade head && gunicorn -k uvicorn.workers.UvicornWorker app.main:app --workers 4 --bind 0.0.0.0:80"]
