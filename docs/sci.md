# SCI — Software Carbon Intensity

Cupola reports **Software Carbon Intensity** (SCI), the ISO/IEC 21031:2024 rate
used by the Green Software Foundation's
[SCI for AI](https://sci-for-ai.greensoftware.foundation/) methodology:

```
SCI = (E x I + M) / R
```

| Term | Meaning | Where cupola gets it |
|---|---|---|
| `E` | energy used, kWh | Sentinel / benchmark energy window, tiered (see below) |
| `I` | grid carbon intensity, kgCO₂e/kWh | static `[accounting]` factor, or a `[carbon]` provider |
| `M` | embodied emissions, kgCO₂e | `[accounting]` method (amortised, or the 12 % heuristic) |
| `R` | the functional unit | per **verified task**, per 1k tokens, or per session hour |

SCI is deliberately a **rate**, not a total: a bigger workload that does more
verified work can have a *better* SCI. Absolute totals (kWh, gCO₂e) are always
reported alongside so the number can be read either way.

## Design rules

Two rules decide everything here, and both exist because the alternative is a
number nobody should trust.

1. **A missing number is never zero.** No energy, no intensity or no functional
   unit produces `sci: null` with a machine-readable reason. Reporting `0` would
   claim the workload was carbon-free.
2. **Every number carries its provenance.** Energy tier, intensity source,
   basis, resolution, series hash and embodied method travel with the result,
   and the report states what it cannot prove.

The three commands that answer questions, in order of how often you want them:

```bash
cupola sci                                  # the last session, per session hour
cupola sci --report b.json                  # a benchmark report, per verified task
cupola telemetry --session                  # same session block, plus thermal data
```

## Why this lives in cupola and not in minder

| SCI term | Needs | Owner |
|---|---|---|
| `E` | `hwmon` power/energy counters and the measured window | cupola |
| `I` | a declared, versioned grid factor and its basis | cupola |
| `M` | device identity and declared service life | cupola |
| `R` | *verified* work the agent did not author | cupola (Compass) |

The boundary is already written down in [prd v2.md](prd%20v2.md): cupola owns
`hwmon` and the hardware and "never touches prompt tokens"; minder owns cognitive
policy and "never interfaces directly with Linux `sysfs` or GPU hardware
registers". Three further reasons, all verifiable on this machine:

- The GPU exposes `power1_average` but not `energy1_input`, so `E` is integrated
  from ~1 Hz samples. Per-request attribution (a proxy's natural boundary) is
  below that resolution; per-run windows match it.
- Minder is a sidecar and does not own the `llama-server` process, so it cannot
  bound a measurement window.
- `R` must be verified work. A proxy sees requests; only Compass knows whether
  the work was actually done.

Minder's legitimate role is to *consume* a carbon signal for policy — not to
compute one.

## Energy measurement tiers

| Tier | Source | Quality |
|---|---|---|
| 1 | wall-meter counter file (`[sentinel] wall_energy_path`, `wall_energy_unit`) | `measured` |
| 2 | GPU hwmon `energy1_input` (µJ) | `measured` |
| 3 | GPU hwmon `power1_average` integrated over the window | `estimated` |
| 4 | explicit `assume_power_w` | `assumed` |
| 0 | no data | `unavailable` |

Tier 4 is excluded from SCI unless `[accounting] allow_assumed_power = true`,
matching the gate the older `co2e_kg` estimate already used. Absence is never
zero, and that is enforced at the tier itself rather than patched by each
consumer: a power-integration window in which *every* reading lacked a power
value, and a tier-2 counter that did not advance or ran backwards, both return
`energy_kwh: null` with quality `unavailable`. A tier that cannot produce a
number falls through to the next one that can, and the result says which tier
actually measured it.

## Configuration

```toml
[accounting]
# I — the factor every provider falls back to. Required for any carbon number.
emissions_factor_kgco2e_per_kwh = 0.40
emissions_factor_source = "user-configured"
emissions_factor_version = "2024-01"
emissions_intensity_basis = "location"   # location | marginal

# M — embodied emissions
embodied_method = "share_of_operational" # share_of_operational | amortised
embodied_share_of_operational = 0.12     # the GSF reference heuristic
# embodied_total_kgco2e = 1200.0         # required by "amortised"
# embodied_lifetime_hours = 20000.0      # required by "amortised"

allow_assumed_power = false

[sentinel]
# wall_energy_path = "/path/to/meter"
wall_energy_unit = "kwh"                 # kwh | wh | j — declared, never guessed
```

## Time-resolved intensity (`[carbon]`)

Cupola is local-first: **no socket is opened unless a provider is configured**.
The default is the static factor above, which is offline and reproducible.

```toml
[carbon]
provider = "static"      # static | file | neso-gb | energidataservice-dk
zone = ""                # GB | DK1 | DK2 | ...
basis = "location"       # location | marginal
# file = "/path/to/series.csv"
# file_unit = "gco2e_per_kwh"          # gco2e_per_kwh | kgco2e_per_kwh
# api_key_env = "ELECTRICITYMAPS_TOKEN" # secrets come from the environment
cache_ttl_s = 3600
timeout_s = 5.0
require_hourly = false   # true => never silently fall back to the annual factor
```

Behaviour:

- A provider that times out, rate-limits or changes shape **degrades to the
  static factor** with the reason recorded; a run never dies because a
  third-party grid API is down.
- `require_hourly = true` refuses that fallback instead, so a coarse number can
  never masquerade as an hourly one.
- Responses are cached under `~/.cache/cupola/carbon/`, keyed by provider,
  zone and the (minute-aligned) window, so re-analysing the same window is
  byte-identical rather than a second fetch. Overlapping windows do not share
  an entry — the cache buys reproducibility and a TTL, not deduplication.
- Only the time window and zone leave the machine, and only when a provider is
  configured.

### Providers shipped in v1

| `provider` | Coverage | Resolution | Key | Notes |
|---|---|---|---|---|
| `static` | any country | annual | none | offline; the `[accounting]` factor |
| `file` | any country | whatever you exported | none | CSV/JSON from Ember, OWID, eGRID, … |
| `neso-gb` | Great Britain | 30 min | none | [NESO Carbon Intensity](https://carbon-intensity.github.io/api-definitions/) (CC BY 4.0) |
| `energidataservice-dk` | DK1 / DK2 | 5 min | none | [Energi Data Service](https://energidataservice.dk/guides/api-guides) `CO2Emis` |

### Documented, not implemented

These are one small class each behind the same `IntensityProvider` protocol in
[`src/cupola/carbon_intensity.py`](../src/cupola/carbon_intensity.py). Selecting
one today reports `provider_not_implemented` and falls back to the static
factor rather than pretending.

| `provider` | Coverage | Resolution | Basis | Access |
|---|---|---|---|---|
| `entsoe` | EU / EEA | hourly | mix → compute | free token ([guide](https://transparency.entsoe.eu/content/static_content/Static%20content/web%20api/Guide.html)) |
| `eia` | 60+ US balancing authorities | hourly | mix → compute | free key ([EIA v2](https://api.eia.gov/v2/electricity/rto/fuel-type-data/)) |
| `openelectricity` | AU NEM + WEM | 5 min | emissions | key ([docs](https://docs.openelectricity.org.au/api-reference/metrics/)) |
| `electricitymaps` | ~200 zones | hourly | average **and** marginal | key ([portal](https://portal.electricitymaps.com/docs/api)); free personal tier |
| `watttime` | 342 regions / 210 countries | 5 min | **marginal** (MOER) | account ([docs](https://docs.watttime.org/)) |
| `ember` | global per country | annual / monthly | location | key ([api](https://api.ember-energy.org/v1/)) |
| `owid` | global per country | annual | location | none ([grapher](https://ourworldindata.org/grapher/carbon-intensity-electricity)) |
| `climatiq` | global factors | annual | location | key ([docs](https://www.climatiq.io/docs/api-reference/energy)) |

Energy-Charts (Fraunhofer ISE, 39 European countries, 15-minute generation mix,
no key, CC BY 4.0) is a good source too; it publishes a mix rather than an
intensity, so it fits the same "mix → compute" shape as `entsoe` and `eia`.

For `M`, [Boavizta](https://doc.api.boavizta.org/) publishes open bottom-up
embodied impacts for servers and GPUs; set `embodied_method = "amortised"` with
those figures for a defensible `M` instead of the heuristic.

### Hourly matching

With a provider series and the run's power samples, cupola computes the
**consumption-matched** intensity over the window:

```
I_eff = sum(P_i x I(t_i) x dt_i) / sum(P_i x dt_i)
```

The series is step-interpolated (a published interval covers
`[start, end)`), never linearly. The report keeps the coverage fraction and the
flat time-mean as well, so the difference between "the annual average" and "what
this workload actually drew" is visible rather than assumed. `hourly_matched`
only counts as *proven* with a resolution of an hour or finer and at least 95 %
window coverage.

## Which functional unit is reported

`R` is not configurable, because it is fixed by what each surface can actually
measure — a knob here would only ever select between numbers cupola already
prints:

| Surface | Unit(s) | Why |
|---|---|---|
| A benchmark report | `verified_task` (headline) and `1k_tokens` | Compass verifies the work and counts the tokens |
| A Sentinel session | `session_hour` | the sampler sees the hardware, not verified work |

`metrics.carbon.unit` names the headline unit, and every SCI key says which unit
it is in (`sci_gco2e_per_verified_task`, `sci_gco2e_per_1k_tokens`), so nothing
is ambiguous at the point of use.

## Reading a result

```bash
cupola sci --report b.json
```

```
Report: coding-core-v2 (a1b2c3d4e5f6)
  SCI: 4.48 gCO2e per verified task [estimated]
  Per 1k tokens: 4.48 gCO2e
  Energy: 0.06 kWh total over 6 run(s)
  I: 0.4 kgCO2e/kWh (user-configured, location, matched=flat)
  M: share_of_operational
  pass     intensity_declared: I = 0.4 kgCO2e/kWh from user-configured (location, version 2024-01)
  unproven energy_measured: 0 metered run(s); 5 needed
  ...
```

## Green software principles

`metrics.carbon.principles` is a closed checklist. A principle is `pass` only
with positive evidence; everything else is `unproven` with the reason. There is
deliberately no `fail` yet — cupola has no registry of carbon *claims* to
contradict. (When capability manifests land, `fail` returns for a claim the
evidence disproves.)

| id | `pass` requires |
|---|---|
| `energy_measured` | ≥ 5 runs at a metered tier (1–2) |
| `intensity_declared` | intensity source, version and basis all present |
| `hourly_matched` | time-resolved provider, resolution ≤ 1 h, ≥ 95 % coverage |
| `embodied_accounted` | `M` from an amortised or provider figure, not the heuristic |
| `unit_declared` | an SCI value with a named functional unit |
| `carbon_efficiency_proven` | a comparable A/B with a delta ≤ 0 and completion held |
| `carbon_awareness` | `I.basis = "marginal"` — the signal that changes when load shifts |

## Does installing a plugin actually help?

This is the question the whole design exists to answer. Run the same suite
twice: once without the capability, once with it, then attribute the difference.

```bash
# arm A — the harness talks straight to llama-server
cupola bench suite run --suite coding-core-v2 --system dsh-local --execute --out a.json

# arm B — identical, except the harness base_url points at the proxy
#          (MINDER_UPSTREAM=http://127.0.0.1:8080, harness -> http://127.0.0.1:8390/v1)
cupola bench suite run --suite coding-core-v2 --system dsh-local-minder --execute --out b.json

cupola bench compare --baseline a.json --candidate b.json \
    --attribute marginal --arm minder=b.json
```

The output carries both answers:

```
minder vs none: PASS
    sci_gco2e_per_verified_task: 10.0 -> 6.0 (better)
carbon: helping - SCI delta -4.0 gCO2e per verified task
green evidence: 4/7 principles proven [intensity_declared, hourly_matched,
unit_declared, carbon_efficiency_proven]; unproven: energy_measured,
embodied_accounted, carbon_awareness
```

`carbon_efficiency_proven` is evaluated here, where both arms exist — a lone
report cannot know its own baseline, so `cupola sci --report` shows it as
unproven. `--json` carries the full principle list for each comparison.

Three things make that honest:

- **Completion is checked first.** A candidate that emits less because it fails
  more is reported as `not helping`.
- **Methodology must match.** If the two arms resolved intensity differently
  (source, basis, resolution or embodied method), the carbon deltas are marked
  `comparable: false` and the answer is `unknown`.
- **Five runs minimum.** Below `MIN_COMPARABLE_RUNS` the answer is `unknown`; a
  single run is not a result.

A negative answer is a real result (`helped: false`), not an error.

## What this is not

- **Not ESG or Scope 1/2/3 accounting.** No supplier emissions, no offsets, no
  disclosure reporting.
- **Not a per-request carbon meter.** `E` is device-wide: other work on the same
  card is included, and the report says so.
- **Not an annual-average claim dressed as hourly.** `matched`, `resolution_s`
  and `coverage` are always recorded.
- **Not authoritative intensity data.** Free-tier terms and factors were read
  from provider documentation in September 2026; re-verify before relying on
  them commercially. The `[accounting]` factor remains yours to defend.
