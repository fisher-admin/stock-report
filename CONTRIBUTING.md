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
# Open any .html file directly in a browser — no build step required
open index.html
```

The site is fully static. All pages read from `data/latest/*.json` locally.

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

## Local Development

### Running the dashboard locally

The HTML pages use `fetch()` to load data from `data/latest/`, which fails on the `file://` protocol in most browsers. You need a local HTTP server.

**Option 1 — Python (recommended, no install needed)**

```bash
cd stock-report
python3 -m http.server 8080
```

Then open `http://localhost:8080` in your browser.

**Option 2 — VS Code Live Server**

Install the [Live Server extension](https://marketplace.visualstudio.com/items?itemName=ritwickdey.LiveServer), right-click `index.html` in the file explorer, and select **Open with Live Server**.

---

### Using local test data

By default all pages load from `data/latest/`. For frontend development you can point pages at local fixtures instead:

1. Create a `data/test/` folder and copy any JSON files from `data/latest/` into it
2. In the HTML file you are editing, temporarily change the fetch path from `data/latest/` to `data/test/`
3. Edit the JSON fixtures to test different states
4. Revert the path before submitting your PR

---

### Running generate_github_pages.py locally

```bash
python3 generate_github_pages.py
```

The script reads from the `data/` directory and writes output files locally. Check the script's top-level comments for any required environment variables before running.
