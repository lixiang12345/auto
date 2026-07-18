FROM python:3.12-slim

WORKDIR /app

COPY relay/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY relay/ ./relay/
COPY shared/ ./shared/

ENV PYTHONPATH=/app/relay:/app/shared
ENV DB_PATH=/data/accounts.db
VOLUME /data

EXPOSE 8000

CMD ["uvicorn", "relay.main:app", "--host", "0.0.0.0", "--port", "8000"]
