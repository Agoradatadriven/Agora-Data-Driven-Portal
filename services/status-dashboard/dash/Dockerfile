FROM python:3.12.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m -u 10001 appuser
USER appuser
# Cloud Run enforces the request timeout itself, so gunicorn uses --timeout 0.
CMD exec gunicorn --bind :$PORT --workers 2 --threads 8 --timeout 0 main:app
