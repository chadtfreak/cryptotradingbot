FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY config.toml .

ENV PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "-m", "bot", "run", "--host", "0.0.0.0", "--port", "8765"]
