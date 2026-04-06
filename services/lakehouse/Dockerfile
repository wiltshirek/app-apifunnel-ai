FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends libmagic1 && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY src/ src/

ENV PYTHONUNBUFFERED=1
EXPOSE 3002

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "3002", "--workers", "2"]
