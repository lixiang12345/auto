FROM python:3.12-slim

# System deps for headless Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg2 ca-certificates fonts-liberation \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers
RUN playwright install --with-deps chromium || playwright install chromium

COPY . /app/registration
COPY ../shared /app/shared

ENV DB_PATH=/data/accounts.db
ENV MODE=server
VOLUME /data

EXPOSE 8001
CMD ["python", "/app/registration/service.py"]
