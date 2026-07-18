FROM python:3.12-slim
WORKDIR /app
COPY dashboard/server.py ./server.py
COPY dashboard/index.html ./index.html
EXPOSE 8080
CMD ["python", "server.py"]
