# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Renamed sinter to cupola (package, module, CLI, docs); issue #3.

### Added
- **RAM disk management** — copy models to/from tmpfs for fast loading
  - `cupola ramdisk status` — show RAM disk usage and quality
  - `cupola ramdisk list` — list models on RAM disk
  - `cupola ramdisk up <profile>` — copy model to RAM disk
  - `cupola ramdisk down <profile>` — remove model from RAM disk
  - Configurable via `[ramdisk]` section in config.toml
  - Supports `CUPOLA_RAMDISK_PATH` environment variable override
- **Sentinel telemetry** — session-scoped temperature, power, energy monitoring
- Energy measurement tiers (wall-meter, hwmon counter, power integration, assumed)
- Optional electricity cost and CO₂e estimates via `[accounting]` config
- Thermal policy with advisory warnings and optional critical stop
- `cupola telemetry` command (one-shot probe, session summary, garbage collection)
- `cupola status` now includes live telemetry quality and readings
- `cupola doctor` includes one-shot sensor probe
- Backend version and feature detection
- Context size benchmarking (`cupola bench`)
- Backend update management (`cupola update --backend`)
- Compatibility validation in `cupola validate`
- Enhanced `cupola doctor` with feature detection

### Changed
- Improved error messages and diagnostics
- Updated hardware probing for better ROCm support

## [0.1.0] - 2025-09-20

### Added
- Process supervision with finite state machine
- Memory admission checks with safety reserves
- Hardware probing (`cupola doctor`)
- Profile validation (`cupola validate`)
- Resource planning (`cupola plan`)
- Backend lifecycle management (`cupola up`/`down`)
- Instance status reporting (`cupola status`)
- Sandboxed command execution (`cupola exec`)
- Interactive setup wizard (`cupola setup`)
- Comprehensive test suite (129 tests)