#!/usr/bin/env bash
set -e

BDS_DIR="/opt/bedrock-server"
DATA_DIR="/data/bedrock-data"
PLAYIT_DIR="/data/playit"
BDS_VERSION="1.26.2.1"
BDS_URL="https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-${BDS_VERSION}.zip"
BDS_LOG="/data/bedrock-server.log"
PLAYIT_LOG="/data/playit.log"
DASHBOARD_LOG="/data/dashboard/dashboard.log"

mkdir -p "$DATA_DIR/worlds" "$PLAYIT_DIR" "/data/dashboard"

ensure_installed() {
    if [ ! -f "$BDS_DIR/bedrock_server" ]; then
        echo "[*] Bedrock Dedicated Server not found in $BDS_DIR. Installing $BDS_VERSION..."
        mkdir -p "$BDS_DIR"
        TMP_ZIP="/tmp/bedrock-${BDS_VERSION}.zip"
        curl -fsSL -A "Mozilla/5.0" -o "$TMP_ZIP" "$BDS_URL"
        unzip -q -o "$TMP_ZIP" -d "$BDS_DIR"
        chmod +x "$BDS_DIR/bedrock_server"
        rm -f "$TMP_ZIP"
        echo "[✓] Bedrock Dedicated Server installed."
    fi

    # Ensure persistent files exist in $DATA_DIR
    if [ ! -f "$DATA_DIR/server.properties" ]; then
        if [ -f "$BDS_DIR/server.properties" ]; then
            cp "$BDS_DIR/server.properties" "$DATA_DIR/server.properties"
        fi
        sed -i 's/^view-distance=.*/view-distance=6/' "$DATA_DIR/server.properties" 2>/dev/null || true
        sed -i 's/^tick-distance=.*/tick-distance=4/' "$DATA_DIR/server.properties" 2>/dev/null || true
        sed -i 's/^max-players=.*/max-players=10/' "$DATA_DIR/server.properties" 2>/dev/null || true
        sed -i 's/^server-name=.*/server-name=Bedrock 24\/7 Server/' "$DATA_DIR/server.properties" 2>/dev/null || true
        sed -i 's/^allow-cheats=.*/allow-cheats=true/' "$DATA_DIR/server.properties" 2>/dev/null || true
    fi

    [ -f "$DATA_DIR/allowlist.json" ] || cp -n "$BDS_DIR/allowlist.json" "$DATA_DIR/allowlist.json" 2>/dev/null || echo "[]" > "$DATA_DIR/allowlist.json"
    [ -f "$DATA_DIR/permissions.json" ] || cp -n "$BDS_DIR/permissions.json" "$DATA_DIR/permissions.json" 2>/dev/null || echo "[]" > "$DATA_DIR/permissions.json"

    # Link persistent files to BDS dir
    rm -f "$BDS_DIR/server.properties" "$BDS_DIR/allowlist.json" "$BDS_DIR/permissions.json"
    rm -rf "$BDS_DIR/worlds"
    ln -sf "$DATA_DIR/server.properties" "$BDS_DIR/server.properties"
    ln -sf "$DATA_DIR/allowlist.json" "$BDS_DIR/allowlist.json"
    ln -sf "$DATA_DIR/permissions.json" "$BDS_DIR/permissions.json"
    ln -sf "$DATA_DIR/worlds" "$BDS_DIR/worlds"
}

is_bds_running() {
    screen -list 2>/dev/null | grep -q '\.bedrock[[:space:]]'
}

is_playit_running() {
    screen -list 2>/dev/null | grep -q '\.playit[[:space:]]'
}

is_dashboard_running() {
    screen -list 2>/dev/null | grep -q '\.dashboard[[:space:]]'
}

start_server() {
    ensure_installed

    # Start cron daemon for 24/7 watchdog
    if ! pgrep -x cron >/dev/null; then
        service cron start >/dev/null 2>&1 || true
    fi

    if is_bds_running; then
        echo "[!] Bedrock server is already running in screen session 'bedrock'."
    else
        echo "[*] Starting Minecraft Bedrock Dedicated Server ($BDS_VERSION) in screen..."
        screen -dmS bedrock bash -c "
            while true; do
                echo \"[\$(date)] Starting Bedrock Server...\" | tee -a '$BDS_LOG';
                cd /opt/bedrock-server && LD_LIBRARY_PATH=. ./bedrock_server 2>&1 | tee -a '$BDS_LOG';
                EXIT_CODE=\$?;
                echo \"[\$(date)] Server stopped with exit code \$EXIT_CODE. Restarting in 5s...\" | tee -a '$BDS_LOG';
                sleep 5;
            done
        "
        echo "[✓] Bedrock server started in background (screen session: bedrock)."
    fi

    if is_playit_running; then
        echo "[!] Playit tunnel is already running in screen session 'playit'."
    else
        echo "[*] Starting Playit.gg tunnel daemon in screen..."
        screen -dmS playit bash -c "
            while true; do
                echo \"[\$(date)] Starting Playit tunnel...\" | tee -a '$PLAYIT_LOG';
                playitd --secret-path /data/playit/playit.toml 2>&1 | tee -a '$PLAYIT_LOG';
                EXIT_CODE=\$?;
                echo \"[\$(date)] Playit stopped with exit code \$EXIT_CODE. Restarting in 5s...\" | tee -a '$PLAYIT_LOG';
                sleep 5;
            done
        "
        echo "[✓] Playit tunnel started in background (screen session: playit)."
    fi

    if is_dashboard_running; then
        echo "[!] Dashboard is already running in screen session 'dashboard'."
    else
        echo "[*] Starting Web Management Dashboard (Port 5000) in screen..."
        screen -wipe >/dev/null 2>&1 || true
        screen -dmS dashboard bash -c '
            while true; do
                echo "[$(date)] Starting Dashboard..." >> /data/dashboard/dashboard.log;
                python3 /data/dashboard/app.py >> /data/dashboard/dashboard.log 2>&1;
                sleep 5;
            done
        '
        echo "[✓] Web Dashboard started on port 5000 (screen session: dashboard)."
    fi
}

stop_server() {
    echo "[*] Stopping Bedrock server cleanly..."
    if is_bds_running; then
        screen -S bedrock -X stuff "stop$(printf '\r')" 2>/dev/null || true
        echo "Waiting for server to save world..."
        for i in {1..10}; do
            if ! pgrep -x bedrock_server >/dev/null; then
                break
            fi
            sleep 1
        done
        screen -S bedrock -X quit 2>/dev/null || true
        pkill -9 -x bedrock_server 2>/dev/null || true
        echo "[✓] Bedrock server stopped."
    else
        echo "[-] Bedrock server was not running."
    fi

    if is_playit_running; then
        echo "[*] Stopping Playit tunnel..."
        screen -S playit -X quit 2>/dev/null || true
        pkill -f playitd 2>/dev/null || true
        echo "[✓] Playit tunnel stopped."
    fi

    if is_dashboard_running; then
        echo "[*] Stopping Dashboard..."
        screen -S dashboard -X quit 2>/dev/null || true
        pkill -f "python3 /data/dashboard/app.py" 2>/dev/null || true
        echo "[✓] Dashboard stopped."
    fi
}

restart_server() {
    stop_server
    sleep 2
    start_server
}

status_server() {
    echo "=================================================="
    echo "          MINECRAFT BEDROCK SERVER STATUS         "
    echo "=================================================="
    
    BDS_PID=$(ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}' | head -n 1 || true)
    if [ -n "$BDS_PID" ]; then
        MEM_KB=$(ps -o rss= -p "$BDS_PID" 2>/dev/null | tr -d ' ' || echo "0")
        MEM_MB=$(( MEM_KB / 1024 ))
        CPU_PCT=$(ps -o %cpu= -p "$BDS_PID" 2>/dev/null | tr -d ' ' || echo "0.0")
        echo "[●] BDS Process: RUNNING in screen 'bedrock' (PID: $BDS_PID)"
        echo "    - Version: $BDS_VERSION"
        echo "    - Memory Usage: ${MEM_MB} MB / 1024 MB (Budget)"
        echo "    - CPU Usage: ${CPU_PCT}%"
        echo "    - UDP Port: 19132 (Bedrock Mobile Default)"
    else
        echo "[○] BDS Process: STOPPED"
    fi

    PLAYIT_PID=$(ps -o pid=,stat= -C playitd 2>/dev/null | awk '$2 !~ /Z/ {print $1}' | head -n 1 || true)
    if [ -n "$PLAYIT_PID" ]; then
        echo "[●] Playit Tunnel: RUNNING in screen 'playit' (PID: $PLAYIT_PID)"
        echo "    - Status: ACTIVE & CONNECTED"
        echo "    - Address: nicely-retread.tun.ply.gg:17373"
    else
        echo "[○] Playit Tunnel: STOPPED"
    fi

    if is_dashboard_running; then
        echo "[●] Web Dashboard: RUNNING in screen 'dashboard' (Port 5000)"
    else
        echo "[○] Web Dashboard: STOPPED"
    fi

    echo "--------------------------------------------------"
    echo "Disk Usage on Persistent Volume (/data):"
    df -h /data | awk 'NR==1 || NR==2'
    echo "=================================================="
}

send_command() {
    if ! is_bds_running; then
        echo "[-] Error: Bedrock server is not running in screen."
        exit 1
    fi
    local CMD="$*"
    echo "[*] Sending command to server console: $CMD"
    screen -S bedrock -X stuff "$CMD$(printf '\r')"
}

attach_console() {
    if ! is_bds_running; then
        echo "[-] Error: Bedrock server is not running in screen."
        exit 1
    fi
    echo "[*] Attaching to Bedrock screen. Press Ctrl+A then D to detach without stopping."
    sleep 1
    screen -r bedrock
}

case "$1" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    restart)
        restart_server
        ;;
    status)
        status_server
        ;;
    console)
        attach_console
        ;;
    cmd|command)
        shift
        send_command "$@"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|console|command <cmd>}"
        exit 1
        ;;
esac
