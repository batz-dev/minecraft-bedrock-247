import os
import re
import io
import json
import shutil
import zipfile
import secrets
import datetime
import subprocess
import urllib.request
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from werkzeug.utils import secure_filename

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
if not os.path.exists(TEMPLATE_DIR):
    TEMPLATE_DIR = "/data/dashboard/templates"

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB max world upload

DATA_DIR = "/data" if os.path.exists("/data") and os.access("/data", os.W_OK) else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
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

TUNNEL_DOMAIN = "nicely-retread.tun.ply.gg"
TUNNEL_IP = "147.185.221.213"
TUNNEL_PORT = 17373
PLAYIT_SECRET = "3f951dcf83b320ccdf736109ca0c51d67287516c996a1d67427b97a32aa6f26a"

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
    try:
        with open("/data/server-control.sh", "r") as f:
            for line in f:
                if line.startswith("BDS_VERSION="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "1.26.45.1"


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
    run_bash('screen -S bedrock -p 0 -X stuff "list$(printf \'\\r\')"')
    import time
    time.sleep(0.3)
    out, _, _ = run_bash("tail -n 40 /data/bedrock-server.log 2>/dev/null")
    players = []
    max_p = 10
    lines = out.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        m = re.search(r"There are (\d+)/(\d+) players online:(.*)", line)
        if m:
            count = int(m.group(1))
            max_p = int(m.group(2))
            same_line = m.group(3).strip()
            if same_line:
                players = [p.strip() for p in same_line.split(",") if p.strip()]
            elif count > 0:
                for j in range(i + 1, min(i + 1 + count + 2, len(lines))):
                    cand = lines[j].strip()
                    if cand and not cand.startswith("[") and not cand.startswith("list"):
                        for p in cand.split(","):
                            if p.strip() and p.strip() not in players:
                                players.append(p.strip())
            break
    return players, max_p


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


def change_server_version(target_version):
    url = resolve_version_url(target_version)
    if not url:
        return False, f"Version '{target_version}' could not be resolved. Please verify the version number.", ""

    tmp_zip = f"/tmp/bds_dl_{secrets.token_hex(4)}.zip"
    try:
        run_bash(f'curl -fsSL -A "Mozilla/5.0" -o "{tmp_zip}" "{url}"')
        if not os.path.exists(tmp_zip) or not zipfile.is_zipfile(tmp_zip):
            return False, f"Failed to download valid server archive for {target_version}.", ""

        # Stop BDS cleanly
        run_bash('screen -S bedrock -p 0 -X stuff "stop$(printf \'\\r\')"')
        import time
        for _ in range(8):
            time.sleep(1)
            out, _, _ = run_bash("ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
            if not out.strip():
                break
        run_bash('screen -S bedrock -X quit 2>/dev/null')
        run_bash('pkill -9 -x bedrock_server 2>/dev/null')

        # Wipe old server engine
        bds_dir = "/opt/bedrock-server"
        shutil.rmtree(bds_dir, ignore_errors=True)
        os.makedirs(bds_dir, exist_ok=True)

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            zf.extractall(bds_dir)
        os.remove(tmp_zip)

        bds_bin = os.path.join(bds_dir, "bedrock_server")
        if os.path.exists(bds_bin):
            os.chmod(bds_bin, 0o755)

        # Relink persistent files
        data_dir = "/data/bedrock-data"
        run_bash(f'rm -f "{bds_dir}/server.properties" "{bds_dir}/allowlist.json" "{bds_dir}/permissions.json"')
        run_bash(f'rm -rf "{bds_dir}/worlds"')
        run_bash(f'ln -sf "{data_dir}/server.properties" "{bds_dir}/server.properties"')
        run_bash(f'ln -sf "{data_dir}/allowlist.json" "{bds_dir}/allowlist.json"')
        run_bash(f'ln -sf "{data_dir}/permissions.json" "{bds_dir}/permissions.json"')
        run_bash(f'ln -sf "{data_dir}/worlds" "{bds_dir}/worlds"')

        m = re.search(r"bedrock-server-([0-9\.]+)\.zip", url)
        final_v = m.group(1) if m else target_version
        run_bash(f'sed -i \'s/BDS_VERSION=".*"/BDS_VERSION="{final_v}"/g\' /data/server-control.sh')

        run_bash("/data/server-control.sh start")
        return True, f"Successfully switched to Bedrock version {final_v}! Server is now running.", final_v
    except Exception as e:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)
        run_bash("/data/server-control.sh start")
        return False, f"Failed to switch version: {str(e)}", ""


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

    out, _, _ = run_bash("ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}' | head -n 1")
    bds_pid = out.strip()
    is_running = bool(bds_pid)
    mem_mb = 0
    cpu_pct = 0.0
    uptime = "0s"

    if is_running:
        out_mem, _, _ = run_bash(f"ps -o rss= -p {bds_pid} 2>/dev/null")
        try:
            mem_mb = int(out_mem.strip()) // 1024
        except Exception:
            mem_mb = 0

        out_cpu, _, _ = run_bash(f"ps -o %cpu= -p {bds_pid} 2>/dev/null")
        try:
            cpu_pct = float(out_cpu.strip() or "0.0")
        except Exception:
            cpu_pct = 0.0

        out_uptime, _, _ = run_bash(f"ps -o etime= -p {bds_pid} 2>/dev/null")
        uptime = out_uptime.strip() or "Running"

    out_playit, _, _ = run_bash("ps -o pid=,stat= -C playitd 2>/dev/null | awk '$2 !~ /Z/ {print $1}' | head -n 1")
    playit_running = bool(out_playit.strip())

    disk_out, _, _ = run_bash("df -h /data | awk 'NR==2 {print $3 \" / \" $2 \" (\" $5 \")\"}'")
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

    out, _, _ = run_bash("tail -n 120 /data/bedrock-server.log 2>/dev/null")
    lines = out.split("\n")
    return jsonify({"logs": lines[-100:]})


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
        run_bash("/data/server-control.sh start")
        return jsonify({"status": "started"})
    elif action == "stop":
        run_bash("/data/server-control.sh stop")
        return jsonify({"status": "stopped"})
    elif action == "restart":
        run_bash("/data/server-control.sh restart")
        return jsonify({"status": "restarted"})
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

    success, msg, final_v = change_server_version(target_version)
    if success:
        return jsonify({
            "status": "success",
            "message": msg,
            "version": final_v,
            "tunnel_domain": TUNNEL_DOMAIN,
            "tunnel_ip": TUNNEL_IP,
            "tunnel_port": TUNNEL_PORT
        })
    else:
        return jsonify({"error": msg}), 400


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
        "naturalregeneration": "Natural Health Regeneration"
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
    safe_target = target.replace('"', '\\"')

    if action_type == "buff":
        buff = data.get("buff", "").strip().lower()
        if buff == "night_vision":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect \\"{safe_target}\\" night_vision 99999 1 true$(printf \'\\r\')"')
            msg = f"Granted permanent Night Vision to '{target}'!"
        elif buff == "speed":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect \\"{safe_target}\\" speed 99999 2 true$(printf \'\\r\')"')
            msg = f"Granted Speed II buff to '{target}'!"
        elif buff == "saturation":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect \\"{safe_target}\\" saturation 99999 1 true$(printf \'\\r\')"')
            msg = f"Granted Infinite Hunger/Saturation to '{target}'!"
        elif buff == "strength":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect \\"{safe_target}\\" strength 99999 2 true$(printf \'\\r\')"')
            msg = f"Granted Strength II buff to '{target}'!"
        elif buff == "regeneration":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect \\"{safe_target}\\" regeneration 99999 2 true$(printf \'\\r\')"')
            msg = f"Granted Rapid Regeneration to '{target}'!"
        elif buff == "clear":
            run_bash(f'screen -S bedrock -p 0 -X stuff "effect \\"{safe_target}\\" clear$(printf \'\\r\')"')
            msg = f"Cleared all effects from '{target}'!"
        else:
            return jsonify({"error": "Unknown buff type"}), 400

        return jsonify({"status": "success", "message": msg})

    elif action_type == "item":
        item = data.get("item", "").strip().lower()
        if item == "diamonds":
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" diamond 64$(printf \'\\r\')"')
            msg = f"Gave 64 Diamonds to '{target}'!"
        elif item == "iron":
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" iron_ingot 64$(printf \'\\r\')"')
            msg = f"Gave 64 Iron Ingots to '{target}'!"
        elif item == "netherite_gear":
            commands = [
                f'give "{safe_target}" netherite_sword 1',
                f'give "{safe_target}" netherite_pickaxe 1',
                f'give "{safe_target}" netherite_helmet 1',
                f'give "{safe_target}" netherite_chestplate 1',
                f'give "{safe_target}" netherite_leggings 1',
                f'give "{safe_target}" netherite_boots 1'
            ]
            for cmd in commands:
                run_bash(f'screen -S bedrock -p 0 -X stuff "{cmd}$(printf \'\\r\')"')
            msg = f"Gave full Netherite armor & tool set to '{target}'!"
        elif item == "elytra":
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" elytra 1$(printf \'\\r\')"')
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" firework_rocket 64$(printf \'\\r\')"')
            msg = f"Gave Elytra and 64 Firework Rockets to '{target}'!"
        elif item == "golden_apples":
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" enchanted_golden_apple 64$(printf \'\\r\')"')
            msg = f"Gave 64 Enchanted Golden Apples to '{target}'!"
        elif item == "totem":
            run_bash(f'screen -S bedrock -p 0 -X stuff "give \\"{safe_target}\\" totem_of_undying 1$(printf \'\\r\')"')
            msg = f"Gave Totem of Undying to '{target}'!"
        else:
            return jsonify({"error": "Unknown item"}), 400

        return jsonify({"status": "success", "message": msg})

    elif action_type == "custom":
        cmd = data.get("command", "").strip()
        if not cmd:
            return jsonify({"error": "No command provided"}), 400
        clean_cmd = cmd.replace('"', '\\"')
        run_bash(f'screen -S bedrock -p 0 -X stuff "{clean_cmd}$(printf \'\\r\')"')
        return jsonify({"status": "success", "message": f"Executed: {cmd}"})

    return jsonify({"error": "Invalid action type"}), 400


# ==================== SETTINGS & PASSWORD API ====================

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
        allowed_keys = ["view-distance", "tick-distance", "max-players", "server-name", "difficulty", "allow-cheats"]
        if os.path.exists(PROPERTIES_FILE):
            for k, v in data.items():
                if k in allowed_keys:
                    safe_v = str(v).strip().replace("'", "").replace('"', "")
                    run_bash(f"sed -i 's/^{k}=.*/{k}={safe_v}/' {PROPERTIES_FILE}")
            return jsonify({"status": "saved"})
        return jsonify({"error": "File not found"}), 404


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
            "max-threads=4\n"
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

    # 6. Start Playit tunnel in screen if not running
    run_bash('screen -wipe >/dev/null 2>&1 || true')
    out_playit, _, _ = run_bash("ps -o pid=,stat= -C playitd 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
    if not out_playit.strip():
        print("[*] Starting Playit tunnel in screen...")
        run_bash('screen -S playit -X quit 2>/dev/null || true')
        run_bash(f'screen -dmS playit bash -c "while true; do echo \\"[\\$(date)] Starting Playit tunnel...\\" | tee -a /data/playit.log; playitd --secret-path \\"{playit_toml}\\" 2>&1 | tee -a /data/playit.log; sleep 5; done"')
        print("[✓] Playit tunnel active in screen session: playit")
    else:
        print("[✓] Playit tunnel is already running.")

    # 7. Start Bedrock server in screen if not running
    out_bds, _, _ = run_bash("ps -o pid=,stat= -C bedrock_server 2>/dev/null | awk '$2 !~ /Z/ {print $1}'")
    if not out_bds.strip():
        print("[*] Starting Bedrock server in screen...")
        run_bash('screen -S bedrock -X quit 2>/dev/null || true')
        run_bash('screen -dmS bedrock bash -c "while true; do echo \\"[\\$(date)] Starting Bedrock Server...\\" | tee -a /data/bedrock-server.log; cd /opt/bedrock-server && LD_LIBRARY_PATH=. ./bedrock_server 2>&1 | tee -a /data/bedrock-server.log; sleep 5; done"')
        print("[✓] Bedrock server active in screen session: bedrock")
    else:
        print("[✓] Bedrock server is already running.")

    # 8. Start cron if available
    run_bash("service cron start >/dev/null 2>&1 || true")
    print("[✓] Turnkey Setup Complete: Server & Tunnel are Live!")


if __name__ == "__main__":
    ensure_auto_setup()
    raw_port = os.environ.get("PORT_DASHBOARD", os.environ.get("PORT", 5000))
    port = int(raw_port)
    if port == 8080:  # Port 8080 is reserved for ttyd terminal in Railway
        port = 5000
    print(f"[*] Starting Web Management Dashboard on port {port}...")
    app.run(host="0.0.0.0", port=port)
