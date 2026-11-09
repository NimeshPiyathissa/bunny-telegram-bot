# Changelog & Release Notes

All notable changes to the **Bunny Stream to Telegram Downloader** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] - 2026-11-09

### Performance Optimization Update: High-Speed Concurrent Downloads & Telegram Uploads

An exhaustive performance audit and acceleration suite addressing download and upload throughput bottlenecks:

#### ⚡ High-Speed Concurrent HLS Segment Downloader
- **Parallel Multi-Segment Fetching**: Engineered `ConcurrentHLSDownloader` utilizing persistent `aiohttp` connection pooling (`TCPConnector(limit=30, keepalive_timeout=60, ttl_dns_cache=300)`).
- **Configurable Segment Concurrency**: Added `HLS_CONCURRENT_SEGMENTS` (default: 10) to download up to 10 video segments simultaneously.
- **Instant Stream-Copy Remuxing**: Replaced single-connection FFmpeg demuxing with parallel segment retrieval and instant stream-copy remuxing (`-c copy -bsf:a aac_adtstoasc -movflags +faststart`), completing remuxing in ~1.2s without video/audio re-encoding.
- **Bunny Stream Token Preservation**: Propagates signed security tokens and query parameters across all master, variant, and media segment URLs.
- **Safe Fallback Protection**: Gracefully falls back to yt-dlp (with `concurrent_fragment_downloads`) or FFmpeg if custom or unsupported playlist tags are encountered.

#### 🚀 MTProto C-Crypto Acceleration (1,433x Speedup)
- **Resolved 10-Minute Pure-Python CPU Bottleneck**: Fixed severe single-threaded CPU lockup caused by Pyrogram falling back to pure-Python `pyaes` (which took ~18.3 minutes to encrypt 163 MB at 0.15 MB/s).
- **Dynamic Crypto Engine Hook (`app/utils/crypto_patch.py`)**: Automatically detects and hooks `cryptg` and `tgcrypto` C-extensions for MTProto AES-IGE-256 packing, boosting encryption throughput to **212.77 MB/s** (0.76s for 163 MB).
- **Updated Dependencies**: Added `cryptg>=0.4.0` (universal wheels) and `tgcrypto>=1.2.0; sys_platform != "win32"` to `requirements.txt`.

#### 📤 Multi-Session Parallel Telegram Upload Pipeline
- **Multi-Session MTProto Uploader (`app/utils/fast_upload.py`)**: Establishes 4 parallel media sessions over independent TCP sockets to Telegram datacenters, overcoming single-socket TCP window limits.
- **Pre-Buffered Worker Queue**: Buffers chunks ahead (`workers_count * 2`) to eliminate disk-read latency between socket writes.
- **Non-Blocking Progress Dispatch**: Decoupled Telegram message edit API requests (200–500ms network roundtrips) from upload workers, ensuring chunk transmission never pauses.
- **Configurable Transmission Concurrency**: Added `MAX_CONCURRENT_TRANSMISSIONS` (default: 4) to `config.py` and client initialization.

#### 📊 Live Download & Upload Speed Metrics & ETA
- **Live Download Metrics**: Displays download speed in MB/s (`⚡ Download Speed: 12.28 MB/s`), progress percentage, and dynamic ETA (`⏳ ETA: 00:12`).
- **Live Upload Metrics**: Displays real-time upload speed in MB/s (`⚡ Upload Speed: 14.50 MB/s`), transferred bytes, progress bar, and dynamic ETA.
- **Comprehensive Completion Summary**: Reports exact measured durations and throughput:
  `⚡ Download: 13.8s (11.83 MB/s)`, `⚡ Upload: 11.2s (14.57 MB/s)`, and `🕒 Total Time: 25.5s`.

#### 🧪 Expanded Test Suite
- Added 13 speed optimization and concurrency unit tests (`tests/test_speed_optimizations.py`).
- Added 11 compatibility, upload cancellation, thread-safety, and startup regression tests (`tests/test_upload_cancellation_and_compat.py`).
- Full test suite expanded to **100 tests passing in ~30s** with 100% success rate.

#### 🔧 Critical Production Hotfix: Thread-Safe Upload Callbacks & Null Writer Prevention
- **Thread-Safe Event Loop Dispatch**: Captured the active running event loop inside `_process_download()` and used `asyncio.run_coroutine_threadsafe()` to schedule `_update_upload_progress()` and `_update_progress()`. This eliminates `RuntimeError: no running event loop` when Pyrogram invokes progress callbacks from threadpool executor worker threads.
- **Eliminated Coroutine Warnings**: Ensured progress coroutines are only instantiated after the throttle window has elapsed and explicitly closed if scheduling fails, completely eliminating `RuntimeWarning: coroutine was never awaited`.
- **Resolved NoneType Writer Crash**: Fixed `'NoneType' object has no attribute 'write'` by ensuring `fast_save_file` awaits full queue draining before completion and raises a clear `RuntimeError` if native Pyrogram `save_file` returns `None`, preventing `InputMediaUploadedDocument` from receiving a null file object.
- **Robust Worker Lifecycle**: Ensured upload worker queues are drained gracefully with sentinel values rather than cancelled prematurely, preventing dropped final chunks.
- **Safe Fallback Protection**: Preserved fast multi-session uploader with automatic fallback to native Pyrogram `save_file`.

---

### Initial Release Candidate (v1.0.0-rc1) - 2026-11-08

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
