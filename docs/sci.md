# SCI — Software Carbon Intensity

Sinter computes **Software Carbon Intensity** (SCI), the ISO/IEC 21031:2024 rate
used by the Green Software Foundation's
[SCI for AI](https://sci-for-ai.greensoftware.foundation/) methodology:

```
SCI = (E x I + M) / R
```

| Term | Meaning | Supplied by |
|---|---|---|
| `E` | energy used, kWh | the caller — a measured, tiered energy figure |
| `I` | grid carbon intensity, kgCO₂e/kWh | [`cupola.carbon_intensity`](../src/cupola/carbon_intensity.py) |
| `M` | embodied emissions, kgCO₂e | [`cupola.sci`](../src/cupola/sci.py) — amortised, or the 12 % heuristic |
| `R` | the functional unit | `FunctionalUnit` — whatever the caller can count |

SCI is deliberately a **rate**, not a total: a bigger workload that does more
useful work can have a *better* SCI. Absolute totals (kWh, gCO₂e) come back
alongside the rate so the number can be read either way.

## Design rules

Two rules decide everything here, and both exist because the alternative is a
number nobody should trust.

1. **A missing number is never zero.** No energy, no intensity or no functional
   unit yields `sci: null` with a machine-readable reason. Reporting `0` would
   claim the workload was carbon-free.
2. **Every number carries its provenance.** The intensity source, its basis,
   its resolution, the resolved series hash and the embodied method travel with
   the result, so a reader can weigh an estimate for what it is.

## The `I` term: grid carbon intensity

Sinter is local-first: **no socket is opened unless a provider is configured**.
The default is a single static factor supplied by the caller, which is offline
and reproducible.

Behaviour:

- A provider that times out, rate-limits or changes shape **degrades to the
  static factor** with the reason recorded; a caller never dies because a
  third-party grid API is down.
- `require_hourly` refuses that fallback instead, so a coarse number can never
  masquerade as an hourly one.
- Responses are cached under `~/.cache/cupola/carbon/`, keyed by provider, zone
  and the (minute-aligned) window, so re-resolving the same window is
  byte-identical rather than a second fetch. Overlapping windows do not share an
  entry — the cache buys reproducibility and a TTL, not deduplication.
- Only the time window and zone leave the machine, and only when a provider is
  configured.
- A series is refused outright if a value is not finite, is negative, or exceeds
  5 kgCO₂e/kWh — which is how a unit mistake (g/kWh declared as kg/kWh) is
  caught rather than silently multiplied by 1000.

### Providers shipped in v1

| `provider` | Coverage | Resolution | Key | Notes |
|---|---|---|---|---|
| `static` | any country | annual | none | offline; the caller's configured factor |
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
embodied impacts for servers and GPUs; amortising those figures gives a
defensible `M` instead of the heuristic.

### Hourly matching

With a provider series and the window's power samples, `I` becomes the
**consumption-matched** intensity over that window:

```
I_eff = sum(P_i x I(t_i) x dt_i) / sum(P_i x dt_i)
```

The series is step-interpolated (a published interval covers `[start, end)`),
never linearly. The coverage fraction and the flat time-mean come back alongside
the matched value, so the difference between "the annual average" and "what this
workload actually drew" is visible rather than assumed.

## Scope

- **Not authoritative intensity data.** Free-tier terms and factors were read
  from provider documentation in September 2026; re-verify before relying on
  them commercially. The static factor remains the caller's to defend.
- **Not ESG or Scope 1/2/3 accounting.** No supplier emissions, no offsets, no
  disclosure reporting.
