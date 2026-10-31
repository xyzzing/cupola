# Contributing to Cupola

Thank you for your interest in contributing to Cupola!

## Development Setup

```bash
# Clone the repository
git clone https://github.com/cupola-project/cupola.git
cd cupola

# Install dependencies
pip install -e .

# Run tests
python3 -m pytest tests/ -v
```

## Code Style

- Follow PEP 8 conventions
- Use type hints for all function signatures
- Run `ruff check` before submitting PRs
- Keep functions focused and well-documented

## Testing

- All new features must include tests
- Run the full test suite before submitting: `python3 -m pytest tests/`
- Use the verification script: `python3 .agentic/verify.py`

## Pull Request Process

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes with tests
4. Run `python3 .agentic/verify.py` to ensure all checks pass
5. Commit your changes (`git commit -m 'Add amazing feature'`)
6. Push to the branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

## Issue Reporting

- Use the issue templates provided
- Include system information (ROCm version, GPU model, Python version)
- Provide reproduction steps for bugs
- Be specific about expected vs actual behavior

## Architecture

Cupola follows a strict separation of concerns:
- **Supervisor**: Process lifecycle management
- **Admission**: Pre-flight resource checks
- **Hardware**: System probing
- **Backend**: Version and feature management
- **Bench**: Performance benchmarking

See [ARCHITECTURE.md](ARCHITECTURE.md) for details.