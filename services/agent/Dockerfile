FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /app

RUN pip install --no-cache-dir \
    "fastapi==0.115.6" \
    "PyJWT[crypto]>=2.10,<3" \
    "pydantic>=2.7,<3" \
    "psycopg[binary,pool]>=3.2,<4" \
    "python-dotenv>=1,<2" \
    "redis>=5,<6" \
    "uvicorn[standard]==0.34.0" \
    "minio>=7.2.0,<8" \
    "httpx>=0.27,<1" \
    "python-multipart>=0.0.20,<1" \
    "python-docx>=1.2.0"

COPY app ./app
COPY worker.py knowledge_worker.py worker_supervisor.py ./

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
