FROM python:3.11-slim

# System deps (just basics; psycopg2-binary ships with libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=10000

CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 8 chatbot:app