# Security Policy

## Reporting Vulnerabilities

To report a security vulnerability, please email the maintainers at security@cupola-project.org or open an issue with the `[security]` label.

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Response Time

We aim to acknowledge security reports within 48 hours and provide a fix within 7 days for critical issues.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Security Considerations

Cupola follows these security principles:
- Loopback-only binding by default
- No in-path execution of agent tools
- Bounded metadata reading with validation
- SHA-256 hash verification for model files
- Private socket communication