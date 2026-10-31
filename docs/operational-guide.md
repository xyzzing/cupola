# Cupola Operational Guide

## Quick Start

```bash
cupola doctor          # Check system health
cupola validate coding # Validate profile
cupola plan coding     # Plan settings
cupola up coding       # Start llama-server
cupola status          # Check status
cupola down            # Stop llama-server
```

## Commands

- `cupola doctor [--json]` - Read-only hardware/backend report
- `cupola validate <profile> [--json]` - Validate profile without loading weights
- `cupola plan <profile> [--json]` - Explain requested vs effective settings
- `cupola up <profile> [--timeout N] [--foreground]` - Start llama-server
- `cupola down [--timeout N]` - Stop the running llama-server
- `cupola status [--json]` - Show current instance status

## Profiles

Defined in `~/.config/cupola/cupola.toml` or `profiles/*.toml`.

```toml
[profiles.coding]
weights_path = "/path/to/model.gguf"
backend_binary = "/path/to/llama-server"
device = "ROCm0"
ctx_size = 131072
n_gpu_layers = 63
port = 8080
```

## Logs

- Operational: `~/.local/state/cupola/logs/` (JSON lines)
- Backend: `~/.local/state/cupola/logs/backend-<profile>.log`

## State Files

- `~/.local/state/cupola/instance.json` - Current instance
- `~/.local/state/cupola/state.json` - Supervisor state
- `/run/user/$UID/cupola/lock` - Supervisor lock

## Troubleshooting

- Port in use: `ss -tlnp | grep :8080`
- VRAM insufficient: `cupola plan <profile>`
- Backend not starting: check `backend-<profile>.log`
- Permission denied: ensure `rocm` group membership