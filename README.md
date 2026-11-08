# 🎬 Bunny Stream to Telegram Downloader

<p align="center">
  <img src="https://raw.githubusercontent.com/NimeshPiyathissa/bunny-telegram-bot/main/assets/banner.png" alt="Bunny Stream Downloader Banner" width="800" onerror="this.style.display='none'"/>
</p>

<p align="center">
  <strong>Production-grade, asynchronous Telegram bot engineered to ingest authorized Bunny Stream embeds, extract multi-bitrate HLS streams, remux losslessly with FFmpeg, and deliver 16:9 widescreen MP4 videos directly to Telegram chats.</strong>
</p>

<p align="center">
  <a href="https://github.com/NimeshPiyathissa/bunny-telegram-bot/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/CI-Passing-brightgreen?style=for-the-badge&logo=githubactions&logoColor=white" alt="CI Status"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"/></a>
  <a href="https://ubuntu.com/"><img src="https://img.shields.io/badge/Ubuntu-24.04%20LTS-E95420?style=for-the-badge&logo=ubuntu&logoColor=white" alt="Ubuntu 24.04 LTS"/></a>
  <a href="https://ffmpeg.org/"><img src="https://img.shields.io/badge/FFmpeg-Enabled-007808?style=for-the-badge&logo=ffmpeg&logoColor=white" alt="FFmpeg"/></a>
  <a href="https://docs.docker.com/"><img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-purple?style=for-the-badge" alt="MIT License"/></a>
</p>

---

## 📑 Table of Contents

- [Key Features](#-key-features)
- [System Architecture](#-system-architecture)
- [Video Processing Pipeline](#-video-processing-pipeline)
- [User Experience & Visual Interface](#-user-experience--visual-interface)
- [Prerequisites](#-prerequisites)
- [Ubuntu 24.04 LTS Production Deployment (systemd)](#-ubuntu-2404-lts-production-deployment-systemd)
- [Docker & Docker Compose Deployment](#-docker--docker-compose-deployment)
- [Windows 10/11 Development Setup](#-windows-1011-development-setup)
- [DevOps & Maintenance Tooling](#-devops--maintenance-tooling)
  - [Automated Backups](#1-automated-atomic-backups)
  - [Safe Restores](#2-safe-database-restore)
  - [Zero-Downtime Updates](#3-zero-downtime-application-update)
  - [System Health Checks](#4-system-health-diagnostics)
- [Environment Configuration Reference](#-environment-configuration-reference)
- [Command Reference](#-command-reference)
- [Security Defenses & Access Control](#-security-defenses--access-control)
- [Automated Testing & QA](#-automated-testing--qa)
- [Compliance & Responsible Use](#-compliance--responsible-use)
- [Contributing & License](#-contributing--license)

---

## 🌟 Key Features

### 🎥 Intelligent Bunny Stream Ingestion
- **Embed URL Parsing**: Resolves standard Bunny Stream web player embeds (`https://iframe.mediadelivery.net/embed/{library_id}/{video_guid}`) into authorized master HLS playlists and CDN endpoints.
- **Dynamic Quality Probing**: Probes actual resolutions available on the source manifest (360p, 480p, 720p, 1080p, 1440p, 4K) and renders an interactive Telegram keyboard containing only valid tiers.
- **Direct & Fallback Stream Handling**: Seamlessly downloads segmented HLS streams or direct MP4 assets, resolving separate audio and video tracks without quality loss.

### 📐 16:9 Native Aspect Ratio & Widescreen Optimization
- **Geometry & SAR/DAR Inspection**: Uses FFprobe to inspect raw dimensions, Sample Aspect Ratio (SAR), Display Aspect Ratio (DAR), and rotation flags.
- **Distortion-Free Telegram Playback**: Supplies exact display dimensions (`width`, `height`) and `duration` metadata to Telegram's player, preventing black bars, letterboxing distortion, and stretched frames.
- **Aspect-Ratio Preserving Thumbnails**: Extracts crisp video cover previews from 10% into the stream, scaled precisely to match the source aspect ratio.

### ✍️ Interactive Custom Caption Engine
- **Rich-Text Entity Support**: Allows users to input customized video descriptions prior to download, preserving Telegram formatting (bold, italic, code, underline, strikethrough, and hyperlinked text).
- **Interactive Multi-Step State Flow**: Guided inline UX: Resolution Selection ➔ Custom Caption Prompt (with Skip option) ➔ Preview Confirmation ➔ Queue Submission.

### ⚡ Concurrency Control & Queue Management
- **Asyncio Worker Pool**: Dedicated background worker pool throttled by a global concurrency semaphore (`MAX_CONCURRENT_DOWNLOADS`).
- **Fair-Use Per-User Throttling**: Limits active concurrent downloads per user (`MAX_DOWNLOADS_PER_USER`) with real-time position notices.
- **Persistent SQLite Job Store**: Records every job lifecycle state (`QUEUED`, `EXTRACTING`, `DOWNLOADING`, `MERGING`, `UPLOADING`, `COMPLETED`, `FAILED`, `CANCELLED`) in an asynchronous SQLite database via SQLAlchemy, with automated WAL (Write-Ahead Logging) journal mode, `busy_timeout=10000`, and concurrent transaction support.
- **Stale Job Reconciliation**: Automatically cleans up and recovers orphaned in-flight tasks upon bot restarts.

### 📊 Real-Time Throttled Progress Dashboard
- **Live Transfer Metrics**: Dynamic Unicode progress bars (`████████░░░░░░░░ 50%`) with downloaded bytes, total file size, real-time speed in MB/s, and accurate ETA countdown.
- **Telegram Flood Protection**: Throttles status message edits (3-second cooldown) to eliminate Telegram `FloodWait` API penalties.

### 🛡️ Enterprise-Grade Security
- **Strict SSRF Mitigation**: Pre-resolves domain names and blocks loopback, link-local, private IP spaces (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `169.254.169.254`, `::1`, `fc00::/7`), and cloud metadata APIs.
- **Subprocess Argument Sanitization**: Invokes FFmpeg via safe argument lists (`asyncio.create_subprocess_exec`) with strict control-character stripping—never through shell string interpolation.
- **Token & Key Redaction**: Custom logging filter scrubs bot tokens, API hashes, session strings, and signed URLs from disk logs and terminal outputs.

---

## 🏗️ System Architecture

```mermaid
flowchart TD
    subgraph Telegram["Telegram Cloud"]
        User["Telegram User"] <--> BotAPI["Telegram Bot API"]
    end

    subgraph BotCore["Bunny Stream Downloader Engine"]
        BotAPI <--> Client["Pyrogram Async Client"]
        Client <--> Dispatcher["State & Handler Dispatcher"]

        Dispatcher --> StartHandler["/start, /help & Menu"]
        Dispatcher --> IntakeHandler["URL Intake & Validation"]
        Dispatcher --> QualityHandler["Quality Selector & Caption Flow"]
        Dispatcher --> AdminHandler["Admin Dashboard & Telemetry"]

        IntakeHandler --> SecValidator["SSRF & URL Security Validator"]
        IntakeHandler --> BunnyParser["Bunny Extractor (yt-dlp / HLS)"]

        QualityHandler --> QueueManager["Queue Manager & Worker Pool"]
        QueueManager <--> SQLiteDB[("SQLite Database (bot.db)")]

        QueueManager --> Workers["Async Worker Tasks"]
        Workers --> Downloader["Download Engine (HLS/Direct)"]
        Workers --> FFmpeg["FFmpeg Service (Remux & Widescreen SAR/DAR)"]
        Workers --> Uploader["Upload Service (Playable Video & Thumbnails)"]

        Uploader --> BotAPI
    end

    subgraph EdgeCDN["Bunny.net Infrastructure"]
        Downloader <--> BunnyCDN["Bunny Video Edge CDN (*.b-cdn.net)"]
    end
```

---

## 🔄 Video Processing Pipeline

```mermaid
sequenceDiagram
    autonumber
    actor User as User
    participant Bot as Telegram Bot
    participant Extractor as Bunny Extractor
    participant Queue as Queue Manager
    participant FFmpeg as FFmpeg Service
    participant TG as Telegram API

    User->>Bot: Submits Bunny Embed URL
    Bot->>Extractor: Resolve Video GUID & Master Playlist
    Extractor-->>Bot: Detected Qualities (e.g. 720p, 1080p)
    Bot->>User: Inline Quality Keyboard
    User->>Bot: Selects Quality (e.g. 1080p)
    Bot->>User: Prompts for Optional Custom Caption
    User->>Bot: Sends Formatted Caption or clicks [Skip]
    Bot->>User: Shows Confirmation Preview
    User->>Bot: Clicks [Start Download]
    Bot->>Queue: Submit Job (Acquire Concurrency Semaphore)
    Queue->>Bot: Update status: DOWNLOADING (live progress bar)
    Queue->>FFmpeg: Probe SAR/DAR, Remux MP4 & Generate Thumbnail
    FFmpeg-->>Queue: Validated 16:9 Widescreen MP4 + Thumbnail
    Queue->>TG: Upload playable video with exact dimensions & caption
    TG-->>User: Delivers completed streaming MP4 video
    Queue->>Bot: Clean temporary files & update DB to COMPLETED
```

---

## 📱 User Experience & Visual Interface

### Interactive Download Flow

```text
/start
   │
   ▼
🎬 BUNNY STREAM DOWNLOADER
Your Premium Video Download Assistant
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📥 Download authorized Bunny videos
🎞️ Lossless MP4 processing & FFmpeg remux
📐 True 16:9 widescreen video delivery
⚡ Non-blocking asynchronous worker queue
☁️ Direct Telegram streaming playback
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 System: Online
🟢 Downloader: Ready

[ 📥 Download Video ]
[ 📂 My Downloads ]   [ 📊 Status ]
[ ⚙️ Settings ]       [ ❓ Help ]
```

```text
User submits embed link:
https://iframe.mediadelivery.net/embed/12345/a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d
   │
   ▼
🎬 VIDEO DETECTED
📺 Title: Biology Lecture - Monosaccharides
⏱️ Duration: 00:27:04

Select desired download resolution:
[ 360p (140 MB) ]    [ 480p (230 MB) ]
[ 720p (580 MB) ]    [ 1080p (1.1 GB) ]
[ ⭐ Best Available ]
[ ❌ Cancel ]
```

```text
User selects [ 1080p ]:
   │
   ▼
✍️ CUSTOM CAPTION (OPTIONAL)
Selected: 1080p

Send your custom video caption now.
Rich text formatting (bold, italic, code, links) will be preserved.

[ ⏭️ Skip Caption ]  [ ⬅️ Change Quality ]
[ ❌ Cancel Download ]
```

```text
Live Throttled Progress Dashboard:
   │
   ▼
⬇️ DOWNLOADING VIDEO
🎬 Video: Biology Lecture - Monosaccharides
📺 Quality: 1080p

████████████░░░░ 68%

📦 Downloaded: 748 MB
📊 Total: 1.10 GB
⚡ Speed: 14.8 MB/s
⏳ ETA: 00:24

Status: Downloading stream segments...
```

---

## 📋 Prerequisites

| Component | Minimum Version | Recommended Version |
| :--- | :--- | :--- |
| **Operating System** | Ubuntu 22.04 LTS / Windows 10 | **Ubuntu 24.04 LTS** / Windows 11 |
| **Python** | 3.11 | **3.12+** |
| **FFmpeg** | 5.0 | **7.0+** (with `libx264`, `aac`, `ffprobe`) |
| **SQLite** | 3.35+ | **3.40+** (CLI utility recommended for backups) |
| **RAM** | 1 GB | **2 GB+** |
| **Disk** | 5 GB SSD free space | **25 GB+ NVMe SSD** |
| **Telegram Credentials** | Valid `API_ID` & `API_HASH` ([my.telegram.org](https://my.telegram.org)) + Bot Token ([@BotFather](https://t.me/BotFather)) |

---

## 🐧 Ubuntu 24.04 LTS Production Deployment (systemd)

Ubuntu 24.04 LTS (Noble Numbat) is the primary targeted server platform. The automated setup adheres strictly to modern Linux standards (including PEP 668 managed virtual environments).

### 1. Clone Repository & Run Automated Installer
```bash
# Navigate to deployment directory
cd /opt

# Clone your repository
sudo git clone https://github.com/NimeshPiyathissa/bunny-telegram-bot.git bunny-bot
cd bunny-bot

# Run the automated installer (installs system packages, creates .venv, configures runtime dirs)
sudo bash scripts/setup_ubuntu.sh
```

### 2. Configure Credentials
```bash
# Edit environment file with your credentials
sudo nano .env
```
Populate your `BOT_TOKEN`, `API_ID`, `API_HASH`, and `OWNER_ID`.

### 3. Install & Start systemd Daemon
```bash
# Install systemd service unit with auto-restart on boot
sudo bash scripts/install_service.sh

# Start the service
sudo systemctl start bunny-bot

# Verify service status
sudo systemctl status bunny-bot
```

### 4. Monitor Live System Logs
```bash
sudo journalctl -u bunny-bot -f
```

---

## 🐳 Docker & Docker Compose Deployment

The project provides a hardened, non-root multi-stage Docker image and compose definition.

### 1. Configure Environment
```bash
cp .env.example .env
nano .env
```

### 2. Build and Launch Containers
```bash
docker compose up -d --build
```

### 3. Service Management Commands
```bash
# Check container status and health
docker compose ps

# View live application logs
docker compose logs -f

# Restart container
docker compose restart

# Stop container
docker compose down
```

---

## 🪟 Windows 10/11 Development Setup

For local testing and development on Windows workstations:

### 1. Install Python & FFmpeg
1. Download **Python 3.12+** from [python.org](https://www.python.org/downloads/). Enable **"Add python.exe to PATH"**.
2. Install **FFmpeg** via Windows Package Manager:
   ```powershell
   winget install Gyan.FFmpeg
   ```

### 2. Run Automated Setup Script
```powershell
# Open PowerShell in project directory and execute:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup_windows.ps1
```

### 3. Configure Credentials & Launch
```powershell
notepad .env
.\.venv\Scripts\Activate.ps1
python bot.py
```

---

## 🛠️ DevOps & Maintenance Tooling

The `scripts/` directory contains hardened operational tools designed for production maintenance.

### 1. Automated Atomic Backups
Performs an online, non-blocking snapshot of SQLite (`data/bot.db`) and archives `.env` with restricted 600 permissions. Automatically prunes archives older than 30 days.
```bash
bash scripts/backup.sh
```

### 2. Safe Database Restore
Verifies backup archive integrity with `PRAGMA integrity_check`, safely stops the service to prevent database lockups, creates a safety snapshot of the active database, and performs an atomic restore.
```bash
bash scripts/restore.sh backups/bot_db_20261108_120000.db
```

### 3. Zero-Downtime Application Update
Safely pulls updates, updates dependencies, verifies database schema, and **executes the automated test suite** before restarting the systemd service. If tests fail, update halts without restarting the service.
```bash
bash scripts/update.sh
```

### 4. System Health Diagnostics
Inspects process status, database responsiveness, partition disk space thresholds, FFmpeg availability, and recent error log frequency.
```bash
bash scripts/healthcheck.sh
```

---

## ⚙️ Environment Configuration Reference

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `BOT_TOKEN` | String | *Required* | Telegram Bot API token obtained from [@BotFather](https://t.me/BotFather) |
| `API_ID` | Integer | *Required* | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | String | *Required* | Telegram API Hash from [my.telegram.org](https://my.telegram.org) |
| `OWNER_ID` | Integer | *Required* | Numeric Telegram ID of primary bot administrator |
| `ADMIN_IDS` | String | `""` | Comma-separated list of secondary admin IDs (e.g. `111,222`) |
| `DATABASE_URL` | String | `sqlite+aiosqlite:///./data/bot.db` | Async SQLAlchemy database connection URL |
| `MAX_CONCURRENT_DOWNLOADS` | Integer | `2` | Maximum global worker tasks downloading simultaneously |
| `MAX_DOWNLOADS_PER_USER` | Integer | `1` | Maximum concurrent active download jobs allowed per user |
| `MAX_FILE_SIZE_MB` | Integer | `1900` | File size ceiling in MB (Telegram default maximum: 2000 MB) |
| `DOWNLOAD_TIMEOUT_SECONDS` | Integer | `3600` | Maximum allowed runtime per media download before timing out |
| `UPLOAD_TIMEOUT_SECONDS` | Integer | `1800` | Maximum allowed runtime for Telegram video upload |
| `AUTO_CLEANUP` | Boolean | `true` | Enables scheduled background garbage collection of orphan temporary files |
| `CLEANUP_AFTER_UPLOAD` | Boolean | `true` | Automatically deletes local MP4, thumbnails, segments, and temp workspace after confirmed delivery |
| `FAILED_JOB_RETENTION_HOURS` | Integer | `24` | Hours to retain failed/uncompleted download workspaces for recovery/inspection |
| `TEMP_FILE_RETENTION_HOURS` | Integer | `6` | Hours to retain abandoned temporary files before automatic garbage collection |
| `MIN_FREE_DISK_GB` | Integer | `3` | Minimum free disk space in GB required before rejecting new jobs or triggering emergency cleanup |
| `DOWNLOAD_DIRECTORY` | String | `./downloads` | Directory where active video files are downloaded |
| `TEMP_DIRECTORY` | String | `./temp` | Directory where per-job isolated workspaces are created |
| `BACKUP_DIRECTORY` | String | `./backups` | Directory where database and config backups are archived |
| `LOG_LEVEL` | String | `INFO` | Application log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `ENABLE_USER_SESSION` | Boolean | `false` | Enable Pyrogram user session for uploading files up to 4 GB |
| `SESSION_STRING` | String | `""` | String session token when `ENABLE_USER_SESSION` is true |

---

## 🧹 Storage Lifecycle & Automatic Cleanup Architecture

The bot employs a safe, non-destructive storage management architecture designed for VPS and local environments:

1. **Isolated Per-Job Workspaces**:
   Every download job executes in an isolated workspace (`temp/job_{job_id}/`). Intermediate HLS segments, converted video/audio streams, and preview thumbnails are stored exclusively inside this folder.
2. **Delivery Confirmation Before Deletion**:
   Local media files are deleted **only after Telegram's API returns a valid Message object** confirming that the video was received by Telegram's cloud servers. The Telegram message ID and file ID are persisted to `data/bot.db`.
3. **Strict Message Preservation**:
   The bot **never** calls `delete_messages` or deletes delivered Telegram messages during local cleanup.
4. **Protected System & User Files**:
   Cleanup operations are strictly bounded to `temp/` and `downloads/`. The application database (`data/bot.db`), Telegram sessions (`*.session*`), `.env`, and source files are immutably excluded from deletion.
5. **Cross-Platform Lock Handling**:
   On Windows workstations, file locks (`PermissionError` / `WinError 32`) are handled via automated garbage collection (`gc.collect()`), read-only flag removal, and exponential retry backoff.
6. **Bounded Upload Retries & Uncertain Delivery**:
   Upload failures trigger up to 3 retry attempts with exponential backoff. If a connection times out while delivery status is uncertain, the bot avoids blind re-sends to prevent duplicate Telegram messages and flags the job in a `RECOVERY` state.
7. **Disk Space Threshold & Emergency Cleaning**:
   Before initiating downloads, free disk capacity is validated against `MIN_FREE_DISK_GB`. If storage is low, emergency garbage collection is dispatched to prune old abandoned workspaces.

---

## 🎮 Command Reference

### User Commands
- `/start` — Initialize bot session, show welcome panel, and open interactive keyboard menu.
- `/download [url]` — Enter download state or directly process an authorized Bunny embed URL.
- `/status` — View in-flight download jobs and live progress metrics for the current user.
- `/cancel` — Gracefully cancel the active job belonging to the caller.
- `/help` — Display comprehensive usage guide, instructions, and supported link formats.

### Admin Commands (Restricted to `OWNER_ID` & `ADMIN_IDS`)
- `/admin` — Open interactive admin control dashboard with system telemetry.
- `/stats` — Query CPU load, memory utilization, disk space, and total download count.
- `/users` — Display user registration statistics and active accounts.
- `/jobs` — Inspect real-time status of all active and queued background jobs.
- `/broadcast <msg>` — *(Owner Only)* Dispatch broadcast notification to all registered users.
- `/ban <user_id>` — Ban a user from submitting downloads.
- `/unban <user_id>` — Revoke ban status for a user.
- `/restart` — *(Owner Only)* Initiate graceful shutdown for systemd/Docker auto-restart.

---

## 🔒 Security Defenses & Access Control

1. **Server-Side Request Forgery (SSRF) Defense**:
   - Every input URL is validated against strict regex patterns.
   - Hostnames are resolved to IP addresses via DNS prior to connection.
   - Requests directed at loopback (`127.0.0.0/8`, `::1`), private subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local addresses (`169.254.0.0/16`), or cloud instance metadata servers are rejected immediately.
2. **Subprocess Injection Immunity**:
   - FFmpeg and FFprobe calls are dispatched through `asyncio.create_subprocess_exec` passing arguments as immutable arrays.
   - Non-printable control characters, null bytes, and path traversal tokens are sanitized.
3. **Secret Redaction**:
   - `SecretFilter` logging middleware automatically scrubs Telegram bot tokens, API hashes, session strings, and signed token queries before records reach logs or console streams.
4. **Filesystem Isolation**:
   - The systemd unit enables `PrivateTmp=true`, `ProtectSystem=full`, `ProtectHome=read-only`, and drops unnecessary Linux capabilities (`NoNewPrivileges=true`).

---

## 🧪 Automated Testing & QA

The project maintains an asynchronous test suite built on `pytest` and `pytest-asyncio`.

```bash
# Activate virtual environment
source .venv/bin/activate   # or .\.venv\Scripts\Activate.ps1 on Windows

# Execute full test suite
pytest -v --tb=short
```

### Test Suite Coverage:
- **`tests/test_aspect_ratio.py`**: Validates 16:9 widescreen detection, Display Aspect Ratio (DAR), Sample Aspect Ratio (SAR), rotation correction, and thumbnail aspect preservation.
- **`tests/test_caption.py`**: Validates interactive caption state flow, formatted entity preservation (bold, italic, code, underline), skip options, and edit cycles.
- **`tests/test_urls.py`**: Validates Bunny embed URL pattern matching, GUID extraction, parameter sanitization, and invalid domain rejection.
- **`tests/test_extractor.py`**: Validates yt-dlp format detection, HLS manifest parsing, and 403 Forbidden handling.
- **`tests/test_download.py`**: Tests FFmpeg lossless remuxing, MP4 `ftyp` magic bytes validation, and document fallback.
- **`tests/test_queue.py`**: Verifies worker pool concurrency limits, per-user limits, cancellation events, and stale job startup reconciliation.
- **`tests/test_security.py`**: Tests SSRF blocks against private IP spaces, cloud metadata addresses, and command injection attacks.
- **`tests/test_handlers.py`**: Tests HTML parser compatibility with Telegram message length constraints and menu routing.

---

## ⚖️ Compliance & Responsible Use

- **Authorized Media Ingestion**: This software is intended exclusively for content creators, platform operators, and developers downloading media assets they own or have explicit legal authorization to access.
- **Access Control Integrity**: This application **does not** crack tokens, bypass DRM encryption, or circumvent access control measures. If a Bunny Stream asset returns `403 Forbidden` due to token restrictions, referrer checks, or DRM, the bot gracefully reports that proper owner credentials or signed URLs are required.

---

## 📄 Contributing & License

Contributions, feature requests, and bug reports are welcome! Please open an issue or pull request on GitHub.

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for complete details.
