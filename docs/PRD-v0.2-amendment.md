# cupola PRD v0.2 amendment — capacity, inspection, and the accepted boundary

Status: draft for maintainer review. Base: current PRD + ADR-001..005.
Renamed from sinter 2026-10-31 (issue #3, PR #4); name strings updated in
place. Role in the program: the engine test cell + the watch-dome.

## Accepted direction

- ADR-005 (merged via #2, string fix #5): engine abstraction at exactly two
  seams - spawn-command construction and capability probing. FSM, admission,
  telemetry, and `contracts.py` stay engine-blind. A second backend lands
  behind a profile field naming its module, with its own measured admission
  numbers before certify. llama-server/ROCm remains the only certified
  backend.

## New requirements

- FR-C1. Incoming inspection (certify-before-serve): `cupola update` runs a
  certification room sweep (windtunnel, stub-or-live) on a newly installed
  engine binary; fail = hold the previous binary and record why. Closes the
  supplier-quality gap (the stale tune.py-config incident is the cautionary
  tale).
- FR-C2. Work-level backpressure: extend `InferenceGrant` from engine
  capacity to work capacity - per-session-class WIP limits, queue depth
  visible, spill policy (local -> cloud -> queue) explicit. Engine admission
  stays as is; this governs the queue in front of it.
- FR-C3. Sentinel/observability completion: the owner's in-progress
  telemetry work (metrics, Prometheus endpoint, Sentinel sessions) lands as
  the next feature slice on the renamed tree; nothing else blocks on it.

## Unchanged mandate

Zero-interruption velocity, conservative pre-flight memory admission
(weights + KV + recurrent + buffers + reserve <= VRAM), explicit failure
over silent degradation, loopback isolation, SCI carbon accounting.

## Non-goals

Multi-GPU and engine-specific flags until a second backend exists; any
in-request-path proxying (ADR-002); automatic work scheduling.
