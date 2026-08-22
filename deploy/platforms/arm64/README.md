# arm64 (future)

> Stub for ARM64 hosts (e.g., Raspberry Pi 5, Apple Silicon via UTM).

- Build `docker` images with `--platform linux/arm64`.
- Playwright `chromium` not `firefox` on arm64 (no firefox arm64 build).
- Reuse `i386-debian-amd64-container/env.sh` logic with `PC_DEVICE_ID=arm64-*`.

Fill when hardware available — keep as backlog item `PCC-BF-ARM64`.
