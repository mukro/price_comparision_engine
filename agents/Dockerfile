FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl wget git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium
RUN playwright install-deps chromium

COPY . /app

CMD ["python", "-m", "agent.main"]
