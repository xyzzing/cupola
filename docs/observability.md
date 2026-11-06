# Observability: DCGM-shaped metrics on an AMD GPU

A dashboard of HTTP 200s cannot see a GPU that is being held back. The request
still succeeds while clocks drop, the link renegotiates to a lower generation,
or the frame buffer fills until the backend dies between requests. This page
documents what Cupola measures on AMD hardware, what it deliberately does **not**
measure, and how the alert rules turn the difference into a page.

NVIDIA's DCGM is the reference shape for this kind of dashboard. DCGM cannot run
here: it is a proprietary agent over NVML, and there is no NVML on `amdgpu`. So
the exporter publishes **DCGM-aligned metric names and semantics** where the
hardware supports them, and declares a gap where it does not. A gap is a metric
that says "not measurable on this ASIC, because …" — never a zero.

```
amdgpu sysfs ─┐
sentinel      ├─> cupola metrics --serve ──> Prometheus ──> Grafana dashboard
  telemetry   │        127.0.0.1:9817          127.0.0.1:9091     127.0.0.1:3000
backend logs  │                                    │
state.json  ──┘                                    └──> 13 alert rules
```

## Running it

```bash
cupola metrics                    # print the exposition once, no server
cupola metrics --json             # same samples, machine-readable
cupola metrics --serve            # serve on 127.0.0.1:9817
cupola metrics --serve --port 9917
```

`[metrics]` in `config.toml` (all keys optional; these are the defaults):

```toml
[metrics]
enabled = true
bind = "127.0.0.1"          # loopback. There is no auth on this endpoint.
port = 9817
log_tail_max_bytes = 262144  # per-scrape cap when scanning backend logs
oom_patterns = ["out of memory", "failed to allocate", "could not allocate",
                "bad_alloc"]
```

The endpoint is stdlib `http.server` inside Cupola. There is no second process to
install, no new dependency, and no client path in between — Cupola is an
out-of-band supervisor (ADR-002), and the exporter stays out of band too.

## The metric contract

Three rules hold everywhere in this stack, and they are the reason the numbers
can be trusted:

1. **A missing measurement is never `0`.** Absent sysfs nodes, an unreachable
   backend, a counter the driver reports as `N/A` — all of them produce a
   `sinter_metric_unavailable_info{name=…,reason=…}` row and no series for the
   metric. A zero is only ever emitted for a real zero (no errors seen, no OOM
   seen).
2. **Identity lives in labels.** `*_info` metrics are constant `1`s whose labels
   carry the state (`flag="power_at_cap"`, `quality="unavailable"`). They are
   declared `gauge`, not `info`: `# TYPE x info` is OpenMetrics, and Prometheus
   rejects the whole scrape over it.
3. **Counters survive restarts.** Event counts are written to
   `<state_dir>/metrics/events.json`, so `increase()` over an exporter restart
   is still correct. If that write fails, the scrape degrades instead of raising
   and publishes `sinter_ledger_persist_error 1` — durable counters are a
   feature, not an assumption.

### Metrics

| Metric | Type | Labels | Source |
| --- | --- | --- | --- |
| `sinter_gpu_temperature_celsius` | gauge | `sensor="edge\|junction\|mem"` | hwmon `tempN_input`, keyed by `tempN_label` |
| `sinter_gpu_temperature_critical_celsius` | gauge | `sensor=…` | hwmon `tempN_crit` — the chip's own limit |
| `sinter_gpu_busy_percent` | gauge | | DRM `gpu_busy_percent` |
| `sinter_mem_busy_percent` | gauge | | DRM `mem_busy_percent` |
| `sinter_clock_mhz` | gauge | `clock="sclk\|mclk\|sclk_max"` | hwmon `freqN_input`; ceiling = highest unmarked state in `pp_dpm_sclk` |
| `sinter_power_watts` | gauge | | hwmon `power1_average` |
| `sinter_power_cap_watts` | gauge | | hwmon `power1_cap` |
| `sinter_vram_used_bytes`, `sinter_vram_total_bytes` | gauge | | DRM `mem_info_vram_*` (device view) |
| `sinter_vram_available_bytes`, `sinter_vram_headroom_bytes` | gauge | | sentinel `telemetry/current.json` (allocation view) |
| `sinter_gtt_used_bytes`, `sinter_gtt_total_bytes` | gauge | | DRM `mem_info_gtt_*` |
| `sinter_gtt_spill_detected`, `sinter_pacing_active` | gauge | | sentinel telemetry |
| `sinter_pcie_link_speed_gts`, `sinter_pcie_link_width` | gauge | `limit="current\|max"` | PCI `current_link_*`, `max_link_*` |
| `sinter_pcie_aer_errors_total` | counter | `type="correctable\|fatal\|nonfatal"` | PCI `aer_dev_*` tables, `TOTAL_ERR_*` rows |
| `sinter_backend_up`, `sinter_backend_state_info` | gauge | `state=…` | `state.json` + a liveness check on the recorded pid |
| `sinter_instance_info` | gauge | `instance_id`, `profile` | `state.json` |
| `sinter_backend_launches_total` | counter | | counted when Cupola spawns a backend |
| `sinter_backend_unexpected_exits_total` | counter | | a supervised pid died with no stop request |
| `sinter_vram_oom_events_total` | counter | `source="backend_log\|process_exit"` | OOM signatures in backend logs; abnormal exit |
| `sinter_scrape_error_total` | counter | | a probe raised during this scrape |
| `sinter_sensor_quality_info` | gauge | `quality="ok\|degraded\|unavailable"` | overall probe quality |
| `sinter_throttle_flag_info` | gauge | `flag=…` | derived, see below |
| `sinter_metric_unavailable_info` | gauge | `name`, `reason` | the gap register |
| `sinter_ledger_persist_error` | gauge | | counters could not be written |
| `sinter_exporter_info` | gauge | `version` | build identity |

Both VRAM views are exported on purpose. The device view says what the GPU
thinks is used; the allocation view says what the serving process thinks it has
left. They diverge exactly when a model starts spilling into system memory, and
that divergence is the signal the VRAM alerts watch.

### Gap register

These are emitted on every scrape, on this ASIC:

| Declared gap | Why |
| --- | --- |
| `sinter_sm_occupancy_percent` | No CU occupancy counter. `sinter_gpu_busy_percent` is the proxy: whether work was submitted to the engine, not how much of the SM/CU array the kernel filled. |
| `sinter_pcie_tx_bytes_total` | `amdgpu` exposes no PCIe TX/RX byte counters; `amd-smi` reports `current_bandwidth_sent` as `N/A`. |
| `sinter_pcie_rx_bytes_total` | Same node, receive side. |
| `sinter_gpu_throttle_reason_bits` | No throttle-reason accumulator. Read `sinter_throttle_flag_info` instead. |

Per-scrape gaps are named too: if a node is missing on the host, `missing`
(`busy`, `clock`, `power_cap`, `pcie_link`, `aer`) produces
`sinter_metric_unavailable_info{name="sinter_sensor_<x>", reason="sysfs node
absent or unreadable on this host"}`. Occupancy and PCIe byte counters would
need a profiling collector (`rocprofv3`, counter collection), which is out of
scope: it needs a second GPU-consuming process and changes what it measures.

## DCGM mapping

Read this table as "what the NVIDIA dashboard would show, and what replaces it
here". Field names are DCGM's; the cupola side is what this exporter emits.

| DCGM field | Cupola | Status here |
| --- | --- | --- |
| `DCGM_FI_DEV_GPU_TEMP` | `sinter_gpu_temperature_celsius{sensor="junction"}` | available |
| `DCGM_FI_DEV_MEMORY_TEMP` | `{sensor="mem"}` | available |
| — (no DCGM equivalent) | `sinter_gpu_temperature_critical_celsius` | available; AMD exposes the critical limit, which is what makes a relative alert possible |
| `DCGM_FI_DEV_GPU_UTIL` | `sinter_gpu_busy_percent` | proxy, not equivalent |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | `sinter_mem_busy_percent` | proxy |
| `DCGM_FI_PROF_SM_ACTIVE`, `…_PIPE_*`, SM occupancy | `sinter_sm_occupancy_percent` (gap) | **not available** without counter collection |
| `DCGM_FI_DEV_SM_CLOCK` | `sinter_clock_mhz{clock="sclk"}` | available |
| `DCGM_FI_DEV_MEM_CLOCK` | `sinter_clock_mhz{clock="mclk"}` | available |
| `DCGM_FI_DEV_POWER_USAGE` | `sinter_power_watts` | available |
| `DCGM_FI_DEV_ENFORCED_POWER_LIMIT` | `sinter_power_cap_watts` | available |
| `DCGM_FI_DEV_FB_USED`, `DCGM_FI_DEV_FB_FREE` | `sinter_vram_used_bytes`, `sinter_vram_available_bytes` | available (two views) |
| — | `sinter_gtt_used_bytes`, `sinter_gtt_spill_detected` | AMD-specific addition: the spill path |
| PCIe TX/RX bytes | `sinter_pcie_tx_bytes_total` / `rx` (gap) | **not available** |
| `DCGM_FI_DEV_PCIE_REPLAY_COUNTER` | `sinter_pcie_aer_errors_total` | partial: AER totals are link errors, not replays |
| — | `sinter_pcie_link_speed_gts`, `sinter_pcie_link_width` | available; the closest thing to a bandwidth signal here |
| `DCGM_FI_DEV_XID_ERRORS` | `sinter_backend_unexpected_exits_total`, `sinter_vram_oom_events_total` | **no driver error bus**; the process is the observable |
| `DCGM_FI_DEV_THROTTLE_*` (reason bitmask) | `sinter_gpu_throttle_reason_bits` (gap) → `sinter_throttle_flag_info` | inferred, see below |
| `DCGM_FI_DEV_ECC_*` | not exported | ECC is not enabled on this consumer part; not measured |

## Derived throttle evidence

There is no register to read that says *why* the clock is low. So the exporter
names the shape it can see, and labels it as evidence rather than cause:

| Flag | Condition |
| --- | --- |
| `clock_below_ceiling` | engine busy ≥ 80% **and** `sclk < 0.85 × pp_dpm_sclk` ceiling |
| `power_at_cap` | `power1_average ≥ 0.97 × power1_cap` |
| `hotspot_at_crit` | junction ≥ its own `tempN_crit` |
| `pcie_link_downgraded` | negotiated speed or width below the capable speed or width |
| `pcie_aer_uncorrectable` | fatal or non-fatal AER totals above zero |

The fractions are heuristics, not hardware facts (`CLOCK_CEILING_FRACTION`,
`POWER_CAP_FRACTION`, `BUSY_MIN_PCT` in `src/cupola/sensors.py`). A clock below
the ceiling with an idle engine is normal power management, which is why the
busy gate exists: without it, every idle GPU pages you. A flag says "something
is limiting this card"; it does not say what, and the dashboard shows the
clock, power, and temperature traces side by side so the reader can tell.

The ceiling itself needs care on RDNA3: in `pp_dpm_sclk` the entry marked `*` is
the **live** clock, not a fixed state, and it moves every sample. The ceiling is
therefore the highest *unmarked* state — 2526 MHz on this card. Taking the
maximum of the whole table instead makes the ceiling a copy of the clock being
measured: the ratio is always 1, the dashboard draws the ceiling on top of the
clock, and a card held back below a genuinely higher ceiling is never flagged.
The corollary is a blind spot worth naming — if the real ceiling sits above the
top static DPM state (overclocked or high-binned parts run past it), a clock
just under that true ceiling looks normal.

## Alerts

`observability/prometheus/alerts.yml`, 13 rules. Every one of them describes
something an HTTP-layer dashboard misses.

| Rule | Condition | `for` | Severity |
| --- | --- | --- | --- |
| `SinterHotspotHigh` | junction > 0.9 × its critical limit | 5m | warning |
| `SinterHotspotAtCritical` | `hotspot_at_crit` | 1m | critical |
| `SinterSilentThrottle` | `clock_below_ceiling` | 10m | warning |
| `SinterPowerCapped` | `power_at_cap` | 10m | warning |
| `SinterPcieLinkDegraded` | negotiated speed **or** width < capable (`ignoring(limit)`) | 5m | critical |
| `SinterAerErrors` | any uncorrectable AER increase in 15m | — | critical |
| `SinterVramHeadroomLow` | headroom < 512 MiB while the backend is up | 5m | warning |
| `SinterGttSpill` | sentinel saw GTT grow | 5m | warning |
| `SinterVramOom` | any OOM signature in 30m | — | critical |
| `SinterBackendDied` | an unexpected exit in 15m | — | critical |
| `SinterCountersNotDurable` | `sinter_ledger_persist_error` | 10m | warning |
| `SinterExporterDown` | `up{job="cupola"} == 0` | 5m | critical |
| `SinterSensorBlind` | `absent(sinter_sensor_quality_info)` or quality `unavailable` | 5m | critical |

Two of these exist because the rest depend on data. A dashboard with no panels is
indistinguishable from a healthy GPU, so a missing series is itself an alert:
`SinterExporterDown` catches the exporter being gone, `SinterSensorBlind` catches
it being present but blind.

The thermal rule is relative to the chip's own limit rather than a literal
number of degrees, so it stays correct when the ASIC changes.

## The compose stack

```
observability/
  docker-compose.yml
  .env.example                 # copy to .env, set GF_ADMIN_PASSWORD
  prometheus/prometheus.yml    # scrape job: 127.0.0.1:9817, 10s interval
  prometheus/alerts.yml
  grafana/provisioning/…       # datasource + dashboard provider
  grafana/dashboards/cupola-throttling.json
```

```bash
cd observability
cp .env.example .env           # then edit GF_ADMIN_PASSWORD
docker compose up -d
docker compose ps
```

Grafana is on <http://127.0.0.1:3000> (dashboard: *Cupola GPU throttling*,
uid `cupola-throttling`), Prometheus on <http://127.0.0.1:9091>. Prometheus uses
9091 because 9090 is already taken on this host; change both the
`--web.listen-address` and the Grafana datasource URL together.

**Both services use `network_mode: host`.** The exporter binds loopback, and a
bridge-networked container cannot reach the host's loopback — a target of
`host.docker.internal:9817` resolves to the docker bridge and fails with
`connection refused`. Host networking plus a `127.0.0.1` target is what makes
the scrape work while keeping both UIs off the LAN. On an SELinux-enforcing host
the bind mounts also need `:Z`; this host has SELinux disabled, so the compose
file omits the label.

To check the stack rather than trust it:

```bash
curl -sS localhost:9817/metrics | head          # the exporter answers
curl -sS 'localhost:9091/api/v1/targets' | grep -o '"health":"[a-z]*"'
docker run --rm -v "$PWD/prometheus:/etc/prometheus:ro" prom/prometheus:v3.2.1 \
  promtool check rules /etc/prometheus/alerts.yml
```

Verified on this host: target `up`, all 13 rule expressions evaluate with no
warnings, and against a synthetic sysfs fixture (link 8 vs 16 GT/s, clock 900
vs 2526 MHz, power at cap, junction at critical, GTT spill, an OOM line in the
backend log, a supervised pid killed without a stop request) the thermal,
throttle, link, AER, spill, OOM and death rules all activate, while they stay
silent against the healthy card.

## Running the exporter as a service

Drop-in for a systemd user unit, so the endpoint is up without a terminal:

```ini
# ~/.config/systemd/user/cupola-metrics.service
[Unit]
Description=Cupola Prometheus exporter
After=default.target

[Service]
ExecStart=/usr/local/bin/cupola metrics --serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now cupola-metrics
loginctl enable-linger "$USER"   # keep it up without an open session
```

## Measured baseline (RX 7900 XTX, Navi 31)

Values seen during development, so a reader can tell "odd" from "broken":

| Signal | Observed |
| --- | --- |
| edge / junction / mem | 65–73 / 73–88 / 74–76 °C, `temp2_crit` = 110 °C |
| power | 246–341 W against a 339 W cap → `power_at_cap` |
| sclk | 500–3054 MHz observed; highest static DPM state 2526 MHz |
| mclk | 1249 MHz |
| busy | `gpu_busy_percent` 58–100, `mem_busy_percent` 15–19 |
| VRAM / GTT | 25 753 026 560 B total; GTT 50 393 620 480 B total |
| PCIe | 16.0 GT/s × 16, current = max |
| AER | 0 correctable, 0 non-fatal, 0 fatal |

The card is DRM `card1` and hwmon `hwmon1` here; `/sys/class/drm` also holds
connector nodes (`card1-HDMI-A-1`, `card1-Writeback-1`), which is why card
discovery matches `card[0-9]+` exactly rather than a prefix.

## Limitations

- **Single GPU.** No `gpu`/`UUID` label dimension. Multi-device hosts need the
  label contract settled first, not just a loop over cards.
- **No kernel OOM-killer view.** OOM is detected from the backend log and from
  an abnormal supervised exit, not from `dmesg` or cgroup events.
- **No driver error bus.** There is no XID equivalent on `amdgpu`; process
  death and allocation signatures are the observable.
- **Occupancy and PCIe byte counters are gaps**, declared on every scrape.
  Closing them means counter collection (`rocprofv3`), which needs a
  GPU-consuming profiler process alongside the inference workload.
- **Loopback only, no authentication.** Do not rebind this endpoint to a
  routable address; put a reverse proxy with auth in front if it must be remote.
- Grafana alerting is provisioned as Prometheus rules only. No Alertmanager,
  no notification routing: paging is a deployment decision, not a dashboard
  decision.
