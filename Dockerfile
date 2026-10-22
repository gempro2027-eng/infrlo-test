FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# PaaS sets $PORT; default to 8000 locally.
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-8000} --timeout 120 --workers 2"]
