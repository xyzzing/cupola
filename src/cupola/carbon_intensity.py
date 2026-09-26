"""Grid carbon intensity providers — the ``I`` in ``SCI = (E x I + M) / R``.

Sinter is local-first, so **nothing here opens a socket unless a provider is
explicitly configured** (``[carbon] provider = ...``). The default is the
static annual factor already in ``[accounting]``.

Four providers ship in v1, chosen so that the common cases need no account:

``static``
    No network. Uses ``accounting.emissions_factor_kgco2e_per_kwh``.
``file``
    No network. Reads a CSV or JSON series the operator exported from Ember,
    Our World in Data, eGRID or any other country-level dataset.
``neso-gb``
    No key. Great Britain, half-hourly settlement periods, forecast + actual.
``energidataservice-dk``
    No key. Denmark DK1/DK2, five-minute CO2 emission intensity.

Every function fails **open**: a provider that times out, rate-limits, or
changes shape degrades to the static factor with a recorded reason, and never
takes a run down with it. Set ``require_hourly = true`` to refuse the fallback
instead, so a coarse number can never masquerade as an hourly one.

Other sources are documented in ``docs/sci.md`` and land as one small class
behind :class:`IntensityProvider` when a user needs them: ``entsoe`` (EU, free
token), ``eia`` (US balancing authorities, free key), ``electricitymaps``
(global, marginal + average), ``watttime`` (global marginal MOER), ``ember``
(country, annual), ``openelectricity`` (Australia).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

USER_AGENT = "cupola-sci/0.1 (+https://github.com/xyzzing/cupola)"

NESO_BASE = "https://api.carbonintensity.org.uk"
ENERGIDATASERVICE_BASE = "https://api.energidataservice.dk"

NESO_RESOLUTION_S = 1800
ENERGIDATASERVICE_RESOLUTION_S = 300

#: Above this the declared file units are almost certainly wrong (a factor
#: mistaken for gCO2e/kWh when it was kgCO2e/kWh, or rather the reverse).
IMPLAUSIBLE_KGCO2E_PER_KWH = 5.0

#: Largest operator-supplied series cupola will parse. A year of five-minute
#: data is ~105k rows (~4 MB), so this is generous; the point is to fail with a
#: reason instead of reading a log file into memory by mistake.
MAX_SERIES_BYTES = 32 * 1024 * 1024

#: Provider zone codes are plain ASCII tokens, never free text.
_ZONE_RE = re.compile(r"[A-Za-z0-9-]{1,16}")

#: Largest provider response cupola will read. A decade of five-minute
#: intervals is well under this.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: Smallest window sent to a provider. A benchmark run shorter than a minute
#: would otherwise ask for an empty range and silently lose its hourly figure.
MIN_FETCH_WINDOW_S = 60.0

_UNIT_DIVISORS = {
    "gco2e_per_kwh": 1000.0,
    "kgco2e_per_kwh": 1.0,
}


class IntensityError(Exception):
    """A provider could not produce a series (network, shape, or zone)."""


@dataclass(frozen=True)
class IntensityPoint:
    """One published interval of grid carbon intensity."""

    ts_start: float
    ts_end: float
    kgco2e_per_kwh: float
    basis: str = "location"
    resolution_s: int = 0
    source: str = ""
    source_version: str = ""
    fetched_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ts_start": self.ts_start,
            "ts_end": self.ts_end,
            "kgco2e_per_kwh": self.kgco2e_per_kwh,
            "basis": self.basis,
            "resolution_s": self.resolution_s,
            "source": self.source,
            "source_version": self.source_version,
            "fetched_at": self.fetched_at,
        }


class IntensityProvider(Protocol):
    """A source of time-resolved grid carbon intensity."""

    name: str
    basis: str
    default_zone: str

    def series(self, *, zone: str, start: float, end: float,
               basis: str) -> list[IntensityPoint]: ...


@dataclass(frozen=True)
class ResolvedIntensity:
    """A resolved series plus everything needed to judge and reproduce it."""

    points: tuple[IntensityPoint, ...] = ()
    source: str = "static"
    version: str = ""
    basis: str = "location"
    region: str = ""
    resolution_s: int = 0
    series_sha256: str = ""
    fetched_at: float = 0.0
    reason: str = ""

    @property
    def time_resolved(self) -> bool:
        return bool(self.points)


def _resolve_cache_dir(cache_dir: Optional[Path]) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    from cupola.config import DEFAULT_CACHE_DIR

    return DEFAULT_CACHE_DIR / "carbon"


def _http_get_json(url: str, timeout: float) -> dict:
    """GET a JSON document. The single network seam, monkeypatched in tests."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # Bounded: a wrong URL must not be able to read a stream forever
            # into memory. One byte over the cap proves it is too big.
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise IntensityError(f"{url}: {exc}") from exc
    if len(payload) > MAX_RESPONSE_BYTES:
        raise IntensityError(
            f"{url}: response exceeds the {MAX_RESPONSE_BYTES}-byte limit")
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise IntensityError(f"{url}: response is not JSON: {exc}") from exc


def _timestamp(value) -> Optional[float]:
    """Epoch seconds from an ISO 8601 string or a numeric timestamp."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%MZ")


def _fetch_window(start: float, end: float) -> tuple[float, float]:
    """Widen a window to whole minutes, which is the precision providers take.

    Without this a benchmark run shorter than a minute asks for ``from == to``
    and receives an empty range, so every short run would silently lose its
    hourly figure and fall back to the annual factor.
    """
    floored = math.floor(start / 60.0) * 60.0
    ceiled = math.ceil(end / 60.0) * 60.0
    if ceiled - floored < MIN_FETCH_WINDOW_S:
        ceiled = floored + MIN_FETCH_WINDOW_S
    return floored, ceiled


def _series_hash(points: Sequence[IntensityPoint]) -> str:
    canonical = json.dumps(
        [[round(p.ts_start, 3), round(p.kgco2e_per_kwh, 6)] for p in points],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# --- providers -------------------------------------------------------------


@dataclass
class StaticProvider:
    """The configured annual factor. Opens no socket, by construction."""

    name: str = "static"
    basis: str = "location"
    default_zone: str = ""

    def series(self, *, zone: str, start: float, end: float,
               basis: str) -> list[IntensityPoint]:
        return []


@dataclass
class FileProvider:
    """An operator-supplied CSV/JSON series. Opens no socket."""

    path: Path
    unit: str = "gco2e_per_kwh"
    basis: str = "location"
    default_zone: str = ""
    name: str = "file"

    def series(self, *, zone: str, start: float, end: float,
               basis: str) -> list[IntensityPoint]:
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            raise IntensityError(f"cannot read {self.path}: {exc}") from exc
        if size > MAX_SERIES_BYTES:
            raise IntensityError(
                f"{self.path} is {size} bytes, above the "
                f"{MAX_SERIES_BYTES}-byte series limit")
        try:
            raw = self.path.read_text()
        except (OSError, ValueError, UnicodeError) as exc:
            # UnicodeDecodeError is a ValueError, so catching OSError alone let
            # a UTF-16 export escape as unhandled.
            raise IntensityError(f"cannot read {self.path}: {exc}") from exc
        divisor = _UNIT_DIVISORS.get(self.unit)
        if divisor is None:
            raise IntensityError(
                f"unknown file unit {self.unit!r} "
                f"(expected one of {sorted(_UNIT_DIVISORS)})")
        points = parse_series(raw, str(self.path), divisor=divisor)
        window = [p for p in points if p.ts_end > start and p.ts_start < end]
        if not window:
            raise IntensityError(
                f"{self.path} has no intervals covering the requested window")
        return window


def parse_series(raw: str, source: str, *, divisor: float = 1000.0,
                 resolution_s: int = 0) -> list[IntensityPoint]:
    """Parse the two shapes people actually export.

    * JSON: a list of ``{from|ts_start, to|ts_end, intensity|kgco2e_per_kwh}``
      objects (or ``{"data": [...]}``), or a single ``{"intensity": ...}``
      block as the NESO API returns.
    * CSV: a header row naming ``from``/``to``/``intensity``, or the simple
      ``timestamp,intensity`` two-column export.

    ``divisor`` converts the file's declared unit to kgCO2e/kWh.
    """
    text = raw.lstrip()
    if text.startswith("[") or text.startswith("{"):
        return _parse_json_series(text, source, divisor)
    return _parse_csv_series(raw, source, divisor, resolution_s)


def _parse_json_series(text: str, source: str, divisor: float
                       ) -> list[IntensityPoint]:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise IntensityError(f"{source}: invalid JSON: {exc}") from exc
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = [payload]
    points = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        start = _timestamp(row.get("ts_start", row.get("from")))
        end = _timestamp(row.get("ts_end", row.get("to")))
        value = row.get("kgco2e_per_kwh")
        if value is None:
            value = row.get("intensity")
            if isinstance(value, dict):
                value = value.get("actual")
                if value is None:
                    value = value if isinstance(value, (int, float)) else None
        if value is None:
            value = row.get("CO2Emission")
        if start is None or not isinstance(value, (int, float)):
            continue
        points.append(_make_point(start, end, float(value) / divisor, source))
    return _sanity_check(points, source)


def _parse_csv_series(raw: str, source: str, divisor: float,
                      resolution_s: int) -> list[IntensityPoint]:
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        raise IntensityError(f"{source}: empty series")
    header = [cell.strip().lower() for cell in lines[0].split(",")]
    if "from" in header:
        start_i = header.index("from")
        end_i = header.index("to") if "to" in header else None
        value_i = header.index("intensity") if "intensity" in header else None
        if value_i is None:
            value_i = header.index("kgco2e_per_kwh") \
                if "kgco2e_per_kwh" in header else len(header) - 1
        body = lines[1:]
    else:
        start_i, end_i, value_i = 0, None, 1
        body = lines[1:] if not _is_number(header[0]) else lines

    raw_points: list[tuple[float, Optional[float], float]] = []
    for line in body:
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) <= max(start_i, value_i):
            continue
        start = _timestamp(cells[start_i])
        try:
            value = float(cells[value_i])
        except (TypeError, ValueError):
            continue
        if start is None:
            continue
        end = _timestamp(cells[end_i]) if end_i is not None and \
            len(cells) > end_i else None
        raw_points.append((start, end, value / divisor))
    raw_points.sort(key=lambda item: item[0])

    points = []
    for index, (start, end, value) in enumerate(raw_points):
        if end is None or end <= start:
            if index + 1 < len(raw_points):
                end = raw_points[index + 1][0]
            else:
                end = start + (resolution_s or 3600)
        points.append(_make_point(start, end, value, source))
    return _sanity_check(points, source)


def _is_number(text: str) -> bool:
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


def _make_point(start: float, end: Optional[float], kgco2e_per_kwh: float,
                source: str) -> IntensityPoint:
    if end is None or end <= start:
        end = start + 3600
    return IntensityPoint(
        ts_start=float(start),
        ts_end=float(end),
        kgco2e_per_kwh=float(kgco2e_per_kwh),
        basis="location",
        resolution_s=int(max(1.0, end - start)),
        source=source,
        source_version="",
        fetched_at=time.time(),
    )


def _sanity_check(points: list[IntensityPoint], source: str
                  ) -> list[IntensityPoint]:
    """Refuse values that cannot be a real grid intensity.

    NaN and infinity propagate silently through arithmetic and comparisons; a
    negative factor would produce a negative — physically impossible — SCI. The
    upper bound catches a unit mistake (g/kWh declared as kg/kWh).
    """
    if not points:
        raise IntensityError(f"{source}: no usable intervals parsed")
    for point in points:
        value = point.kgco2e_per_kwh
        if not math.isfinite(value):
            raise IntensityError(
                f"{source}: intensity is not a finite number ({value!r})")
        if value < 0.0 or value > IMPLAUSIBLE_KGCO2E_PER_KWH:
            raise IntensityError(
                f"{source}: intensity {value} kgCO2e/kWh is outside "
                f"0..{IMPLAUSIBLE_KGCO2E_PER_KWH} - check the declared units")
    return points


@dataclass
class NesoGbProvider:
    """Great Britain, half-hourly, no API key (CC BY 4.0)."""

    timeout: float = 5.0
    name: str = "neso-gb"
    basis: str = "location"
    default_zone: str = "GB"

    def series(self, *, zone: str, start: float, end: float,
               basis: str) -> list[IntensityPoint]:
        url = (f"{NESO_BASE}/intensity/{_to_iso(start)}/{_to_iso(end)}")
        payload = _http_get_json(url, timeout=self.timeout)
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise IntensityError("neso-gb returned no intervals")
        points = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            block = row.get("intensity") or {}
            value = block.get("actual")
            if value is None:
                value = block.get("forecast")
            start_ts = _timestamp(row.get("from"))
            end_ts = _timestamp(row.get("to"))
            if start_ts is None or not isinstance(value, (int, float)):
                continue
            points.append(IntensityPoint(
                ts_start=start_ts,
                ts_end=end_ts if end_ts else start_ts + NESO_RESOLUTION_S,
                kgco2e_per_kwh=float(value) / 1000.0,  # gCO2e/kWh -> kgCO2e/kWh
                basis="location",
                resolution_s=NESO_RESOLUTION_S,
                source=self.name,
                source_version=str(block.get("index") or ""),
                fetched_at=time.time(),
            ))
        if not points:
            raise IntensityError("neso-gb returned no usable intervals")
        return points


@dataclass
class EnergiDataServiceProvider:
    """Denmark DK1/DK2, five-minute CO2 emission intensity, no API key."""

    timeout: float = 5.0
    name: str = "energidataservice-dk"
    basis: str = "location"
    default_zone: str = "DK1"

    def series(self, *, zone: str, start: float, end: float,
               basis: str) -> list[IntensityPoint]:
        price_area = (zone or self.default_zone).upper()
        if not _ZONE_RE.fullmatch(price_area):
            raise IntensityError(
                f"energidataservice-dk: {price_area!r} is not a price area "
                "(expected DK1 or DK2)")
        query = urllib.parse.urlencode({
            "start": _to_iso(start),
            "end": _to_iso(end),
            "filter": json.dumps({"PriceArea": [price_area]}),
            "limit": 0,
        })
        url = f"{ENERGIDATASERVICE_BASE}/dataset/CO2Emis?{query}"
        payload = _http_get_json(url, timeout=self.timeout)
        rows = payload.get("records")
        if not isinstance(rows, list) or not rows:
            raise IntensityError(
                f"energidataservice-dk has no records for {price_area}")
        points = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            start_ts = _timestamp(row.get("Minutes5UTC") or row.get("Minutes5DK"))
            value = row.get("CO2Emission")
            if start_ts is None or not isinstance(value, (int, float)):
                continue
            if start_ts < start - ENERGIDATASERVICE_RESOLUTION_S or \
                    start_ts > end + ENERGIDATASERVICE_RESOLUTION_S:
                continue
            points.append(IntensityPoint(
                ts_start=start_ts,
                ts_end=start_ts + ENERGIDATASERVICE_RESOLUTION_S,
                kgco2e_per_kwh=float(value) / 1000.0,  # gCO2e/kWh -> kgCO2e/kWh
                basis="location",
                resolution_s=ENERGIDATASERVICE_RESOLUTION_S,
                source=self.name,
                source_version=str(row.get("PriceArea") or price_area),
                fetched_at=time.time(),
            ))
        if not points:
            raise IntensityError(
                f"energidataservice-dk returned no usable intervals for "
                f"{price_area}")
        return points


#: name -> factory. Adding a provider is one class plus one row here; no core
#: change, which is the point of the registry.
PROVIDERS: dict[str, Callable[[object], IntensityProvider]] = {
    "static": lambda carbon: StaticProvider(),
    "file": lambda carbon: FileProvider(
        path=Path(getattr(carbon, "file", "") or ""),
        unit=getattr(carbon, "file_unit", "gco2e_per_kwh") or "gco2e_per_kwh",
    ),
    "neso-gb": lambda carbon: NesoGbProvider(timeout=_timeout_of(carbon)),
    "energidataservice-dk": lambda carbon: EnergiDataServiceProvider(
        timeout=_timeout_of(carbon)),
}


def _timeout_of(carbon) -> float:
    """The configured HTTP timeout, defaulted and sanity-bounded."""
    try:
        value = float(getattr(carbon, "timeout_s", 5.0) or 5.0)
    except (TypeError, ValueError):
        return 5.0
    return value if value > 0 else 5.0

#: Accepted aliases, so a user's spelling is not the reason a provider is off.
PROVIDER_ALIASES = {
    "": "static",
    "off": "static",
    "none": "static",
    "energidataservice": "energidataservice-dk",
    "energidk": "energidataservice-dk",
    "national-grid-eso": "neso-gb",
    "carbon-intensity-uk": "neso-gb",
}

#: Documented in ``docs/sci.md`` but not implemented in v1. Selecting one
#: yields the static factor with a reason, rather than a silent no-op.
DOCUMENTED_PROVIDERS = (
    "entsoe", "eia", "openelectricity", "electricitymaps", "watttime",
    "ember", "owid", "climatiq",
)


def provider_for(carbon) -> IntensityProvider:
    """Build the configured provider, defaulting to ``static``."""
    raw = (getattr(carbon, "provider", "static") or "static").strip().lower()
    name = PROVIDER_ALIASES.get(raw, raw)
    factory = PROVIDERS.get(name)
    if factory is None:
        # Unknown or documented-only: no network, and the caller records why.
        return StaticProvider()
    if name == "file" and not (getattr(carbon, "file", "") or ""):
        return StaticProvider()
    return factory(carbon)


def resolve_intensity(
    *,
    carbon,
    accounting,
    start: float,
    end: float,
    cache_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> ResolvedIntensity:
    """Resolve the intensity series for ``[start, end]``, cache first.

    Never raises: on any provider failure the configured static factor is
    returned with ``reason`` set, unless ``carbon.require_hourly`` is true, in
    which case an empty series is returned so SCI reports itself unavailable
    rather than silently coarser.
    """
    moment = time.time() if now is None else now
    provider = provider_for(carbon)
    static_value = getattr(accounting, "emissions_factor_kgco2e_per_kwh", None)
    static_basis = getattr(accounting, "emissions_intensity_basis", "location")
    static_source = getattr(accounting, "emissions_factor_source", "") or "static"
    static_version = getattr(accounting, "emissions_factor_version", "")
    region = (getattr(carbon, "zone", "") or provider.default_zone or
              getattr(accounting, "emissions_region", "") or "")
    basis = getattr(carbon, "basis", "location") or "location"
    ttl = float(getattr(carbon, "cache_ttl_s", 3600) or 0)
    require_hourly = bool(getattr(carbon, "require_hourly", False))

    def static_result(reason: str, source: str = "", version: str = "",
                      basis_override: str = "") -> ResolvedIntensity:
        return ResolvedIntensity(
            source=source or static_source or "static",
            version=version or static_version or "static",
            basis=basis_override or static_basis,
            region=region,
            resolution_s=0,
            series_sha256="",
            reason=reason,
        )

    def unavailable(reason: str) -> ResolvedIntensity:
        return ResolvedIntensity(
            source=provider.name, basis=basis, region=region, reason=reason)

    configured_name = (getattr(carbon, "provider", "static") or "static").strip()
    if provider.name == "static":
        lowered = configured_name.lower()
        if lowered == "file" and not (getattr(carbon, "file", "") or ""):
            return static_result(
                "provider_file_not_configured: [carbon] file = <path> is empty")
        if lowered not in ("", "off", "none", "static") \
                and lowered not in PROVIDER_ALIASES:
            if lowered in DOCUMENTED_PROVIDERS:
                return static_result(
                    f"provider_not_implemented: {configured_name} is documented "
                    "but has no implementation yet - it needs a keyed account, "
                    "so add a class in cupola/carbon_intensity.py or export the "
                    "series to a file (see docs/sci.md)")
            return static_result(f"provider_unknown: {configured_name!r}")
        if static_value is None:
            return static_result("intensity_unconfigured")
        return static_result("")

    fetch_start, fetch_end = _fetch_window(start, end)
    cache_path = _cache_path(cache_dir, provider.name, region, fetch_start,
                             fetch_end)
    cached = _read_cache(cache_path)
    if cached is not None:
        fetched_at, points = cached
        # A non-positive TTL means the cached series never expires.
        if ttl <= 0 or (moment - fetched_at) < ttl:
            return _resolved_from(points, provider, basis, region, fetched_at, "")

    try:
        points = provider.series(zone=region, start=fetch_start, end=fetch_end,
                                 basis=basis)
    except Exception as exc:
        # A provider must never take a run down with it, whatever it raises:
        # this method's contract is that it does not raise. An unexpected
        # exception is reported with its type so it is not silently absorbed.
        error = (exc if isinstance(exc, IntensityError)
                 else IntensityError(f"{type(exc).__name__}: {exc}"))
        if cached is not None:
            fetched_at, points = cached
            return _resolved_from(
                points, provider, basis, region, fetched_at,
                f"provider_error_using_cache: {error}")
        if require_hourly:
            return unavailable(f"intensity_hourly_unavailable: {error}")
        if static_value is None:
            return static_result(f"provider_error: {error}")
        return static_result(f"provider_error_static_fallback: {error}")

    if not points:
        if require_hourly:
            return unavailable("intensity_hourly_unavailable: empty series")
        return static_result("provider_returned_empty")

    _write_cache(cache_path, points, moment)
    return _resolved_from(points, provider, basis, region, moment, "")


def _resolved_from(points: Sequence[IntensityPoint], provider,
                   basis: str, region: str, fetched_at: float,
                   reason: str) -> ResolvedIntensity:
    actual_basis = points[0].basis if points else basis
    note = reason
    if actual_basis != basis:
        mismatch = f"basis_mismatch: {provider.name} publishes {actual_basis}"
        note = f"{note}; {mismatch}" if note else mismatch
    resolutions = [p.resolution_s for p in points if p.resolution_s]
    return ResolvedIntensity(
        points=tuple(points),
        source=provider.name,
        version=points[0].source_version if points else "",
        basis=actual_basis,
        region=region,
        resolution_s=min(resolutions) if resolutions else 0,
        series_sha256=_series_hash(points),
        fetched_at=fetched_at,
        reason=note,
    )


# --- cache -----------------------------------------------------------------

_INTENSITY_FIELDS = ("ts_start", "ts_end", "kgco2e_per_kwh", "basis",
                     "resolution_s", "source", "source_version", "fetched_at")


def _cache_path(cache_dir: Optional[Path], provider: str, zone: str,
                start: float, end: float) -> Path:
    safe_zone = "".join(ch for ch in (zone or "default")
                        if ch.isalnum() or ch in "-_") or "default"
    return _resolve_cache_dir(cache_dir) / (
        f"{provider}-{safe_zone}-{int(start)}-{int(end)}.json")


def _read_cache(path: Path) -> Optional[tuple[float, list[IntensityPoint]]]:
    try:
        payload = json.loads(path.read_text())
        points = [IntensityPoint(**{key: row[key] for key in _INTENSITY_FIELDS})
                  for row in payload.get("points") or []]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not points:
        return None
    return float(payload.get("fetched_at") or 0.0), points


def _write_cache(path: Path, points: Sequence[IntensityPoint],
                 fetched_at: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": fetched_at,
            "source": points[0].source if points else "",
            "points": [p.to_dict() for p in points],
        }
        path.write_text(json.dumps(payload, indent=2) + "\n")
    except OSError:
        pass  # a cache miss next time is not a failure worth reporting
