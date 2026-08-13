FROM python:3.13-slim

WORKDIR /app

# Dependencies first, so a code change does not reinstall the world.
COPY pyproject.toml ./
COPY bot ./bot
RUN pip install --no-cache-dir .

# The WhatsApp webhook. The Telegram entrypoint is the same image with a
# different command — see docker-compose.yml.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn bot.api:app --host 0.0.0.0 --port ${PORT:-8080}"]
