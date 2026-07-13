FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py dashboard.html ./

# data (jobs.sqlite + aggregates.json) is pulled from GCS at startup:
#   DATA_BUCKET / DATA_PREFIX  (see main.py)

ENV PORT=8080
CMD exec gunicorn --preload --bind :$PORT --workers 2 --threads 4 --timeout 120 main:app
