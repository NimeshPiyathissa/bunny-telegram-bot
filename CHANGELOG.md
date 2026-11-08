# Changelog & Release Notes

All notable changes to the **Bunny Stream to Telegram Downloader** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] - 2026-11-08

### Initial Release Candidate (v1.0.0-rc1)

Welcome to the initial public release of **Bunny Stream to Telegram Downloader**, an enterprise-grade asynchronous Telegram bot engineered to ingest authorized Bunny Stream media embeds, remux adaptive audio/video tracks, and deliver crystal-clear MP4 videos directly to Telegram.

### Key Highlights & Features

#### 🎬 Bunny Stream Video Ingestion
- **Embed URL Parsing**: Full support for standard Bunny embed URLs (`https://iframe.mediadelivery.net/embed/{library_id}/{video_guid}`).
- **Master HLS Playlist Analysis**: Parses and inspects `.m3u8` playlists and direct MP4 endpoints on Bunny CDN edge infrastructure (`*.b-cdn.net`, `vz-cdn.net`).
- **Dynamic Quality Matrix**: Automatically probes source streams and presents users with an inline keyboard offering detected resolutions (360p, 480p, 720p, 1080p, 1440p, 4K) or Best Available.
- **Strict DRM & Access Compliance**: Gracefully detects protected streams (403 Forbidden / Token Required) and informs operators rather than attempting circumvention.

#### 📐 16:9 Native Aspect Ratio & Widescreen Optimization
- **Display Aspect Ratio (DAR) Extraction**: Probes Sample Aspect Ratio (SAR), DAR, video stream dimensions, and rotation tags via FFprobe.
- **Exact Geometry Delivery**: Passes exact width, height, and duration to Telegram's video player, ensuring true 16:9 widescreen playback on mobile, desktop, and web clients without black bars or letterboxing distortion.
- **Aspect-Preserving Thumbnails**: Automatically generates preview thumbnails from 10% into the video stream matched to the exact display aspect ratio.

#### ✍️ Interactive Custom Caption Formatting
- **Inline Caption Editor**: Prompts users for an optional custom caption prior to starting the download.
- **Formatting Entity Preservation**: Retains bold, italics, code, strikethrough, underline, and links formatting entered via Telegram's rich text editor.
- **Workflow State Engine**: Seamless interactive flow with Quality Selection -> Caption Prompt -> Preview Confirmation -> Queue Submission.

#### 🧹 Storage Management & VPS Cleanup Lifecycle
- **Isolated Per-Job Workspaces**: Each job operates within an isolated workspace directory (`temp/job_{job_id}/`), preventing cross-job file collisions and segment leakage.
- **Confirmed Delivery Cleanup**: Local MP4, thumbnails, segments, and workspaces are deleted only after Telegram API confirms receipt with a valid Message object.
- **Zero Telegram Message Deletion**: The bot strictly preserves delivered Telegram messages and never calls `delete_messages` during cleanup.
- **Protected Files**: Databases (`data/bot.db`), Telegram sessions (`*.session*`), `.env`, and source code are immutably excluded from cleanup.
- **Cross-Platform Lock Handling**: Safely handles Windows file locks (`PermissionError` / `WinError 32`) through automated garbage collection (`gc.collect()`), read-only attribute stripping, and exponential backoff retries.
- **Bounded Upload Retries**: Retries failed uploads up to 3 times with exponential backoff without prematurely deleting retryable media.
- **Uncertain Delivery Protection**: If network connectivity drops while delivery status is uncertain, the bot transitions to a `RECOVERY` state rather than creating duplicate Telegram messages.
- **Disk Usage Guard & Thresholds**: Configurable storage options (`AUTO_CLEANUP`, `CLEANUP_AFTER_UPLOAD`, `FAILED_JOB_RETENTION_HOURS`, `TEMP_FILE_RETENTION_HOURS`, `MIN_FREE_DISK_GB`) with automated emergency cleanup when free space falls below thresholds.

#### ⚡ Asynchronous Concurrency & Queue Management
- **Asyncio Worker Pool**: Dedicated background worker pool with global concurrency semaphore (`MAX_CONCURRENT_DOWNLOADS`).
- **Per-User Throttling**: Enforces fair-use download limits per user (`MAX_DOWNLOADS_PER_USER`).
- **Database Job Persistence**: Asynchronous SQLite persistence with SQLAlchemy and aiosqlite tracking active, queued, and completed jobs, featuring automatic WAL journal mode initialization, busy_timeout=10000, and non-blocking concurrent read/write transactions.
- **Startup Reconciliation**: Detects and resolves stale, orphaned jobs if the bot restarts mid-process.

#### 📊 Live Throttled Progress Reporting
- **Real-Time Visual Indicators**: High-contrast Unicode progress bar with downloaded bytes, total file size, network transfer speed (MB/s), and estimated completion time (ETA).
- **Anti-Flood Rate Limiting**: Built-in 3.0s edit throttle window preventing Telegram Bot API `FloodWait` penalties.

#### 🛡️ Bank-Grade Security & Reliability
- **SSRF Defenses**: Full DNS pre-resolution blocking loopback, link-local, private IPv4/IPv6 ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `169.254.169.254`, `::1`, `fc00::/7`), and cloud metadata services.
- **Subprocess Injection Defense**: FFmpeg arguments passed strictly as structured arrays with sanitization (`shell=False`).
- **Credential Redaction**: Application logging intercepts and redacts Telegram bot tokens, API hashes, session strings, and signed URLs.

#### 🐧 Production VPS & Container Deployment
- **Ubuntu 24.04 & 22.04 LTS Ready**: Automated 15-step installer (`setup_ubuntu.sh`) adhering to PEP 668 managed environments, checking CPU, RAM, disk, and network, with an interactive first-time setup wizard.
- **systemd Service Daemon**: Production service installer with automatic restart (`Restart=always`, `RestartSec=5s`), resource limits (`LimitNOFILE=65535`), and sandboxing (`PrivateTmp=true`, `ProtectSystem=full`).
- **DevOps Script Suite**: Safe atomic backups (`backup.sh`), integrity-checked restores (`restore.sh`), zero-downtime updates (`update.sh`), and comprehensive system diagnostics (`healthcheck.sh`).
- **Docker & Compose**: Hardened non-root Dockerfile (`python:3.12-slim`), health checks, resource ceilings, and secure `.dockerignore`.
- **GitHub Actions CI**: Automated test workflows (`.github/workflows/tests.yml` & `ci.yml`) testing across Python 3.11 and 3.12 on Ubuntu runners.
- **Comprehensive QA**: 76 automated unit, integration, aspect ratio, concurrency, and release verification tests passing with 100% success rate.
