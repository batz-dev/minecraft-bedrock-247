# ⛏️ Minecraft Bedrock 24/7 Dedicated Server & Web Manager

Complete, 1-click self-bootstrapping Minecraft Bedrock Edition 24/7 Dedicated Server with Web Management Dashboard and permanent zero-config Playit.gg UDP tunneling.

---

## 🌟 Key Features

- **⚡ 1-Click Auto Setup (`python3 app.py`):**
  Running `app.py` automatically checks, downloads, configures, and boots:
  - Official Bedrock Dedicated Server (BDS) Linux engine
  - Playit.gg UDP tunneling agent with permanent tunnel secret
  - GNU Screen background sessions with auto-restart loops
  - Flask web management dashboard
- **📱 Permanent Mobile Connection (Zero Port Forwarding):**
  - **Server Domain:** `nicely-retread.tun.ply.gg`
  - **Server Port:** `17373`
  - **Numeric IP:** `147.185.221.213`
- **🎮 Web Management Dashboard (Port 5000):**
  - **Real-Time Resource Graphs:** Live CPU and RAM usage tracking (optimized for < 400 MB RAM, silky 120 FPS)
  - **🔄 1-Click Version Switcher:** Downgrade or upgrade server versions (1.26.45, 1.26.2, 1.26.0, 1.21.50) without losing your world
  - **👑 Operator (Admin) Management:** View, grant, and revoke in-game OP permissions online and offline with persistent XUID tracking
  - **🛡️ 1-Click Game Rules:** Toggle KeepInventory (no item loss on death), Anti-Creeper griefing, coordinates display, PvP, daylight cycle live
  - **⚡ Superpowers & Potion Buffs:** Give permanent Night Vision, Speed II, Infinite Hunger/Saturation, Strength II, Regeneration with 1 click
  - **💎 Instant Item Dispenser:** 64x Diamonds, full Netherite armor & tools, Elytra & rockets, God Apples
  - **📢 Big Screen Broadcast:** Send giant golden title announcements directly across all mobile players' screens
  - **🌍 World Backup & Restore:** 1-click download of `.mcworld` backup or upload a map to replace the world
  - **🖥️ Live Console:** Live server logs and interactive command sender

---

## 🚀 Quick Start (1-Click Run)

### Local / VPS / Linux Container
```bash
# 1. Clone repository
git clone https://github.com/batz-dev/minecraft-bedrock-247.git
cd minecraft-bedrock-247

# 2. Install Python requirements
pip3 install -r requirements.txt

# 3. Start everything with one command!
python3 app.py
```

### Docker
```bash
docker build -t minecraft-bedrock-247 .
docker run -d -p 5000:5000 -p 19132:19132/udp --name bedrock-server minecraft-bedrock-247
```

### Default Dashboard Login
- **URL:** `http://localhost:5000` (or your host domain)
- **Default Password:** `admin123` *(changeable in Settings)*

---

## 📱 In-Game Admin (OP) Commands Cheat Sheet

To use commands inside Minecraft Mobile:
1. Tap the speech bubble icon `[💬]` at the top center of the screen.
2. Start the command with `/` and tap Enter.

| Action | Command |
| :--- | :--- |
| **Creative Mode** | `/gamemode creative` |
| **Survival Mode** | `/gamemode survival` |
| **Keep Inventory (No Item Loss)** | `/gamerule keepinventory true` |
| **Permanent Night Vision** | `/effect @s night_vision 99999 1 true` |
| **Super Speed II** | `/effect @s speed 99999 2 true` |
| **Never Hungry (Saturation)** | `/effect @s saturation 99999 1 true` |
| **Give 64 Diamonds** | `/give @s diamond 64` |
| **Give Elytra & Fireworks** | `/give @s elytra 1` followed by `/give @s firework_rocket 64` |
| **Find Nearest Village** | `/locate structure village` |
| **Find Nether Fortress** | `/locate structure fortress` |
| **Teleport to Friend** | `/tp @s "PlayerName"` |
| **Set Time to Day** | `/time set day` |
| **Clear Rain/Storm** | `/weather clear` |

---

## 📂 Project Structure

```
minecraft-bedrock-247/
├── app.py                     # Self-bootstrapping turnkey entrypoint + Flask Dashboard
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Docker deployment image
├── Procfile                   # Process configuration for PaaS (Railway, Render, etc.)
├── server-control.sh          # Server CLI management script
├── server-watchdog.sh         # Cron 24/7 watchdog
├── config/                    # Default configuration templates
│   ├── server.properties      # Optimized 120 FPS server settings
│   ├── permissions.json       # Operator permissions
│   ├── known_players.json     # Permanent player XUID database
│   └── playit.toml            # Playit tunnel configuration
└── templates/
    ├── index.html             # Web Management UI
    └── login.html             # Dashboard Login UI
```

---

## 🛡️ License
MIT License
