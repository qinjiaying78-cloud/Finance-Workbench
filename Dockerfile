FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FW_REQUIRE_AUTH=true PORT=8000
ENV FINANCE_DB_PATH=/data/finance_workbench.db FINANCE_UPLOADS_DIR=/data/uploads FINANCE_SECRET_DIR=/data/private
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt && useradd -u 10001 -m finance && mkdir /data && chown finance /data
COPY backend backend
COPY index.html index.html
COPY assets assets
USER finance
EXPOSE 8000
CMD ["python","-m","backend.run"]
