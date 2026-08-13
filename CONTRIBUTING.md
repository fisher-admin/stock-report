# Contributing

This repository is the **public** slice of an A-share research pipeline: architecture, source snapshots, tests, GitHub Pages, and allowlisted result summaries.

It is not a live brokerage, not a signal dump, and not a request line for private market data.

## Before you open an issue

1. Read [README.md](README.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
2. Confirm the page or file you mean is in this repo, not in the local runtime.
3. Do **not** paste API tokens, local paths under a home directory, raw AI transcripts, or per-stock recommendation history.

Useful issue types: broken Pages rendering, public-contract mistakes, test gaps, documentation errors, and patches that keep the observe-only boundary.

## Pull requests

- Keep secrets and local databases out of the tree. `scripts/enforce_public_boundary.py` is fail-closed.
- Public Python and Node tests must pass (see README).
- Do not add order routing, broker APIs, or anything that places trades.
- Do not expand `config/public-result-allowlist.txt` to include row-level recommendation history or raw AI output.

## What this project will not accept

- Requests for unpublished candidate lists, full history CSVs, or local `.db` / `.parquet` files
- Changes that present observation output as a buy/sell instruction
- License relicensing away from MIT without the maintainer’s written agreement
