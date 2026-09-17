FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install core dependencies (screen, curl, unzip, python3, cron, network utilities)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    screen \
    procps \
    cron \
    net-tools \
    libcurl4 \
    python3 \
    python3-pip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Set execution permissions
RUN chmod +x server-control.sh server-watchdog.sh

# Expose web dashboard port and Minecraft Bedrock UDP port
EXPOSE 5000 19132/udp

# Run 1-click turnkey setup and dashboard
CMD ["python3", "app.py"]
