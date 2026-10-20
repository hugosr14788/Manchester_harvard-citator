FROM python:3.12-slim
# poppler-utils: read PDF titles/dates · wamerican: word list for sentence-casing titles
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils wamerican ca-certificates \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir certifi
WORKDIR /app
COPY server.py index.html ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
