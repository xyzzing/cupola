# Cupola Installation Guide

## Prerequisites

- Linux (tested on Fedora 44)
- Python 3.11+
- ROCm stack 7.1.1+ (for AMD GPU support)
- ROCm-enabled llama.cpp build with `llama-server`
- Model weights in GGUF format

## From Source

```bash
git clone https://github.com/zac-ch/cupola.git
cd cupola
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Verify

```bash
cupola --version
cupola doctor
```

## XDG Directories

| Directory | Purpose | Default |
|-----------|---------|---------|
| `XDG_CONFIG_HOME/cupola` | Configuration | `~/.config/cupola` |
| `XDG_STATE_HOME/cupola` | State, logs | `~/.local/state/cupola` |
| `XDG_RUNTIME_DIR/cupola` | Lock, PID | `/run/user/$UID/cupola` |

## Uninstall

```bash
rm -rf .venv
pip uninstall cupola  # if system-wide
rm -rf ~/.config/cupola ~/.local/state/cupola  # optional
```