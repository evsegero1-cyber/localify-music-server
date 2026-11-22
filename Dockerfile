FROM python:3.12-slim
ARG INSTALL_VK=0
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt requirements-vk.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$INSTALL_VK" = "1" ]; then pip install --no-cache-dir -r requirements-vk.txt; fi
COPY app.py providers.py ./
USER 10001:10001
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --no-proxy-headers --limit-concurrency 60"]
