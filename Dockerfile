FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tor && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Tor hidden service dir
RUN mkdir -p /var/lib/tor/hidden_service && chmod 700 /var/lib/tor/hidden_service

EXPOSE 5000

# Start both Tor and Flask via supervisord or a simple script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
