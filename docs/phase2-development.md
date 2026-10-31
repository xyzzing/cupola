# Cupola Phase 2 Development Plan

## Overview
Smart backend management and performance optimization to reduce manual configuration and prevent compatibility issues between models, hardware, and the llama.cpp backend.

## Phase 2 Features

### 1. Backend Version & Feature Management

**Version Detection**
- Query llama-server binary for version information
- Track backend version in Cupola state/config
- Display version in `cupola doctor` output

**Feature Detection**
- Test which features the binary supports:
  - Flash attention (`--flash-attn`)
  - KV cache quantization types (q4_0, q8_0, f16)
  - ROCm-specific optimizations
  - Newer architecture support (Mamba, etc.)
- Cache feature detection results to avoid repeated testing
- Update detection when binary changes

**Compatibility Validation**
- Validate model requirements against binary capabilities
- Warn when profile settings exceed binary support
- Error when model requires features not in current binary

**Update Command**
```bash
cupola update --backend              # Update to latest
cupola update --backend 0.2.5        # Update to specific version
cupola update --backend --recompile  # Force recompile
```
- Download/clone specified version
- Build from source
- Verify build succeeded
- Update config/state with new version

**Doctor Report Enhancement**
```
llama-server version: 0.2.3
  ✓ flash_attn supported
  ✓ q4_0 cache supported
  ⚠ ROCm optimizations (added in 0.3.0) not available
  → Run 'cupola update --backend' to get latest features
```

### 2. Automatic Context Size Benchmarking

**Context Size Testing**
- Run llama-bench with progressively larger context sizes
- Measure VRAM usage at each size
- Find maximum size that fits within 2.5GB headroom requirement
- Test with actual model weights (not synthetic)

**Profile Recommendations**
- Suggest optimal `ctx_size` based on hardware and model
- Show VRAM headroom at recommended size
- Warn if current profile exceeds optimal size

**Bench Command**
```bash
cupola bench --profile coding
cupola bench --profile coding --context-sizes 8192,16384,32768,65536
```

Output:
```
Running llama-bench with profile 'coding'...
Model: qwen2.5-27b-q4_k_m.gguf
GPU: AMD Radeon RX 7900 XTX (24GB VRAM)

Benchmark results:
  Tokens/sec: 45.2
  Latency (first token): 1.2s
  VRAM used: 18.7GB / 24.0GB (78%)

Context size test:
  Max working: 49152 tokens (VRAM: 22.1GB)
  Failed at: 65536 tokens (VRAM: 25.8GB - OOM)

Recommendation: Set ctx_size to 49152 for optimal performance
```

### 3. Model-Backend Compatibility Matrix

**Feature Requirements Tracking**
- Maintain database of model requirements:
  - Minimum llama.cpp version
  - Required features (flash_attn, specific cache types)
  - Architecture support
- Validate against current binary before launching

**Validation Warnings**
```bash
$ cupola validate my-profile
Warning: Model qwen2.5-27b benefits from flash_attn,
         but your llama-server binary doesn't support it.
         Run 'cupola update --backend' to improve performance.
```

**Upgrade Guidance**
- Explain why recompilation is needed in plain language
- Show performance impact of missing features
- Provide estimated build time

## Implementation Priority

1. **Version/feature detection** (foundation for everything else)
2. **Compatibility validation** (prevents user errors)
3. **Bench command** (performance optimization)
4. **Update command** (workflow completion)

## Technical Notes

- Use existing `llama-bench` tool from llama.cpp installation
- Feature detection should be cached and invalidated on binary change
- Version detection should parse llama-server `--version` output
- Compatibility matrix should be maintainable (JSON/YAML config)
- Bench results should be comparable across runs

## Success Criteria

- User can run `cupola doctor` and see clear compatibility status
- User can run `cupola bench` to find optimal settings
- User gets warnings before launching incompatible model/backend combos
- User can easily update backend with `cupola update --backend`
- All changes are backward compatible with existing profiles
