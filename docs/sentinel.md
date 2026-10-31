# Sentinel — Session Telemetry

Sentinel provides session-scoped, out-of-band telemetry for Cupola-managed `llama-server` processes on AMD ROCm workstations. It measures temperature, power, energy consumption, and optionally estimates electricity cost and carbon emissions.

## Features

- **Passive monitoring**: No HTTP proxying or in-path sampling on the inference request path
- **Session-scoped**: Samples only while a verified Cupola instance is running
- **Measurement tiers**: Transparent quality indicators for energy measurements
- **Thermal policy**: Advisory warnings with optional critical stop
- **Local estimates only**: Cost and CO₂e are operational estimates, not ESG accounting

## Measurement Tiers

| Tier | Source | Quality |
|------|--------|---------|
| 1 | Wall-meter file/counter (user-configured) | Metered |
| 2 | GPU hwmon `energy1_input` (µJ) | Metered |
| 3 | GPU hwmon `power1_average` integration | Estimated |
| 4 | Explicit `assume_power_w` | Estimate |
| 0 | No power/energy data | Unavailable |

## Configuration

Add to `~/.config/cupola/config.toml`:

```toml
[sentinel]
sample_hz = 1.0
warn_hotspot_c = 95
critical_hotspot_c = 105
critical_hold_s = 8
auto_stop_on_critical = false
# assume_power_w = 150.0  # Only if no power sensor
# wall_energy_path = "/path/to/counter"

[accounting]
currency = "USD"
electricity_tariff_per_kwh = 0.12
emissions_region = "US"
emissions_factor_kgco2e_per_kwh = 0.40
emissions_factor_source = "user-configured"
emissions_factor_version = "2024-01"
allow_assumed_power = false
```

## CLI Commands

```bash
# One-shot sensor probe
cupola telemetry

# JSON output
cupola telemetry --json

# Last session summary
cupola telemetry --session

# Garbage collect old telemetry (>14 days)
cupola telemetry --gc

# Status includes telemetry
cupola status
```

## What Sentinel Is Not

- **Not Scope 1/2/3 accounting**: No supplier emissions, no product LCA
- **Not ESG disclosure**: No ESRS, ISSB, or offset features
- **Not automatic**: Default policy is advisory; auto-stop is opt-in
- **Not a benchmark**: Compass handles benchmarking separately

## RAM Disk Management

Cupola supports moving LLM model weights to a RAM disk (tmpfs) for faster loading by llama-server.

### Configuration

Add to `~/.config/cupola/config.toml`:

```toml
[ramdisk]
path = "/mnt/ai_ramdisk"
enabled = true
warn_used_pct = 80
critical_used_pct = 90
```

Or use environment variable: `CUPOLA_RAMDISK_PATH=/mnt/ai_ramdisk`

### CLI Commands

```bash
# Show RAM disk status
cupola ramdisk status

# List models on RAM disk
cupola ramdisk list

# Copy model to RAM disk
cupola ramdisk up coding

# Remove model from RAM disk
cupola ramdisk down coding
```

### How It Works

1. Model weights are copied from persistent storage to the RAM disk
2. llama-server loads the model from the RAM disk (faster I/O)
3. When done, models can be removed from the RAM disk to free space

### Monitoring

RAM disk status is included in `cupola doctor` and `cupola status` output, showing:
- Total size, used space, available space
- Usage percentage
- Quality assessment (ok/degraded/unavailable)
- List of models currently on the RAM disk
