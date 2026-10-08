FROM python:3.12-slim
ENV PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple/ \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY worker.py ./worker.py
# The Elasticsearch mappings are loaded from `config/` at runtime.
COPY config ./config
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
