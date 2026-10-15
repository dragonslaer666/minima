# Консоль поддержки Minima: FastAPI + SQLite, статика index.html раздаётся тем же процессом.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/app/data/minima-support.sqlite3

WORKDIR /app

# Не root: внутри контейнера приложение работает от отдельного пользователя.
RUN useradd --create-home --uid 10001 minima

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY index.html ./
RUN mkdir -p /app/data && chown -R minima:minima /app

USER minima
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
