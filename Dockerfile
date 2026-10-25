FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=Asia/Shanghai
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && groupadd -g 1001 rolebot && useradd -u 1001 -g rolebot -M -s /usr/sbin/nologin rolebot
COPY --chown=1001:1001 backend/rolebot /app/rolebot
USER 1001:1001
EXPOSE 8080
CMD ["uvicorn", "rolebot.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log", "--ws-max-size", "1048576"]
