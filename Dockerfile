FROM python:3.12-slim

WORKDIR /app

# Cài system deps tối thiểu (httpx cần ca-certs để kết nối TikTok HTTPS)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Biến mặc định — ghi đè qua --env hoặc env_file trong docker-compose
ENV TIKTOK_USERNAME=""
ENV TIKTOK_LIVE_RETRY_SEC=30
ENV COMMENT_BUFFER_SIZE=50
ENV COMMENT_MAX_AGE_SEC=30

EXPOSE 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
