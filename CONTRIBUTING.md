# Contributing to FisherQuant · stock-report

Thank you for your interest in contributing! This repository is the **public output layer** of the FisherQuant quantitative research system. The private agent backend is not included, but the frontend dashboard, data schema, and deployment tooling are fully open for contributions.

---

## What You Can Contribute

| Area | Files | Examples |
|---|---|---|
| **Frontend / UI** | `*.html`, `assets/`, `css/` | Fix display bugs, improve layout, add dark mode |
| **Data Schema** | `data/latest/*.json` | Propose new fields, improve traceability |
| **Deployment Tooling** | `deploy.sh`, `generate_github_pages.py`, `.github/workflows/` | Improve CI, add validation steps |
| **Documentation** | `README.md`, `VERIFICATION.md` | Fix typos, improve clarity |

## What Is Out of Scope

- Private agent backend logic (not in this repo)
- Proprietary strategy parameters
- Changes that break the `data/latest/*.json` data contract without a migration path

---

## Getting Started

```bash
git clone https://github.com/fisher-admin/stock-report.git
cd stock-report
# Serve the static site over HTTP from the repository root
python3 -m http.server 8080
# then open http://localhost:8080
```

The site is fully static, but the dashboard pages fetch JSON files from `data/latest/`. Serving the repository over HTTP avoids the `file://` fetch restrictions that some browsers apply.

If you prefer VS Code, the Live Server extension works as well: right-click `index.html` and choose **Open with Live Server**.

## Local Development

### Run the dashboard locally

1. Start a local server from the repository root:

   ```bash
   python3 -m http.server 8080
   ```

2. Open `http://localhost:8080/index.html` in your browser.
3. Navigate to the other dashboard pages from there as needed.

### Work with local test data

All frontend pages read from `data/latest/*.json` by default. For frontend experiments, point the relevant fetch paths at copies of those files stored under a temporary fixture directory such as `data/dev/`, or replace `data/latest/` with fixture paths in your local branch before testing UI changes.

Before opening a PR, switch any temporary fixture paths back to the checked-in `data/latest/` contract.

### Regenerate published assets locally

`generate_github_pages.py` currently expects the maintainer's `~/.openclaw/workspace/stock_data` layout. If you have that data workspace available locally, you can run:

```bash
python3 generate_github_pages.py
```

External contributors who are working from a plain repository clone can usually skip this step for frontend-only or documentation changes and validate their work through the local HTTP server instead.

Use `python3 generate_github_pages.py --deploy` only when you explicitly intend to publish from a trusted environment.

---

## Submitting a Pull Request

1. **Open an issue first** for non-trivial changes (UI redesigns, schema changes, new pages). This avoids wasted effort.
2. Fork the repository and create a branch: `git checkout -b fix/your-description`
3. Make your changes. If modifying data schema, update `VERIFICATION.md` acceptance criteria accordingly.
4. Submit a PR with a clear description of what changed and why.

## Code Style

- HTML: semantic elements, no inline styles
- JavaScript: vanilla JS only, no frameworks (keep the site dependency-free)
- JSON: follow the existing schema patterns in `data/latest/`

---

## Questions?

Open an issue with the `question` label. Response time is typically within a few days.
