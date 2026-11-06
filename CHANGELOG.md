# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Renamed cupola to cupola (package, module, CLI, docs); issue #3.

### Added
- **GPU observability and throttling detection** — a Prometheus endpoint inside
  Cupola plus a provisioned Grafana/Prometheus stack, for the failures an
  HTTP-layer dashboard cannot see (ADR-004)
  - `cupola metrics [--serve] [--json] [--bind HOST] [--port PORT]` — stdlib
    text exposition on `127.0.0.1:9817`, no new dependency
  - DCGM-aligned names where the ASIC answers: junction/edge/memory temperature
    against the chip's own critical limit, sclk/mclk plus the highest static
    `pp_dpm_sclk` state as the ceiling, power against the enforced cap, VRAM and
    GTT totals, PCIe negotiated versus capable link speed and width, AER error
    totals
  - Derived throttle evidence (`sinter_throttle_flag_info`) in place of the
    throttle-reason register this hardware does not have: clock below ceiling
    while busy, power at cap, hotspot at critical, link downgraded,
    uncorrectable AER
  - Gaps are metrics, not zeros — `sinter_metric_unavailable_info{name, reason}`
    declares SM occupancy and PCIe TX/RX byte counters as unmeasurable on
    `amdgpu` rather than reporting them as zero
  - VRAM failure detection from both sides: allocation headroom and GTT spill
    from the sentinel, plus counters for OOM signatures in backend logs and for
    supervised exits with no stop request
  - Event counters persist to `<state_dir>/metrics/events.json` so `increase()`
    survives an exporter restart; losing that durability publishes
    `sinter_ledger_persist_error`
  - `observability/` — `docker-compose.yml` (host networking, loopback-published
    UIs), scrape config, 13 alert rules, and the *Cupola GPU throttling*
    dashboard, which includes a panel listing what is measured nowhere
  - `docs/observability.md` — DCGM mapping, gap register, alert rationale,
    systemd user unit, measured baseline
- **Software Carbon Intensity (SCI)** — `SCI = (E x I + M) / R` per verified
  task, per 1k tokens, or per session hour
  - `cupola sci [--report PATH] [--json]` — the carbon number and its provenance
  - `cupola telemetry --session` now includes the session SCI block
  - Time-resolved grid intensity via `[carbon]` providers: `static`, `file`,
    `neso-gb` (GB, half-hourly, no key), `energidataservice-dk` (DK1/DK2, 5 min,
    no key); eight more documented and selectable later
  - Hourly matching: `I_eff` is consumption-weighted over the run window, with
    resolution and coverage recorded
  - Benchmark reports gain `metrics.carbon` (provenance, energy tiers, green
    principles) and `sci_gco2e_per_verified_task` / `_per_success` /
    `_per_1k_tokens`; `compare` reports SCI deltas and marks them not comparable
    across differing methodologies
  - Green-software principles are `pass` only with evidence; unknown is
    `unproven`, never green
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
- **Fixed**: `[sentinel] wall_energy_path` was accepted but never read, so tier-1
  (metered) energy was unreachable; `wall_energy_unit` now declares the counter
  unit and tier 1 is preferred when configured

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