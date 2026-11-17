FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_URL=postgresql://postgres:postgres@localhost:5432/znz \
    SANDBOX_SCRATCH_DIR=/tmp/znz_sandbox \
    HOST=0.0.0.0 \
    PORT=5001

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY SYSTEM_PROMPT.md feishu_card_config.yaml db_adapter.py feishu_bot.py api_server.py ./
COPY agent ./agent
COPY rule_patrol ./rule_patrol

RUN mkdir -p /tmp/znz_sandbox

EXPOSE 5001
CMD ["python", "api_server.py"]
