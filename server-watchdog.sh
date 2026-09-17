#!/usr/bin/env bash

# Make sure cron is running
if ! pgrep -x cron >/dev/null; then
    service cron start >/dev/null 2>&1 || true
fi

# Check if Bedrock Server is running
if ! ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}' | grep -q '^[0-9]'; then
    echo "[$(date)] Watchdog: Bedrock server not running. Starting..." >> /data/watchdog.log
    /data/server-control.sh start >> /data/watchdog.log 2>&1
fi

# Check if Playit is running
if ! ps -o pid=,stat= -C playitd 2>/dev/null | awk '$2 !~ /Z/ {print $1}' | grep -q '^[0-9]'; then
    echo "[$(date)] Watchdog: Playit not running. Starting..." >> /data/watchdog.log
    /data/server-control.sh start >> /data/watchdog.log 2>&1
fi

# Check if Dashboard is running
if ! ps -o pid=,stat=,args= -C python3 2>/dev/null | grep "app.py" | awk '$2 !~ /Z/ {print $1}' | grep -q '^[0-9]'; then
    echo "[$(date)] Watchdog: Dashboard not running. Starting..." >> /data/watchdog.log
    /data/server-control.sh start >> /data/watchdog.log 2>&1
fi
