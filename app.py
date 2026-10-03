import os
import re
import io
import gc
import json
import time
import shutil
import zipfile
import secrets
import datetime
import threading
import subprocess
import urllib.request
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
if not os.path.exists(TEMPLATE_DIR):
    TEMPLATE_DIR = "/data/dashboard/templates"

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB max world upload

# Primary persistent directory resolution
DATA_DIR = os.environ.get("DATA_DIR", "/data")
if not (os.path.exists(DATA_DIR) and os.access(DATA_DIR, os.W_OK)):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        DATA_DIR = os.path.join(BASE_DIR, "data")
        os.makedirs(DATA_DIR, exist_ok=True)

DASHBOARD_DIR = os.path.join(DATA_DIR, "dashboard")
BEDROCK_DATA = os.path.join(DATA_DIR, "bedrock-data")
PLAYIT_DIR = os.path.join(DATA_DIR, "playit")
WORLDS_DIR = os.path.join(BEDROCK_DATA, "worlds")
ACTIVE_WORLD_DIR = os.path.join(WORLDS_DIR, "Bedrock level")
BACKUP_DIR = os.path.join(BEDROCK_DATA, "worlds_backup")
PROPERTIES_FILE = os.path.join(BEDROCK_DATA, "server.properties")
ALLOWLIST_FILE = os.path.join(BEDROCK_DATA, "allowlist.json")
PERMISSIONS_FILE = os.path.join(BEDROCK_DATA, "permissions.json")
KNOWN_PLAYERS_FILE = os.path.join(BEDROCK_DATA, "known_players.json")
PASSWORD_FILE = os.path.join(DASHBOARD_DIR, "password.txt")
SECRET_KEY_FILE = os.path.join(DASHBOARD_DIR, "secret.key")
SERVER_LOG_FILE = os.path.join(DATA_DIR, "bedrock-server.log")
PLAYIT_LOG_FILE = os.path.join(DATA_DIR, "playit.log")
VERSION_FILE = os.path.join(DATA_DIR, "version.txt")
SERVER_CONTROL_SCRIPT = os.path.join(BASE_DIR, "server-control.sh")
if not os.path.exists(SERVER_CONTROL_SCRIPT):
    SERVER_CONTROL_SCRIPT = os.path.join(DATA_DIR, "server-control.sh")

# Global version switch background task tracking
VERSION_SWITCH_STATE = {
    "status": "idle",  # "idle" | "running" | "completed" | "error"
    "step": "",
    "progress": 0,
    "message": "",
    "version": "",
    "error": ""
}
VERSION_SWITCH_LOCK = threading.Lock()

TUNNEL_DOMAIN = "nicely-retread.tun.ply.gg"
TUNNEL_IP = "147.185.221.213"
TUNNEL_PORT = 17373
PLAYIT_SECRET = "3f951dcf83b320ccdf736109ca0c51d67287516c996a1d67427b97a32aa6f26a"

# GitHub automated cloud backup settings
def get_github_token():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    out, _, code = run_bash("git remote get-url origin 2>/dev/null")
    if code == 0 and "://" in out:
        m = re.search(r"https://[^:]+:([^@]+)@github\.com", out)
        if m:
            return m.group(1).strip()
    token_file = os.path.join(DATA_DIR, "github_token.txt")
    if os.path.exists(token_file):
        try:
            with open(token_file, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""

GITHUB_REPO = os.environ.get("GITHUB_REPO", "batz-dev/minecraft-bedrock-247")
GITHUB_BACKUP_BRANCH = os.environ.get("GITHUB_BACKUP_BRANCH", "world-backup")
LAST_BACKUP_INFO_FILE = os.path.join(DATA_DIR, "last_backup_info.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DASHBOARD_DIR, exist_ok=True)
os.makedirs(BEDROCK_DATA, exist_ok=True)
os.makedirs(PLAYIT_DIR, exist_ok=True)
os.makedirs(WORLDS_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# Secret key setup
if not os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE, "w") as f:
        f.write(secrets.token_hex(32))
with open(SECRET_KEY_FILE, "r") as f:
    app.secret_key = f.read().strip()

# Password setup
if not os.path.exists(PASSWORD_FILE):
    with open(PASSWORD_FILE, "w") as f:
        f.write("admin123")


def get_admin_password():
    try:
        with open(PASSWORD_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return "admin123"


def is_authenticated():
    return session.get("logged_in") is True


def run_bash(cmd):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        return res.stdout.strip(), res.stderr.strip(), res.returncode
    except Exception as e:
        return "", str(e), 1


def get_current_bds_version():
    for path in [
        VERSION_FILE,
        os.path.join(BASE_DIR, "version.txt"),
        SERVER_CONTROL_SCRIPT,
        "/data/server-control.sh"
    ]:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    content = f.read().strip()
                    if path.endswith(".txt") and content:
                        return content
                    for line in content.splitlines():
                        if line.startswith("BDS_VERSION="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    return "1.26.2.1"


def get_world_size():
    total = 0
    if os.path.exists(ACTIVE_WORLD_DIR):
        for dirpath, dirnames, filenames in os.walk(ACTIVE_WORLD_DIR):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
    if total < 1024:
        return f"{total} B"
    elif total < 1024 * 1024:
        return f"{total / 1024:.1f} KB"
    else:
        return f"{total / (1024 * 1024):.1f} MB"


def get_online_players():
    players = set()
    max_p = 10

    if os.path.exists(PROPERTIES_FILE):
        try:
            with open(PROPERTIES_FILE, "r") as f:
                for line in f:
                    if line.startswith("max-players="):
                        max_p = int(line.split("=", 1)[1].strip())
                        break
        except Exception:
            pass

    if not os.path.exists(SERVER_LOG_FILE):
        return [], max_p

    try:
        with open(SERVER_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-300:]

        for line in lines:
            if "Server started." in line or "Quit: server stop" in line:
                players.clear()
            elif "Player connected:" in line:
                m = re.search(r"Player connected:\s*([^,]+)", line)
                if m:
                    players.add(m.group(1).strip())
            elif "Player disconnected:" in line:
                m = re.search(r"Player disconnected:\s*([^,]+)", line)
                if m:
                    players.discard(m.group(1).strip())
    except Exception:
        pass

    return sorted(list(players)), max_p


def get_xuid_map():
    xuid_to_name = {}
    name_to_xuid = {}

    # Load from persistent known_players.json if it exists
    if os.path.exists(KNOWN_PLAYERS_FILE):
        try:
            with open(KNOWN_PLAYERS_FILE, "r") as f:
                saved = json.load(f)
                for x, n in saved.get("xuids", {}).items():
                    xuid_to_name[x] = n
                    name_to_xuid[n.lower()] = x
        except Exception:
            pass

    # Read server log for any new player connections
    updated = False
    if os.path.exists(SERVER_LOG_FILE):
        try:
            with open(SERVER_LOG_FILE, "r") as f:
                for line in f:
                    m = re.search(r"Player connected:\s*([^,]+),\s*xuid:\s*(\d+)", line)
                    if m:
                        name = m.group(1).strip()
                        xuid = m.group(2).strip()
                        if name.lower() not in name_to_xuid or name_to_xuid[name.lower()] != xuid:
                            updated = True
                        name_to_xuid[name.lower()] = xuid
                        xuid_to_name[xuid] = name
        except Exception:
            pass

    # Save to persistent storage
    if updated or (not os.path.exists(KNOWN_PLAYERS_FILE) and xuid_to_name):
        try:
            with open(KNOWN_PLAYERS_FILE, "w") as f:
                json.dump({"names": name_to_xuid, "xuids": xuid_to_name}, f, indent=3)
        except Exception:
            pass

    return name_to_xuid, xuid_to_name


def read_json_file(path):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def write_json_file(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=3)


def resolve_version_url(version_input):
    v = version_input.strip()
    try:
        req = urllib.request.Request(
            "https://raw.githubusercontent.com/kittizz/bedrock-server-downloads/refs/heads/main/bedrock-server-downloads.json",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        data = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
        release = data.get("release", {})
        if v in release:
            return release[v].get("linux", {}).get("url")
        for k, val in release.items():
            if k == v or k.startswith(v + ".") or v.startswith(k + "."):
                url = val.get("linux", {}).get("url")
                if url:
                    return url
    except Exception:
        pass

    test_urls = [
        f"https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{v}.zip",
        f"https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{v}.1.zip",
        f"https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{v}.01.zip",
        f"https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{v}.2.zip",
        f"https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{v}.02.zip",
        f"https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{v}.10.zip",
    ]
    for url in test_urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
            res = urllib.request.urlopen(req, timeout=4)
            if res.status == 200:
                return url
        except Exception:
            pass
    return None


def change_server_version_worker(target_version):
    global VERSION_SWITCH_STATE
    bds_dir = "/opt/bedrock-server"
    tmp_zip = f"/tmp/bds_dl_{secrets.token_hex(4)}.zip"

    def set_progress(status, step, progress, message="", version="", error=""):
        with VERSION_SWITCH_LOCK:
            VERSION_SWITCH_STATE["status"] = status
            VERSION_SWITCH_STATE["step"] = step
            VERSION_SWITCH_STATE["progress"] = progress
            VERSION_SWITCH_STATE["message"] = message
            VERSION_SWITCH_STATE["version"] = version
            VERSION_SWITCH_STATE["error"] = error

    try:
        set_progress("running", "🔍 Resolving Version", 5, f"Resolving download link for v{target_version}...")
        url = resolve_version_url(target_version)
        if not url:
            set_progress("error", "Version Not Found", 0, "", "", f"Version '{target_version}' could not be resolved. Please verify the version number.")
            return

        m = re.search(r"bedrock-server-([0-9\.]+)\.zip", url)
        final_v = m.group(1) if m else target_version

        set_progress("running", "⬇️ Downloading BDS Engine", 10, f"Connecting to official download server for v{final_v}...")

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_zip, "wb") as f_out:
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            last_pct = -1
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f_out.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = int((downloaded / total_size) * 100)
                    if pct != last_pct and (pct % 2 == 0 or pct == 100):
                        last_pct = pct
                        mb_dl = downloaded / (1024 * 1024)
                        mb_tot = total_size / (1024 * 1024)
                        mapped_pct = 10 + int(pct * 0.5)  # 10% -> 60%
                        set_progress("running", "⬇️ Downloading BDS Engine", mapped_pct, f"Downloading v{final_v}: {mb_dl:.1f} MB / {mb_tot:.1f} MB ({pct}%)")

        if not os.path.exists(tmp_zip) or not zipfile.is_zipfile(tmp_zip):
            set_progress("error", "Download Failed", 0, "", "", f"Failed to download a valid server archive for {final_v}.")
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
            return

        set_progress("running", "🛑 Stopping Previous Server", 65, "Safely shutting down running Bedrock process...")
        run_bash('screen -S bedrock -p 0 -X stuff "stop$(printf \'\\r\')"')
        for _ in range(5):
            time.sleep(1)
            out, _, _ = run_bash("ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
            if not out.strip():
                break
        run_bash('screen -S bedrock -X quit 2>/dev/null || true')
        run_bash('pkill -9 -x bedrock_server 2>/dev/null || true')

        set_progress("running", "📦 Extracting BDS Files", 75, f"Extracting clean BDS v{final_v} binary...")
        shutil.rmtree(bds_dir, ignore_errors=True)
        os.makedirs(bds_dir, exist_ok=True)
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            zf.extractall(bds_dir)
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)

        bds_bin = os.path.join(bds_dir, "bedrock_server")
        if os.path.exists(bds_bin):
            os.chmod(bds_bin, 0o755)

        set_progress("running", "🔗 Preserving World & Settings", 85, "Symlinking worlds, permissions, allowlist, and server properties...")

        # Ensure default persistent files exist in BEDROCK_DATA
        if not os.path.exists(PROPERTIES_FILE):
            if os.path.exists(os.path.join(bds_dir, "server.properties")):
                shutil.copy2(os.path.join(bds_dir, "server.properties"), PROPERTIES_FILE)
            elif os.path.exists(os.path.join(BASE_DIR, "config", "server.properties")):
                shutil.copy2(os.path.join(BASE_DIR, "config", "server.properties"), PROPERTIES_FILE)
            else:
                default_props = (
                    "server-name=Bedrock 24/7 Server\n"
                    "gamemode=survival\n"
                    "force-gamemode=false\n"
                    "difficulty=easy\n"
                    "allow-cheats=true\n"
                    "max-players=10\n"
                    "online-mode=false\n"
                    "white-list=false\n"
                    "server-port=19132\n"
                    "server-portv6=19133\n"
                    "view-distance=6\n"
                    "tick-distance=4\n"
                    "player-idle-timeout=30\n"
                    "max-threads=4\n"
                    "level-name=Bedrock level\n"
                    "level-seed=\n"
                    "default-player-permission-level=member\n"
                    "texturepack-required=false\n"
                    "content-log-file-enabled=false\n"
                    "compression-threshold=1\n"
                    "server-authoritative-movement=server-auth\n"
                    "player-movement-score-threshold=20\n"
                    "player-movement-distance-threshold=0.3\n"
                    "player-movement-duration-threshold-in-ms=500\n"
                )
                with open(PROPERTIES_FILE, "w") as f:
                    f.write(default_props)

        if not os.path.exists(ALLOWLIST_FILE):
            if os.path.exists(os.path.join(bds_dir, "allowlist.json")):
                shutil.copy2(os.path.join(bds_dir, "allowlist.json"), ALLOWLIST_FILE)
            else:
                with open(ALLOWLIST_FILE, "w") as f:
                    f.write("[]\n")

        if not os.path.exists(PERMISSIONS_FILE):
            if os.path.exists(os.path.join(bds_dir, "permissions.json")):
                shutil.copy2(os.path.join(bds_dir, "permissions.json"), PERMISSIONS_FILE)
            else:
                with open(PERMISSIONS_FILE, "w") as f:
                    f.write("[]\n")

        os.makedirs(WORLDS_DIR, exist_ok=True)

        # Relink persistent files
        run_bash(f'rm -f "{bds_dir}/server.properties" "{bds_dir}/allowlist.json" "{bds_dir}/permissions.json"')
        run_bash(f'rm -rf "{bds_dir}/worlds"')
        run_bash(f'ln -sf "{PROPERTIES_FILE}" "{bds_dir}/server.properties"')
        run_bash(f'ln -sf "{ALLOWLIST_FILE}" "{bds_dir}/allowlist.json"')
        run_bash(f'ln -sf "{PERMISSIONS_FILE}" "{bds_dir}/permissions.json"')
        run_bash(f'ln -sf "{WORLDS_DIR}" "{bds_dir}/worlds"')

        try:
            with open(VERSION_FILE, "w") as f:
                f.write(final_v)
        except Exception:
            pass

        if os.path.exists(SERVER_CONTROL_SCRIPT):
            run_bash(f'sed -i \'s/BDS_VERSION=".*"/BDS_VERSION="{final_v}"/g\' "{SERVER_CONTROL_SCRIPT}"')
        if os.path.exists(os.path.join(DATA_DIR, "server-control.sh")):
            run_bash(f'sed -i \'s/BDS_VERSION=".*"/BDS_VERSION="{final_v}"/g\' "{os.path.join(DATA_DIR, "server-control.sh")}"')

        set_progress("running", "🚀 Launching Bedrock Server", 92, f"Starting Bedrock Server v{final_v} in screen...")
        run_bash('screen -S bedrock -X quit 2>/dev/null || true')
        run_bash(f'screen -dmS bedrock bash -c "while true; do echo \\"[\\$(date)] Starting Bedrock Server...\\" | tee -a \\"{SERVER_LOG_FILE}\\"; cd /opt/bedrock-server && LD_LIBRARY_PATH=. ./bedrock_server 2>&1 | tee -a \\"{SERVER_LOG_FILE}\\"; sleep 5; done"')

        # Verify it started and didn't crash
        time.sleep(2)
        out_check, _, _ = run_bash("ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
        if not out_check.strip():
            log_snippet, _, _ = run_bash(f"tail -n 10 '{SERVER_LOG_FILE}' 2>/dev/null")
            set_progress("error", "Startup Failed", 0, "", "", f"Bedrock Server crashed on startup. Recent log: {log_snippet.strip()}")
            return

        set_progress("completed", "✅ Update Complete!", 100, f"Successfully switched to Bedrock version {final_v}! Server is active.", final_v)
    except Exception as e:
        if os.path.exists(tmp_zip):
            try:
                os.remove(tmp_zip)
            except Exception:
                pass
        run_bash('screen -S bedrock -X quit 2>/dev/null || true')
        run_bash(f'screen -dmS bedrock bash -c "while true; do echo \\"[\\$(date)] Starting Bedrock Server...\\" | tee -a \\"{SERVER_LOG_FILE}\\"; cd /opt/bedrock-server && LD_LIBRARY_PATH=. ./bedrock_server 2>&1 | tee -a \\"{SERVER_LOG_FILE}\\"; sleep 5; done"')
        set_progress("error", "Update Failed", 0, "", "", str(e))


def change_server_version(target_version):
    change_server_version_worker(target_version)
    with VERSION_SWITCH_LOCK:
        if VERSION_SWITCH_STATE.get("status") == "completed":
            return True, VERSION_SWITCH_STATE.get("message", ""), VERSION_SWITCH_STATE.get("version", target_version)
        else:
            return False, VERSION_SWITCH_STATE.get("error") or VERSION_SWITCH_STATE.get("message", "Switch failed"), ""


# ==================== AUTH ROUTES ====================

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == get_admin_password():
            session["logged_in"] = True
            return redirect(url_for("index"))
        else:
            error = "Invalid password. Default is: admin123"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not is_authenticated():
        return redirect(url_for("login"))
    return render_template("index.html")


# ==================== METRICS & STATUS API ====================


@app.route("/api/status")
def api_status():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    is_running = False
    bds_pid = None
    mem_mb = 0
    cpu_pct = 0.0
    uptime = "Stopped"

    # Fetch BDS metrics in a single lightweight command (skipping zombie processes)
    out_bds, _, code = run_bash("ps -C bedrock_server -o pid=,stat=,rss=,%cpu=,etime= 2>/dev/null | awk '$2 !~ /Z/ {print $1, $3, $4, $5; exit}'")
    parts = out_bds.strip().split()
    if code == 0 and len(parts) >= 4:
        is_running = True
        bds_pid = parts[0]
        try:
            mem_mb = int(parts[1]) // 1024
        except Exception:
            mem_mb = 0
        try:
            cpu_pct = float(parts[2])
        except Exception:
            cpu_pct = 0.0
        uptime = parts[3]

    out_playit, _, _ = run_bash("ps -C playitd -o pid=,stat= 2>/dev/null | awk '$2 !~ /Z/ {print $1; exit}'")
    playit_running = bool(out_playit.strip())

    try:
        usage = shutil.disk_usage(DATA_DIR)
        disk_out = f"{usage.used / (1024**3):.1f}G / {usage.total / (1024**3):.1f}G ({int(usage.used / usage.total * 100)}%)"
    except Exception:
        disk_out = "N/A"

    players, max_p = get_online_players()

    return jsonify({
        "running": is_running,
        "pid": bds_pid,
        "memory_mb": mem_mb,
        "memory_max": 1024,
        "memory_pct": round((mem_mb / 1024) * 100, 1),
        "cpu_pct": cpu_pct,
        "uptime": uptime,
        "playit_running": playit_running,
        "tunnel_address": f"{TUNNEL_DOMAIN}:{TUNNEL_PORT}",
        "tunnel_domain": TUNNEL_DOMAIN,
        "tunnel_ip": TUNNEL_IP,
        "tunnel_port": TUNNEL_PORT,
        "disk_usage": disk_out or "N/A",
        "world_size": get_world_size(),
        "online_players": players,
        "online_count": len(players),
        "max_players": max_p,
        "version": get_current_bds_version()
    })


@app.route("/api/logs")
def api_logs():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    source = request.args.get("source", "bedrock")
    target_file = PLAYIT_LOG_FILE if source == "playit" else SERVER_LOG_FILE

    if not os.path.exists(target_file):
        return jsonify({"logs": []})

    try:
        with open(target_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.rstrip("\r\n") for l in f.readlines()[-250:]]

        if source == "bedrock":
            filtered = [
                l for l in lines 
                if not re.search(r"There are \d+/\d+ players online:", l) 
                and l.strip() != "list"
            ]
            return jsonify({"logs": filtered[-100:]})
        return jsonify({"logs": lines[-100:]})
    except Exception as e:
        return jsonify({"logs": [f"Error reading logs: {e}"]})


@app.route("/api/command", methods=["POST"])
def api_command():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    cmd = data.get("command", "").strip()
    if not cmd:
        return jsonify({"error": "Empty command"}), 400

    clean_cmd = cmd.replace('"', '\\"')
    run_bash(f'screen -S bedrock -p 0 -X stuff "{clean_cmd}$(printf \'\\r\')"')
    return jsonify({"status": "success", "command": cmd})


@app.route("/api/action", methods=["POST"])
def api_action():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    action = data.get("action", "")

    if action == "start":
        run_bash(f'bash "{SERVER_CONTROL_SCRIPT}" start >/dev/null 2>&1 || true')
        out_bds, _, _ = run_bash("ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
        if not out_bds.strip():
            run_bash('screen -S bedrock -X quit 2>/dev/null || true')
            run_bash(f'screen -dmS bedrock bash -c "while true; do echo \\"[\\$(date)] Starting Bedrock Server...\\" >> \\"{SERVER_LOG_FILE}\\" 2>&1; cd /opt/bedrock-server && LD_LIBRARY_PATH=. ./bedrock_server 2>&1 >> \\"{SERVER_LOG_FILE}\\"; sleep 5; done"')
        return jsonify({"status": "started"})
    elif action == "stop":
        run_bash('screen -S bedrock -p 0 -X stuff "stop$(printf \'\\r\')"')
        run_bash('sleep 2; screen -S bedrock -X quit 2>/dev/null; pkill -9 -x bedrock_server 2>/dev/null || true')
        return jsonify({"status": "stopped"})
    elif action == "restart":
        run_bash('screen -S bedrock -p 0 -X stuff "stop$(printf \'\\r\')"')
        run_bash('sleep 2; screen -S bedrock -X quit 2>/dev/null; pkill -9 -x bedrock_server 2>/dev/null || true')
        run_bash(f'bash "{SERVER_CONTROL_SCRIPT}" start >/dev/null 2>&1 || true')
        out_bds, _, _ = run_bash("ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
        if not out_bds.strip():
            run_bash(f'screen -dmS bedrock bash -c "while true; do echo \\"[\\$(date)] Starting Bedrock Server...\\" >> \\"{SERVER_LOG_FILE}\\" 2>&1; cd /opt/bedrock-server && LD_LIBRARY_PATH=. ./bedrock_server 2>&1 >> \\"{SERVER_LOG_FILE}\\"; sleep 5; done"')
        return jsonify({"status": "restarted"})
    elif action == "restart_playit":
        run_bash('screen -S playit -X quit 2>/dev/null || true')
        run_bash('pkill -9 -x playitd 2>/dev/null || true')
        playit_toml = os.path.join(PLAYIT_DIR, "playit.toml")
        run_bash(f'screen -dmS playit bash -c "while true; do echo \\"[\\$(date)] Starting Playit tunnel...\\" >> \\"{PLAYIT_LOG_FILE}\\" 2>&1; playitd --secret_path \\"{playit_toml}\\" >> \\"{PLAYIT_LOG_FILE}\\" 2>&1; sleep 5; done"')
        return jsonify({"status": "playit_restarted"})
    else:
        return jsonify({"error": "Invalid action"}), 400


# ==================== VERSION SWITCHER API ====================

@app.route("/api/version/info")
def api_version_info():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify({
        "current_version": get_current_bds_version(),
        "tunnel_domain": TUNNEL_DOMAIN,
        "tunnel_ip": TUNNEL_IP,
        "tunnel_port": TUNNEL_PORT,
        "popular_versions": [
            {"version": "1.26.45.1", "label": "1.26.45.1 (Latest)"},
            {"version": "1.26.2.1", "label": "1.26.2 (Stable)"},
            {"version": "1.26.0.2", "label": "1.26.0 (Stable)"},
            {"version": "1.21.50.10", "label": "1.21.50 (Legacy)"},
            {"version": "1.21.31.04", "label": "1.21.31 (Legacy)"}
        ]
    })


@app.route("/api/version/progress")
def api_version_progress():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    with VERSION_SWITCH_LOCK:
        return jsonify(dict(VERSION_SWITCH_STATE))


@app.route("/api/version/switch", methods=["POST"])
def api_version_switch():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    target_version = data.get("version", "").strip()
    if not target_version:
        return jsonify({"error": "Please enter a valid version number!"}), 400

    if not re.match(r"^[0-9\.\-]+$", target_version):
        return jsonify({"error": "Invalid version format. Example: 1.26.2 or 1.26.45.1"}), 400

    with VERSION_SWITCH_LOCK:
        if VERSION_SWITCH_STATE.get("status") == "running":
            return jsonify({"error": "A version update is already in progress. Please wait."}), 409

    t = threading.Thread(target=change_server_version_worker, args=(target_version,), daemon=True)
    t.start()
    return jsonify({
        "status": "started",
        "message": f"Version switch to v{target_version} started.",
        "target_version": target_version
    })


# ==================== PLAYER MANAGEMENT API ====================

@app.route("/api/players")
def api_players():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    online_players, max_p = get_online_players()
    whitelist = read_json_file(ALLOWLIST_FILE)
    raw_perms = read_json_file(PERMISSIONS_FILE)

    name_to_xuid, xuid_to_name = get_xuid_map()

    ops = []
    for p in raw_perms:
        if p.get("permission") == "operator":
            x = p.get("xuid", "")
            name = xuid_to_name.get(x, f"Player (XUID: {x})")
            ops.append({"name": name, "xuid": x})

    allowlist_enabled = False
    if os.path.exists(PROPERTIES_FILE):
        with open(PROPERTIES_FILE, "r") as f:
            for line in f:
                if line.strip().startswith("allow-list="):
                    allowlist_enabled = line.strip().split("=")[1].lower() == "true"

    return jsonify({
        "online": online_players,
        "whitelist": whitelist,
        "operators": ops,
        "name_to_xuid": name_to_xuid,
        "allowlist_enabled": allowlist_enabled,
        "max_players": max_p
    })


@app.route("/api/players/action", methods=["POST"])
def api_player_action():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    player = data.get("player", "").strip()
    action = data.get("action", "").strip()
    req_xuid = data.get("xuid", "").strip()

    if not player and not req_xuid:
        return jsonify({"error": "Missing player name or XUID"}), 400

    name_to_xuid, xuid_to_name = get_xuid_map()
    xuid = req_xuid or name_to_xuid.get(player.lower())
    if not xuid and player.isdigit():
        xuid = player

    display_name = xuid_to_name.get(xuid, player) if xuid else player
    safe_player = display_name.replace('"', '\\"')

    if action == "op":
        # 1. Send console command
        run_bash(f'screen -S bedrock -p 0 -X stuff "op \\"{safe_player}\\"$(printf \'\\r\')"')

        # 2. Update permissions.json directly (works online and offline!)
        perms = read_json_file(PERMISSIONS_FILE)
        if xuid:
            if not any(p.get("xuid") == xuid for p in perms):
                perms.append({"permission": "operator", "xuid": xuid})
                write_json_file(PERMISSIONS_FILE, perms)
            run_bash('screen -S bedrock -p 0 -X stuff "permission reload$(printf \'\\r\')"')
            return jsonify({
                "status": "success",
                "message": f"Successfully granted Admin (OP) to '{display_name}'! (XUID: {xuid})"
            })
        else:
            return jsonify({
                "status": "warning",
                "message": f"Command sent: 'op {player}'. Note: Player has never joined before. Once they connect, their XUID will be registered and saved permanently as OP!"
            })

    elif action == "deop":
        run_bash(f'screen -S bedrock -p 0 -X stuff "deop \\"{safe_player}\\"$(printf \'\\r\')"')
        perms = read_json_file(PERMISSIONS_FILE)
        target_xuid = xuid or player
        new_perms = []
        for p in perms:
            p_xuid = p.get("xuid", "")
            p_name = xuid_to_name.get(p_xuid, "").lower()
            if p_xuid == target_xuid or (p_name and p_name == player.lower()):
                continue
            new_perms.append(p)
        write_json_file(PERMISSIONS_FILE, new_perms)
        run_bash('screen -S bedrock -p 0 -X stuff "permission reload$(printf \'\\r\')"')
        return jsonify({"status": "success", "message": f"Successfully removed Admin (OP) from '{display_name}'."})

    elif action == "kick":
        reason = data.get("reason", "Kicked by Administrator").replace('"', '\\"')
        run_bash(f'screen -S bedrock -p 0 -X stuff "kick \\"{safe_player}\\" \\"{reason}\\"$(printf \'\\r\')"')
        return jsonify({"status": "success", "message": f"Kicked player '{display_name}'."})

    elif action == "gamemode":
        mode = data.get("mode", "survival")
        run_bash(f'screen -S bedrock -p 0 -X stuff "gamemode {mode} \\"{safe_player}\\"$(printf \'\\r\')"')
        return jsonify({"status": "success", "message": f"Set gamemode of '{display_name}' to {mode}."})

    elif action == "tp":
        target = data.get("target", "~ ~ ~")
        run_bash(f'screen -S bedrock -p 0 -X stuff "tp \\"{safe_player}\\" {target}$(printf \'\\r\')"')
        return jsonify({"status": "success", "message": f"Teleported '{display_name}'."})

    return jsonify({"error": "Unknown player action"}), 400


@app.route("/api/whitelist/add", methods=["POST"])
def api_whitelist_add():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    player = data.get("player", "").strip()
    if not player:
        return jsonify({"error": "Missing player name"}), 400

    safe_player = player.replace('"', '\\"')
    run_bash(f'screen -S bedrock -p 0 -X stuff "allowlist add \\"{safe_player}\\"$(printf \'\\r\')"')
    wl = read_json_file(ALLOWLIST_FILE)
    if not any(entry.get("name", "").lower() == player.lower() for entry in wl):
        wl.append({"ignoresPlayerLimit": False, "name": player, "xuid": ""})
        write_json_file(ALLOWLIST_FILE, wl)

    return jsonify({"status": "success", "message": f"Added '{player}' to whitelist."})


@app.route("/api/whitelist/remove", methods=["POST"])
def api_whitelist_remove():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    player = data.get("player", "").strip()
    if not player:
        return jsonify({"error": "Missing player name"}), 400

    safe_player = player.replace('"', '\\"')
    run_bash(f'screen -S bedrock -p 0 -X stuff "allowlist remove \\"{safe_player}\\"$(printf \'\\r\')"')
    wl = read_json_file(ALLOWLIST_FILE)
    wl = [e for e in wl if e.get("name", "").lower() != player.lower()]
    write_json_file(ALLOWLIST_FILE, wl)

    return jsonify({"status": "success", "message": f"Removed '{player}' from whitelist."})


@app.route("/api/whitelist/toggle", methods=["POST"])
def api_whitelist_toggle():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    enable = data.get("enable", False)
    val = "true" if enable else "false"
    run_bash(f"sed -i 's/^allow-list=.*/allow-list={val}/' {PROPERTIES_FILE}")
    cmd = "allowlist on" if enable else "allowlist off"
    run_bash(f'screen -S bedrock -p 0 -X stuff "{cmd}$(printf \'\\r\')"')
    return jsonify({"status": "success", "message": f"Whitelist {'enabled' if enable else 'disabled'}."})


# ==================== WORLD IMPORT & EXPORT API ====================

@app.route("/api/world/export")
def api_world_export():
    if not is_authenticated():
        return redirect(url_for("login"))

    run_bash('screen -S bedrock -p 0 -X stuff "save hold$(printf \'\\r\')"')
    import time
    time.sleep(1)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    zip_filename = f"Bedrock_World_{now_str}.mcworld"

    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(ACTIVE_WORLD_DIR):
            for f in files:
                fp = os.path.join(root, f)
                if not os.path.islink(fp):
                    arcname = os.path.relpath(fp, ACTIVE_WORLD_DIR)
                    zf.write(fp, arcname)

    run_bash('screen -S bedrock -p 0 -X stuff "save resume$(printf \'\\r\')"')
    bio.seek(0)

    return send_file(
        bio,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=zip_filename
    )


@app.route("/api/world/import", methods=["POST"])
def api_world_import():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    if "world_file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["world_file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    filename = secure_filename(file.filename)
    if not (filename.endswith(".zip") or filename.endswith(".mcworld")):
        return jsonify({"error": "File must be a .zip or .mcworld file"}), 400

    upload_temp_path = os.path.join("/tmp", f"upload_{filename}")
    file.save(upload_temp_path)

    if not zipfile.is_zipfile(upload_temp_path):
        os.remove(upload_temp_path)
        return jsonify({"error": "Uploaded file is not a valid zip or .mcworld archive"}), 400

    try:
        run_bash('screen -S bedrock -p 0 -X stuff "stop$(printf \'\\r\')"')
        import time
        for _ in range(8):
            time.sleep(1)
            out, _, _ = run_bash("ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
            if not out.strip():
                break

        if os.path.exists(ACTIVE_WORLD_DIR):
            backup_name = f"backup_before_import_{int(time.time())}"
            backup_dest = os.path.join(BACKUP_DIR, backup_name)
            shutil.copytree(ACTIVE_WORLD_DIR, backup_dest)

        staging_dir = os.path.join("/tmp", "import_staging")
        shutil.rmtree(staging_dir, ignore_errors=True)
        os.makedirs(staging_dir, exist_ok=True)

        with zipfile.ZipFile(upload_temp_path, "r") as zf:
            zf.extractall(staging_dir)

        target_root = staging_dir
        if not os.path.exists(os.path.join(staging_dir, "level.dat")):
            for root, dirs, files in os.walk(staging_dir):
                if "level.dat" in files:
                    target_root = root
                    break

        if not os.path.exists(os.path.join(target_root, "level.dat")):
            shutil.rmtree(staging_dir, ignore_errors=True)
            os.remove(upload_temp_path)
            return jsonify({"error": "Invalid world archive: level.dat not found inside"}), 400

        shutil.rmtree(ACTIVE_WORLD_DIR, ignore_errors=True)
        os.makedirs(ACTIVE_WORLD_DIR, exist_ok=True)
        for item in os.listdir(target_root):
            s = os.path.join(target_root, item)
            d = os.path.join(ACTIVE_WORLD_DIR, item)
            if os.path.isdir(s):
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)

        shutil.rmtree(staging_dir, ignore_errors=True)
        os.remove(upload_temp_path)

        return jsonify({"status": "success", "message": "World imported successfully! The server is loading the new world."})

    except Exception as e:
        return jsonify({"error": f"Import failed: {str(e)}"}), 500



# ==================== GITHUB CLOUD BACKUP & RESTORE ====================

def run_github_backup(trigger="manual"):
    """
    Safely creates a world archive and pushes all configs & world data
    to the GitHub repository on branch 'world-backup'.
    """
    gh_token = get_github_token()
    if not gh_token or not GITHUB_REPO:
        return False, "GitHub Token or Repository not configured", {}

    staging_dir = f"/tmp/gh_backup_{secrets.token_hex(4)}"
    world_archive = os.path.join("/tmp", f"Bedrock_World_{secrets.token_hex(4)}.mcworld")
    try:
        # 1. Instruct Bedrock server to hold saves so memory buffers write to disk
        run_bash('screen -S bedrock -p 0 -X stuff "save hold$(printf \'\\r\')"')
        time.sleep(1)

        # 2. Package world into .mcworld
        total_bytes = 0
        with zipfile.ZipFile(world_archive, "w", zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(ACTIVE_WORLD_DIR):
                for dirpath, dirnames, filenames in os.walk(ACTIVE_WORLD_DIR):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        if not os.path.islink(fp):
                            arcname = os.path.relpath(fp, ACTIVE_WORLD_DIR)
                            zf.write(fp, arcname)
                            total_bytes += os.path.getsize(fp)

        # 3. Resume Bedrock server save
        run_bash('screen -S bedrock -p 0 -X stuff "save resume$(printf \'\\r\')"')

        # 4. Clone or init world-backup branch in staging_dir
        os.makedirs(staging_dir, exist_ok=True)
        clone_url = f"https://batz-dev:{gh_token}@github.com/{GITHUB_REPO}.git"
        cmd_clone = f"git clone --depth 1 --branch {GITHUB_BACKUP_BRANCH} '{clone_url}' '{staging_dir}' 2>&1"
        out_clone, err_clone, code_clone = run_bash(cmd_clone)
        if code_clone != 0:
            run_bash(f"cd '{staging_dir}' && git init && git checkout -b {GITHUB_BACKUP_BRANCH} && git remote add origin '{clone_url}'")

        # 5. Populate staging files
        os.makedirs(os.path.join(staging_dir, "worlds"), exist_ok=True)
        os.makedirs(os.path.join(staging_dir, "configs"), exist_ok=True)
        os.makedirs(os.path.join(staging_dir, "dashboard"), exist_ok=True)
        os.makedirs(os.path.join(staging_dir, "tunnel"), exist_ok=True)

        target_mcworld = os.path.join(staging_dir, "worlds", "Bedrock_World_Latest.mcworld")
        if total_bytes < 100 * 1024 and os.path.exists(target_mcworld) and os.path.getsize(target_mcworld) > 1024 * 1024:
            print("[!] Safeguard: Local world is empty. Preserving existing large GitHub world backup.")
        else:
            shutil.copy2(world_archive, target_mcworld)
        if os.path.exists(world_archive):
            os.remove(world_archive)

        # Server configs & version
        for cfg_file in [PROPERTIES_FILE, PERMISSIONS_FILE, ALLOWLIST_FILE, KNOWN_PLAYERS_FILE, VERSION_FILE]:
            if os.path.exists(cfg_file):
                shutil.copy2(cfg_file, os.path.join(staging_dir, "configs", os.path.basename(cfg_file)))

        # Web Dashboard settings (password, session key)
        for dash_file in [PASSWORD_FILE, SECRET_KEY_FILE]:
            if os.path.exists(dash_file):
                shutil.copy2(dash_file, os.path.join(staging_dir, "dashboard", os.path.basename(dash_file)))

        # Permanent Tunnel configs
        playit_cfg = os.path.join(PLAYIT_DIR, "playit.toml")
        if os.path.exists(playit_cfg):
            shutil.copy2(playit_cfg, os.path.join(staging_dir, "tunnel", "playit.toml"))

        arc_size_mb = os.path.getsize(target_mcworld) / (1024 * 1024)
        size_str = f"{arc_size_mb:.2f} MB"
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_ist = now_utc + datetime.timedelta(hours=5, minutes=30)
        time_utc_str = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
        time_ist_str = now_ist.strftime("%Y-%m-%d %I:%M:%S %p IST")

        backup_meta = {
            "backup_time": time_utc_str,
            "backup_time_ist": time_ist_str,
            "world_name": "Bedrock level",
            "archive_name": "Bedrock_World_Latest.mcworld",
            "size": size_str,
            "status": "success",
            "trigger": trigger,
            "version": get_current_bds_version()
        }

        with open(os.path.join(staging_dir, "backup_info.json"), "w") as f:
            json.dump(backup_meta, f, indent=2)

        readme_content = (
            f"# ⛏️ Minecraft Bedrock Cloud Backup ({GITHUB_BACKUP_BRANCH})\n\n"
            f"Automated backup of Minecraft Bedrock Dedicated Server.\n\n"
            f"- **Latest Backup Time (IST):** `{time_ist_str}`\n"
            f"- **Latest Backup Time (UTC):** `{time_utc_str}`\n"
            f"- **Archive Size:** `{size_str}`\n"
            f"- **Trigger:** `{trigger}`\n"
            f"- **Engine Version:** `{backup_meta['version']}`\n\n"
            f"### Restoring:\n"
            f"You can restore this backup with 1 click from your Web Management Dashboard, or download `worlds/Bedrock_World_Latest.mcworld` to open in Minecraft directly.\n"
        )
        with open(os.path.join(staging_dir, "README.md"), "w") as f:
            f.write(readme_content)

        # 6. Git commit & push
        push_cmds = (
            f"cd '{staging_dir}' && "
            f"git config user.name 'Minecraft Backup Bot' && "
            f"git config user.email 'bot@minecraft-bedrock-247' && "
            f"git add -A && "
            f"git commit -m 'Automated Backup: {time_ist_str} [{trigger}]' && "
            f"git push -u origin {GITHUB_BACKUP_BRANCH} --force"
        )
        out_p, err_p, code_p = run_bash(push_cmds)

        shutil.rmtree(staging_dir, ignore_errors=True)

        if code_p != 0:
            return False, f"Git push failed: {err_p or out_p}", {}

        try:
            with open(LAST_BACKUP_INFO_FILE, "w") as f:
                json.dump(backup_meta, f, indent=2)
        except Exception:
            pass

        return True, f"Successfully backed up world and configs to GitHub at {time_ist_str}!", backup_meta

    except Exception as e:
        if os.path.exists(world_archive):
            try:
                os.remove(world_archive)
            except Exception:
                pass
        shutil.rmtree(staging_dir, ignore_errors=True)
        run_bash('screen -S bedrock -p 0 -X stuff "save resume$(printf \'\\r\')"')
        return False, f"Backup error: {str(e)}", {}


def run_github_restore():
    """
    Fetches the latest backup from the GitHub 'world-backup' branch,
    safely replaces active world and configs, and restarts the server.
    """
    gh_token = get_github_token()
    if not gh_token or not GITHUB_REPO:
        return False, "GitHub Token or Repository not configured"

    staging_dir = f"/tmp/gh_restore_{secrets.token_hex(4)}"
    try:
        os.makedirs(staging_dir, exist_ok=True)
        clone_url = f"https://batz-dev:{gh_token}@github.com/{GITHUB_REPO}.git"
        cmd_clone = f"git clone --depth 1 --branch {GITHUB_BACKUP_BRANCH} '{clone_url}' '{staging_dir}' 2>&1"
        out_clone, err_clone, code_clone = run_bash(cmd_clone)
        if code_clone != 0:
            shutil.rmtree(staging_dir, ignore_errors=True)
            return False, f"Failed to fetch world-backup branch from GitHub: {err_clone or out_clone}"

        mcworld_file = os.path.join(staging_dir, "worlds", "Bedrock_World_Latest.mcworld")
        if not os.path.exists(mcworld_file) or not zipfile.is_zipfile(mcworld_file):
            shutil.rmtree(staging_dir, ignore_errors=True)
            return False, "Valid Bedrock_World_Latest.mcworld not found in GitHub backup branch."

        # Stop Bedrock cleanly
        run_bash('screen -S bedrock -p 0 -X stuff "stop$(printf \'\\r\')"')
        for _ in range(6):
            time.sleep(1)
            out, _, _ = run_bash("ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
            if not out.strip():
                break
        run_bash('screen -S bedrock -X quit 2>/dev/null || true')
        run_bash('pkill -9 -x bedrock_server 2>/dev/null || true')

        # Safety snapshot of current world
        if os.path.exists(ACTIVE_WORLD_DIR):
            safety_backup = os.path.join(BACKUP_DIR, f"pre_restore_{int(time.time())}")
            try:
                shutil.copytree(ACTIVE_WORLD_DIR, safety_backup)
            except Exception:
                pass

        # Extract mcworld
        shutil.rmtree(ACTIVE_WORLD_DIR, ignore_errors=True)
        os.makedirs(ACTIVE_WORLD_DIR, exist_ok=True)
        with zipfile.ZipFile(mcworld_file, "r") as zf:
            zf.extractall(ACTIVE_WORLD_DIR)

        # Restore server configs & version
        backup_cfg_dir = os.path.join(staging_dir, "configs")
        if os.path.exists(backup_cfg_dir):
            for f in os.listdir(backup_cfg_dir):
                src = os.path.join(backup_cfg_dir, f)
                if f == "version.txt":
                    shutil.copy2(src, VERSION_FILE)
                else:
                    dst = os.path.join(BEDROCK_DATA, f)
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)

        # Restore dashboard settings (password & secret key)
        backup_dash_dir = os.path.join(staging_dir, "dashboard")
        if os.path.exists(backup_dash_dir):
            for f in os.listdir(backup_dash_dir):
                src = os.path.join(backup_dash_dir, f)
                dst = os.path.join(DASHBOARD_DIR, f)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)

        # Restore tunnel configs
        backup_tunnel_dir = os.path.join(staging_dir, "tunnel")
        if os.path.exists(backup_tunnel_dir):
            for f in os.listdir(backup_tunnel_dir):
                src = os.path.join(backup_tunnel_dir, f)
                dst = os.path.join(PLAYIT_DIR, f)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)

        # Relink symlinks
        bds_dir = "/opt/bedrock-server"
        if os.path.exists(bds_dir):
            run_bash(f'rm -f "{bds_dir}/server.properties" "{bds_dir}/allowlist.json" "{bds_dir}/permissions.json"')
            run_bash(f'rm -rf "{bds_dir}/worlds"')
            run_bash(f'ln -sf "{PROPERTIES_FILE}" "{bds_dir}/server.properties"')
            run_bash(f'ln -sf "{ALLOWLIST_FILE}" "{bds_dir}/allowlist.json"')
            run_bash(f'ln -sf "{PERMISSIONS_FILE}" "{bds_dir}/permissions.json"')
            run_bash(f'ln -sf "{WORLDS_DIR}" "{bds_dir}/worlds"')

        shutil.rmtree(staging_dir, ignore_errors=True)

        # Start server in screen
        run_bash('screen -S bedrock -X quit 2>/dev/null || true')
        run_bash(f'screen -dmS bedrock bash -c "while true; do echo \\"[\\$(date)] Starting Bedrock Server...\\" | tee -a \\"{SERVER_LOG_FILE}\\"; cd /opt/bedrock-server && LD_LIBRARY_PATH=. ./bedrock_server 2>&1 | tee -a \\"{SERVER_LOG_FILE}\\"; sleep 5; done"')

        return True, "World and configurations successfully restored from GitHub! Server is restarting with the restored world."

    except Exception as e:
        shutil.rmtree(staging_dir, ignore_errors=True)
        run_bash('screen -S bedrock -X quit 2>/dev/null || true')
        run_bash(f'screen -dmS bedrock bash -c "while true; do echo \\"[\\$(date)] Starting Bedrock Server...\\" | tee -a \\"{SERVER_LOG_FILE}\\"; cd /opt/bedrock-server && LD_LIBRARY_PATH=. ./bedrock_server 2>&1 | tee -a \\"{SERVER_LOG_FILE}\\"; sleep 5; done"')
        return False, f"Restore failed: {str(e)}"


def backup_scheduler_worker():
    last_run_date = ""
    while True:
        try:
            # India Standard Time (UTC + 5:30)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            now_ist = now_utc + datetime.timedelta(hours=5, minutes=30)
            today_str = now_ist.strftime("%Y-%m-%d")

            # Check if midnight (00:00 - 00:05) IST
            if now_ist.hour == 0 and now_ist.minute < 5 and last_run_date != today_str:
                last_run_date = today_str
                print(f"[*] Midnight 12:00 AM IST reached ({today_str}). Running automated GitHub backup...")
                success, msg, meta = run_github_backup(trigger="daily_midnight_ist")
                print(f"[*] Automated Midnight Backup Result: {success} - {msg}")
        except Exception as e:
            print(f"[!] Midnight Backup Scheduler Error: {e}")
        time.sleep(30)


@app.route("/api/github/backup/info")
def api_github_backup_info():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = None
    if os.path.exists(LAST_BACKUP_INFO_FILE):
        try:
            with open(LAST_BACKUP_INFO_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            pass

    if not data:
        try:
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BACKUP_BRANCH}/backup_info.json"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            data = {
                "backup_time": "No backup found",
                "backup_time_ist": "No backup found",
                "size": "N/A",
                "status": "idle"
            }

    return jsonify({
        "repo": GITHUB_REPO,
        "branch": GITHUB_BACKUP_BRANCH,
        "last_backup": data
    })


@app.route("/api/github/backup", methods=["POST"])
def api_github_backup_now():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    success, msg, meta = run_github_backup(trigger="manual_web_click")
    if success:
        return jsonify({"status": "success", "message": msg, "info": meta})
    else:
        return jsonify({"error": msg}), 500


@app.route("/api/github/restore", methods=["POST"])
def api_github_restore_now():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    success, msg = run_github_restore()
    if success:
        return jsonify({"status": "success", "message": msg})
    else:
        return jsonify({"error": msg}), 500


# ==================== ADVANCED IN-GAME MANAGEMENT API ====================

@app.route("/api/broadcast", methods=["POST"])
def api_broadcast():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    msg = data.get("message", "").strip()
    if not msg:
        return jsonify({"error": "Please enter a message to broadcast!"}), 400

    clean_msg = msg.replace('"', '\\"').replace("'", "")
    run_bash(f'screen -S bedrock -p 0 -X stuff "titleraw @a title {{\\"rawtext\\":[{{\\"text\\":\\"§e§l[ANNOUNCEMENT]§r §f{clean_msg}\\"}}]}}$(printf \'\\r\')"')
    run_bash(f'screen -S bedrock -p 0 -X stuff "say §e§l[ANNOUNCEMENT]§r §f{clean_msg}$(printf \'\\r\')"')

    return jsonify({"status": "success", "message": f"Broadcast sent: '{msg}'"})

@app.route("/api/gamerule", methods=["POST"])
def api_gamerule():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    rule = data.get("rule", "").strip().lower()
    value = str(data.get("value", "")).strip().lower()

    allowed_rules = {
        "keepinventory": "Keep Inventory on Death",
        "mobgriefing": "Mob & Creeper Griefing",
        "showcoordinates": "Show XYZ Coordinates",
        "pvp": "Player vs Player (PvP) Combat",
        "dodaylightcycle": "Day/Night Cycle",
        "dofiretick": "Fire Spread Damage",
        "naturalregeneration": "Natural Health Regeneration",
        "doimmediaterespawn": "Immediate Respawn (No Death Screen)",
        "tntexplodes": "TNT Explosion Damage",
        "showdeathmessages": "Show Death Messages in Chat"
    }

    if rule not in allowed_rules:
        return jsonify({"error": f"Invalid gamerule: {rule}"}), 400

    if value not in ["true", "false"]:
        return jsonify({"error": "Value must be true or false"}), 400

    run_bash(f'screen -S bedrock -p 0 -X stuff "gamerule {rule} {value}$(printf \'\\r\')"')
    rule_name = allowed_rules[rule]
    state_str = "ENABLED" if value == "true" else "DISABLED"
    return jsonify({
        "status": "success",
        "message": f"Game Rule '{rule_name}' is now {state_str}!"
    })


@app.route("/api/quick-action", methods=["POST"])
def api_quick_action():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    action_type = data.get("type", "").strip()
    target = data.get("target", "@a").strip()
    if target.startswith("@"):
        cmd_target = target
    else:
        safe_player = target.replace('"', '\\"')
        cmd_target = f'"{safe_player}"'

    if action_type == "buff":
        buff = data.get("buff", "").strip().lower()
        if buff == "night_vision":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect {cmd_target} night_vision 99999 1 false$(printf \'\\r\')"')
            run_bash(f'screen -S bedrock -p 0 -X stuff "titleraw {cmd_target} actionbar {{\\\"rawtext\\\":[{{\\\"text\\\":\\\"§b§l👁️ Permanent Night Vision Active!\\\"}}]}}$(printf \'\\r\')"')
            msg = f"Granted permanent Night Vision to '{target}'!"
        elif buff == "speed":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect {cmd_target} speed 99999 2 false$(printf \'\\r\')"')
            run_bash(f'screen -S bedrock -p 0 -X stuff "titleraw {cmd_target} actionbar {{\\\"rawtext\\\":[{{\\\"text\\\":\\\"§e§l⚡ Super Speed II Active!\\\"}}]}}$(printf \'\\r\')"')
            msg = f"Granted Speed II buff to '{target}'!"
        elif buff == "saturation":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect {cmd_target} saturation 99999 1 false$(printf \'\\r\')"')
            run_bash(f'screen -S bedrock -p 0 -X stuff "titleraw {cmd_target} actionbar {{\\\"rawtext\\\":[{{\\\"text\\\":\\\"§6§l🍖 Infinite Saturation (Never Hungry)!\\\"}}]}}$(printf \'\\r\')"')
            msg = f"Granted Infinite Hunger/Saturation to '{target}'!"
        elif buff == "strength":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect {cmd_target} strength 99999 100 false$(printf \'\\r\')"')
            run_bash(f'screen -S bedrock -p 0 -X stuff "titleraw {cmd_target} actionbar {{\\\"rawtext\\\":[{{\\\"text\\\":\\\"§c§l⚔️ SUPER STRENGTH ACTIVATED! (1-Hit Kill)\\\"}}]}}$(printf \'\\r\')"')
            msg = f"Granted Super Strength (1-Hit Kill) to '{target}'!"
        elif buff == "regeneration":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect {cmd_target} regeneration 99999 5 false$(printf \'\\r\')"')
            run_bash(f'screen -S bedrock -p 0 -X stuff "titleraw {cmd_target} actionbar {{\\\"rawtext\\\":[{{\\\"text\\\":\\\"§d§l💖 Rapid Regeneration Active!\\\"}}]}}$(printf \'\\r\')"')
            msg = f"Granted Rapid Regeneration to '{target}'!"
        elif buff == "clear":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect {cmd_target} clear$(printf \'\\r\')"')
            run_bash(f'screen -S bedrock -p 0 -X stuff "titleraw {cmd_target} actionbar {{\\\"rawtext\\\":[{{\\\"text\\\":\\\"§7§l✨ Cleared all effects!\\\"}}]}}$(printf \'\\r\')"')
            msg = f"Cleared all effects from '{target}'!"
        else:
            return jsonify({"error": "Unknown buff type"}), 400

        return jsonify({"status": "success", "message": msg})

    elif action_type == "item":
        item = data.get("item", "").strip().lower()
        count = data.get("count") or data.get("quantity") or data.get("amount") or 64
        try:
            count = max(1, min(65535, int(count)))
        except (ValueError, TypeError):
            count = 64

        if item in ["diamonds", "diamond"]:
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" diamond {count}$(printf \'\\r\')"')
            msg = f"Gave {count}x Diamonds to '{target}'!"
        elif item in ["iron", "iron_ingot"]:
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" iron_ingot {count}$(printf \'\\r\')"')
            msg = f"Gave {count}x Iron Ingots to '{target}'!"
        elif item in ["golden_apples", "enchanted_golden_apple"]:
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" enchanted_golden_apple {count}$(printf \'\\r\')"')
            msg = f"Gave {count}x Enchanted Golden Apples to '{target}'!"
        elif item in ["totem", "totem_of_undying"]:
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" totem_of_undying {count}$(printf \'\\r\')"')
            msg = f"Gave {count}x Totem of Undying to '{target}'!"
        elif item == "elytra":
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" elytra {count}$(printf \'\\r\')"')
            fireworks = min(count * 64, 320)
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" firework_rocket {fireworks}$(printf \'\\r\')"')
            msg = f"Gave {count}x Elytra and {fireworks}x Fireworks to '{target}'!"
        elif item == "netherite_gear":
            commands = [
                f'give "{safe_target}" netherite_sword 1',
                f'give "{safe_target}" netherite_pickaxe 1',
                f'give "{safe_target}" netherite_axe 1',
                f'give "{safe_target}" netherite_helmet 1',
                f'give "{safe_target}" netherite_chestplate 1',
                f'give "{safe_target}" netherite_leggings 1',
                f'give "{safe_target}" netherite_boots 1'
            ]
            for cmd in commands:
                run_bash(f'screen -S bedrock -p 0 -X stuff "{cmd}$(printf \'\\r\')"')
            msg = f"Gave full Netherite armor & tool set to '{target}'!"
        else:
            safe_item = re.sub(r'[^a-zA-Z0-9_:]', '', item)
            if not safe_item:
                return jsonify({"error": "Invalid item name"}), 400
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" {safe_item} {count}$(printf \'\\r\')"')
            msg = f"Gave {count}x '{safe_item}' to '{target}'!"

        return jsonify({"status": "success", "message": msg})

    elif action_type in ["teleport", "tp"]:
        mode = data.get("mode", "coords")
        if mode == "player":
            dest = data.get("destination", "").strip()
            if not dest:
                return jsonify({"error": "No destination player specified"}), 400
            safe_dest = dest.replace('"', '\\"')
            run_bash(f'screen -S bedrock -p 0 -X stuff "tp \\"{safe_target}\\" \\"{safe_dest}\\"$(printf \'\\r\')"')
            msg = f"Teleported '{target}' to player '{dest}'!"
            return jsonify({"status": "success", "message": msg})
        else:
            x = str(data.get("x", "~")).strip() or "~"
            y = str(data.get("y", "~")).strip() or "~"
            z = str(data.get("z", "~")).strip() or "~"
            safe_x = re.sub(r'[^0-9\-~.]', '', x) or "~"
            safe_y = re.sub(r'[^0-9\-~.]', '', y) or "~"
            safe_z = re.sub(r'[^0-9\-~.]', '', z) or "~"
            run_bash(f'screen -S bedrock -p 0 -X stuff "tp \\"{safe_target}\\" {safe_x} {safe_y} {safe_z}$(printf \'\\r\')"')
            msg = f"Teleported '{target}' to coordinates ({safe_x}, {safe_y}, {safe_z})!"
            return jsonify({"status": "success", "message": msg, "coords": {"x": safe_x, "y": safe_y, "z": safe_z}})

    elif action_type == "locate":
        struct = data.get("structure") or data.get("name") or "village"
        locate_kind = data.get("locate_type", "structure")
        safe_struct = re.sub(r'[^a-zA-Z0-9_:]', '', struct.strip().lower())
        if not safe_struct:
            return jsonify({"error": "Invalid structure name"}), 400

        cmd = f"locate {locate_kind} {safe_struct}"
        run_bash(f'screen -S bedrock -p 0 -X stuff "{cmd}$(printf \'\\r\')"')

        time.sleep(0.8)
        found_msg = f"Locate command executed for '{safe_struct}'."
        coords = None
        if os.path.exists(SERVER_LOG_FILE):
            out, _, _ = run_bash(f'tail -n 25 "{SERVER_LOG_FILE}"')
            lines = out.splitlines()
            for line in reversed(lines):
                if "The nearest" in line and (safe_struct in line or "is at block" in line or "is at" in line):
                    found_msg = line.split("INFO] ", 1)[-1] if "INFO] " in line else line
                    m = re.search(r'is at (?:block )?([-\d]+),\s*(\([^\)]+\)|[-\d]+)?,\s*([-\d]+)', line)
                    if m:
                        coords = {"x": m.group(1), "y": "~" if "(y?)" in (m.group(2) or "") else m.group(2), "z": m.group(3)}
                    break
                elif "Could not find" in line or "syntax error" in line.lower():
                    found_msg = line.split("ERROR] ", 1)[-1] if "ERROR] " in line else line
                    break

        return jsonify({
            "status": "success",
            "message": found_msg,
            "coords": coords,
            "structure": safe_struct
        })

    elif action_type == "custom":
        cmd = data.get("command", "").strip()
        if not cmd:
            return jsonify({"error": "No command provided"}), 400
        clean_cmd = cmd.replace('"', '\\"')
        run_bash(f'screen -S bedrock -p 0 -X stuff "{clean_cmd}$(printf \'\\r\')"')
        return jsonify({"status": "success", "message": f"Executed: {cmd}"})

    return jsonify({"error": "Invalid action type"}), 400


# ==================== SETTINGS & PASSWORD API ====================

def update_server_properties(props_dict):
    if not os.path.exists(PROPERTIES_FILE):
        return False
    try:
        with open(PROPERTIES_FILE, "r") as f:
            lines = f.readlines()
    except Exception:
        lines = []

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in props_dict:
                val_str = str(props_dict[k]).strip()
                if val_str.lower() in ["true", "false"]:
                    val_str = val_str.lower()
                new_lines.append(f"{k}={val_str}\n")
                updated_keys.add(k)
                continue
        new_lines.append(line)

    for k, v in props_dict.items():
        if k not in updated_keys:
            val_str = str(v).strip()
            if val_str.lower() in ["true", "false"]:
                val_str = val_str.lower()
            new_lines.append(f"{k}={val_str}\n")

    with open(PROPERTIES_FILE, "w") as f:
        f.writelines(new_lines)

    # Sync to BDS dir
    try:
        bds_props = "/opt/bedrock-server/server.properties"
        if os.path.exists("/opt/bedrock-server") and not os.path.islink(bds_props):
            shutil.copy2(PROPERTIES_FILE, bds_props)
    except Exception:
        pass

    # Sync to repo config dir
    try:
        cfg_props = os.path.join(BASE_DIR, "config", "server.properties")
        os.makedirs(os.path.dirname(cfg_props), exist_ok=True)
        shutil.copy2(PROPERTIES_FILE, cfg_props)
    except Exception:
        pass

    return True


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    if request.method == "GET":
        settings = {}
        if os.path.exists(PROPERTIES_FILE):
            with open(PROPERTIES_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        settings[k.strip()] = v.strip()
        return jsonify(settings)

    if request.method == "POST":
        data = request.get_json() or {}
        allowed_keys = [
            "view-distance", "tick-distance", "max-players", "server-name",
            "difficulty", "allow-cheats", "gamemode", "force-gamemode",
            "online-mode", "allow-list", "default-player-permission-level"
        ]
        updates = {}
        for k, v in data.items():
            if k in allowed_keys:
                updates[k] = v

        if updates and os.path.exists(PROPERTIES_FILE):
            update_server_properties(updates)

            # Live in-game console commands if server is running
            if "difficulty" in updates:
                diff = str(updates["difficulty"]).strip().lower()
                run_bash(f'screen -S bedrock -p 0 -X stuff "difficulty {diff}$(printf \'\\r\')"')

            if "allow-cheats" in updates:
                c_val = str(updates["allow-cheats"]).strip().lower()
                run_bash(f'screen -S bedrock -p 0 -X stuff "changesetting allow-cheats {c_val}$(printf \'\\r\')"')

            # Optional immediate restart if requested
            if data.get("restart"):
                threading.Thread(target=lambda: run_bash('screen -S bedrock -p 0 -X stuff "stop$(printf \'\\r\')"; sleep 2; screen -S bedrock -X quit 2>/dev/null; pkill -9 -x bedrock_server 2>/dev/null; screen -dmS bedrock bash -c "while true; do echo \\"[\\$(date)] Starting Bedrock Server...\\" | tee -a ' + SERVER_LOG_FILE + '; cd /opt/bedrock-server && LD_LIBRARY_PATH=. ./bedrock_server 2>&1 | tee -a ' + SERVER_LOG_FILE + '; sleep 5; done"'), daemon=True).start()

            # Background sync to GitHub backup so cold boot or redeploy never loses these settings
            if get_github_token():
                threading.Thread(target=lambda: run_github_backup(trigger="settings_sync"), daemon=True).start()

            return jsonify({"status": "saved", "updated": list(updates.keys())})
        return jsonify({"error": "File not found or no valid keys"}), 404


@app.route("/api/change-password", methods=["POST"])
def api_change_password():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    new_pw = data.get("new_password", "").strip()
    if len(new_pw) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400

    with open(PASSWORD_FILE, "w") as f:
        f.write(new_pw)
    return jsonify({"status": "password updated"})


# ==================== 1-CLICK TURNKEY AUTO-SETUP ====================

def ensure_auto_setup():
    """
    1-Click Auto Setup:
    1. Installs/Verifies Playit.gg with permanent secret_key (nicely-retread.tun.ply.gg:17373)
    2. Installs/Verifies Bedrock Dedicated Server binary & configuration
    3. Auto-links persistent world, properties, allowlist, and operator permissions
    4. Auto-starts Bedrock server and Playit tunnel in background screen sessions
    5. Sets up cron watchdog for 24/7 reliability
    """
    print("[*] Checking 24/7 Bedrock Environment & Auto-Setup...")
    
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BEDROCK_DATA, exist_ok=True)
    os.makedirs(WORLDS_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    os.makedirs(PLAYIT_DIR, exist_ok=True)

    # 1. Playit configuration with permanent secret
    playit_toml = os.path.join(PLAYIT_DIR, "playit.toml")
    if not os.path.exists(playit_toml) or os.path.getsize(playit_toml) == 0:
        print("[*] Creating permanent Playit.gg tunnel config...")
        with open(playit_toml, "w") as f:
            f.write(f'secret_key = "{PLAYIT_SECRET}"\n')
        print(f"[✓] Playit configured: {TUNNEL_DOMAIN}:{TUNNEL_PORT}")

    # 2. Playit daemon binary
    playit_bin = shutil.which("playitd") or "/usr/bin/playitd" or "/usr/local/bin/playitd"
    if not (os.path.exists(playit_bin) and os.access(playit_bin, os.X_OK)):
        print("[*] Playit daemon not found. Installing standalone binary...")
        try:
            target_bin = "/usr/local/bin/playitd"
            run_bash(f'curl -fsSL -o "{target_bin}" https://github.com/playit-cloud/playit-agent/releases/download/v0.15.26/playit-linux-amd64')
            if os.path.exists(target_bin):
                os.chmod(target_bin, 0o755)
                print("[✓] Playit daemon successfully installed to " + target_bin)
        except Exception as e:
            print(f"[!] Warning: Could not download playitd binary: {e}")

    # 3. Bedrock Dedicated Server binary
    bds_dir = "/opt/bedrock-server"
    bds_bin = os.path.join(bds_dir, "bedrock_server")
    if not os.path.exists(bds_bin):
        print("[*] Bedrock Dedicated Server not found. Auto-installing v1.26.2.1...")
        os.makedirs(bds_dir, exist_ok=True)
        tmp_zip = "/tmp/bedrock_server_install.zip"
        url = "https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-1.26.2.1.zip"
        run_bash(f'curl -fsSL -A "Mozilla/5.0" -o "{tmp_zip}" "{url}"')
        if os.path.exists(tmp_zip) and zipfile.is_zipfile(tmp_zip):
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(bds_dir)
            if os.path.exists(bds_bin):
                os.chmod(bds_bin, 0o755)
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
            print("[✓] Bedrock Dedicated Server successfully installed.")

    # 4. Optimized default configurations
    if not os.path.exists(PROPERTIES_FILE):
        default_props = (
            "server-name=Bedrock 24/7 Server\n"
            "gamemode=survival\n"
            "difficulty=normal\n"
            "allow-cheats=true\n"
            "max-players=10\n"
            "online-mode=true\n"
            "white-list=false\n"
            "server-port=19132\n"
            "server-portv6=19133\n"
            "view-distance=6\n"
            "tick-distance=4\n"
            "player-idle-timeout=30\n"
            "max-threads=2\n"
            "level-name=Bedrock level\n"
            "level-seed=\n"
            "default-player-permission-level=member\n"
            "texturepack-required=false\n"
            "content-log-file-enabled=true\n"
            "compression-threshold=1\n"
            "server-authoritative-movement=server-auth\n"
            "player-movement-score-threshold=20\n"
            "player-movement-distance-threshold=0.3\n"
            "player-movement-duration-threshold-in-ms=500\n"
        )
        with open(PROPERTIES_FILE, "w") as f:
            f.write(default_props)

    if not os.path.exists(PERMISSIONS_FILE) or os.path.getsize(PERMISSIONS_FILE) < 5:
        with open(PERMISSIONS_FILE, "w") as f:
            json.dump([
                {"permission": "operator", "xuid": "2535428121904797"},
                {"permission": "operator", "xuid": "2535455223264941"}
            ], f, indent=3)

    if not os.path.exists(KNOWN_PLAYERS_FILE):
        with open(KNOWN_PLAYERS_FILE, "w") as f:
            json.dump({
                "names": {"biswajit2876": "2535428121904797", "mc flash1564": "2535455223264941"},
                "xuids": {"2535428121904797": "biswajit2876", "2535455223264941": "MC FLASH1564"}
            }, f, indent=3)

    if not os.path.exists(ALLOWLIST_FILE):
        with open(ALLOWLIST_FILE, "w") as f:
            f.write("[]\n")

    # 5. Relink persistent files into BDS directory
    if os.path.exists(bds_dir):
        run_bash(f'rm -f "{bds_dir}/server.properties" "{bds_dir}/allowlist.json" "{bds_dir}/permissions.json"')
        run_bash(f'rm -rf "{bds_dir}/worlds"')
        run_bash(f'ln -sf "{PROPERTIES_FILE}" "{bds_dir}/server.properties"')
        run_bash(f'ln -sf "{ALLOWLIST_FILE}" "{bds_dir}/allowlist.json"')
        run_bash(f'ln -sf "{PERMISSIONS_FILE}" "{bds_dir}/permissions.json"')
        run_bash(f'ln -sf "{WORLDS_DIR}" "{bds_dir}/worlds"')

    # 5.5 Ensure server-control.sh and watchdog are available in DATA_DIR
    for script_name in ["server-control.sh", "server-watchdog.sh"]:
        src_script = os.path.join(BASE_DIR, script_name)
        dst_script = os.path.join(DATA_DIR, script_name)
        if os.path.exists(src_script) and src_script != dst_script:
            try:
                shutil.copy2(src_script, dst_script)
                os.chmod(dst_script, 0o755)
            except Exception:
                pass

    # 6. Start Playit tunnel in screen if not running
    run_bash('screen -wipe >/dev/null 2>&1 || true')
    out_playit, _, _ = run_bash("ps -o pid=,stat= -C playitd 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
    if not out_playit.strip():
        print("[*] Starting Playit tunnel in screen...")
        run_bash('screen -S playit -X quit 2>/dev/null || true')
        run_bash(f'screen -dmS playit bash -c "while true; do echo \\"[\\$(date)] Starting Playit tunnel...\\" >> \\"{PLAYIT_LOG_FILE}\\" 2>&1; playitd --secret_path \\"{playit_toml}\\" >> \\"{PLAYIT_LOG_FILE}\\" 2>&1; sleep 5; done"')
        print("[✓] Playit tunnel active in screen session: playit")
    else:
        print("[✓] Playit tunnel is already running.")

    # 6.5 Auto-restore world & settings from GitHub on cold container boot
    world_db = os.path.join(ACTIVE_WORLD_DIR, "db")
    has_world = os.path.exists(world_db) and len(os.listdir(world_db)) > 2
    if not has_world and get_github_token():
        print("[*] Cold container boot detected. Auto-restoring world & settings from GitHub cloud backup...")
        try:
            ok, r_msg = run_github_restore()
            print(f"[*] Auto-restore result: {ok} - {r_msg}")
        except Exception as e:
            print(f"[!] Auto-restore error: {e}")

    # 7. Start Bedrock server in screen if not running
    out_bds, _, _ = run_bash("ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
    if not out_bds.strip():
        print("[*] Starting Bedrock server in screen...")
        run_bash('screen -S bedrock -X quit 2>/dev/null || true')
        run_bash(f'screen -dmS bedrock bash -c "while true; do echo \\"[\\$(date)] Starting Bedrock Server...\\" | tee -a \\"{SERVER_LOG_FILE}\\"; cd /opt/bedrock-server && LD_LIBRARY_PATH=. ./bedrock_server 2>&1 | tee -a \\"{SERVER_LOG_FILE}\\"; sleep 5; done"')
        print("[✓] Bedrock server active in screen session: bedrock")
    else:
        print("[✓] Bedrock server is already running.")

    # 8. Start cron if available
    run_bash("service cron start >/dev/null 2>&1 || true")

    # 9. Start automated midnight backup scheduler
    threading.Thread(target=backup_scheduler_worker, daemon=True).start()
    print("[✓] Automated 12:00 AM IST GitHub Cloud Backup scheduler initialized.")
    print("[✓] Turnkey Setup Complete: Server & Tunnel are Live!")


if __name__ == "__main__":
    ensure_auto_setup()
    raw_port = os.environ.get("PORT_DASHBOARD", os.environ.get("PORT", 5000))
    port = int(raw_port)
    if port == 8080:  # Port 8080 is reserved for ttyd terminal in Railway
        port = 5000
    print(f"[*] Starting Web Management Dashboard on port {port}...")
    app.run(host="0.0.0.0", port=port)
