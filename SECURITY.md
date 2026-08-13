# Security

## What belongs in this repository

Source, tests, docs, Pages assets, and **allowlisted** public JSON summaries. Credentials, broker sessions, Tushare tokens, and local market databases stay on the maintainer machine.

## Reporting a vulnerability

If you believe this **public** tree contains secrets, path leaks, or a way to exfiltrate non-allowlisted data from GitHub Pages:

1. Do not open a public issue that repeats the secret.
2. Use GitHub’s private advisory flow for [fisher-admin/stock-report](https://github.com/fisher-admin/stock-report/security/advisories/new) if available, or email the maintainer via the GitHub profile.
3. Include the file path, a short reproduction, and whether Pages already served the data.

Market losses, model error, or “the signal was wrong” are not security issues.

## Maintainer checklist

- Run `python3 -m unittest discover tests -p 'test_*.py' -v` before publishing.
- Never commit files outside `config/public-result-allowlist.txt` under `data/`.
- Treat environment-variable lookups in source as allowed; literal tokens in source as a release blocker.
