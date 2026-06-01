# Security Policy

## Scope

This repository contains the **public visualization layer** of the FisherQuant quantitative research system. It is a fully static site — no server-side code, no authentication, no user data collection.

**In scope for security reports:**
- Cross-site scripting (XSS) vulnerabilities in the HTML/JS dashboard pages
- Dependency vulnerabilities in Python scripts (`generate_github_pages.py`, `deploy.sh`)
- Sensitive data accidentally committed (API keys, tokens, PII)
- GitHub Actions workflow security issues

**Out of scope:**
- The private agent backend (not in this repository)
- Market data accuracy or investment advice quality

---

## Reporting a Vulnerability

Please **do not** open a public GitHub Issue for security vulnerabilities.

Instead, report privately via GitHub's built-in mechanism:  
**Security → Report a vulnerability** (top of the repository page)

Or email: include "SECURITY" in the subject line and contact via the GitHub profile.

### What to include
- Description of the vulnerability
- Steps to reproduce
- Affected files or components
- Suggested fix (optional)

---

## Response Timeline

| Stage | Target |
|---|---|
| Acknowledgement | Within 72 hours |
| Initial assessment | Within 7 days |
| Fix or mitigation | Within 30 days for confirmed vulnerabilities |

---

## Supported Versions

Only the current `main` branch is actively maintained.
