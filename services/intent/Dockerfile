FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    LAYA_HOST=0.0.0.0 \
    LAYA_PORT=8110 \
    LAYA_PRELOAD=1 \
    LAYA_MODELS=multilingual \
    LAYA_DEVICE=auto

RUN pip install "laya[serve]>=0.3.21,<0.4"

EXPOSE 8110
VOLUME ["/models"]

CMD ["python", "app.py"]
