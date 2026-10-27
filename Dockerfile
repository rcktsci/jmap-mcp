FROM python:3.14-slim

WORKDIR /app

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY src ./src
COPY config.yml.example ./config.yml.example

# The MCP server listens on 8000 by default; the value can be changed with
# the environment (HOST / PORT).
EXPOSE 8000

CMD ["python", "main.py"]
