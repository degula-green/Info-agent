# Chromium stays out of the Agent process, the same reason the renderer lives
# in its own container: a browser is a large, frequently-updated attack surface
# and a crash there must degrade one capability, not the Agent service.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        "fastapi==0.115.6" \
        "uvicorn[standard]==0.34.0" \
        "playwright==1.63.0" \
        "pydantic>=2.7,<3"

COPY app ./app

ENV FORM_BROWSER_HOST=0.0.0.0 \
    FORM_BROWSER_PORT=8500 \
    FORM_BROWSER_HEADLESS=true

EXPOSE 8500

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8500"]
