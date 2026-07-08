FROM python:3.12.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m -u 10001 appuser
USER appuser
CMD ["python", "tcs_shopify_loader.py"]
