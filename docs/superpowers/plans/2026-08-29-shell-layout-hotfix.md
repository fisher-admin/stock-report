# Shell Layout Hotfix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the GitHub Pages desktop layout by removing the unmatched closing tag that ejects navigation and content from the application shell.

**Architecture:** Keep the existing static-site renderer and visual system unchanged. Add one structural regression assertion to the current dependency-free Node render suite, then make the one-line template correction and verify it in automated tests and real browsers.

**Tech Stack:** JavaScript ES modules, Node.js built-in test assertions, static HTML/CSS, GitHub Pages.

---

### Task 1: Reproduce the malformed sidebar in an automated test

**Files:**
- Modify: `tests/render.test.mjs`
- Test: `tests/render.test.mjs`

- [ ] **Step 1: Add a failing shell-balance assertion**

Add this check after the existing FisherQuant branding check:

```javascript
check('侧栏标签必须配对，导航与主内容不得被挤出应用骨架', () => {
  const html = RENDERERS.dashboard(model);
  const sidebar = html.match(/<aside class="sidebar">([\s\S]*?)<\/aside>/)?.[1];
  assert.ok(sidebar, '缺少 sidebar 页面骨架');
  const openDivs = (sidebar.match(/<div\b/g) || []).length;
  const closeDivs = (sidebar.match(/<\/div>/g) || []).length;
  assert.equal(closeDivs, openDivs, `sidebar 内 div 标签不配对：开 ${openDivs} / 闭 ${closeDivs}`);
  assert.ok(sidebar.includes('<nav class="side-nav"'), '主导航不在 sidebar 内');
});
```

- [ ] **Step 2: Run the test and confirm the regression is caught**

Run: `node tests/render.test.mjs`

Expected: exit code `1` with the new check reporting unequal opening and closing `div` counts.

### Task 2: Apply the minimal shell correction

**Files:**
- Modify: `assets/scripts/v2/render/shell.js`
- Test: `tests/render.test.mjs`

- [ ] **Step 1: Remove the unmatched closing tag**

Change the brand-to-navigation boundary from:

```javascript
        </a>
      </div>
      </div>
      <nav class="side-nav" aria-label="主导航">
```

to:

```javascript
        </a>
      </div>
      <nav class="side-nav" aria-label="主导航">
```

- [ ] **Step 2: Run the focused regression test**

Run: `node tests/render.test.mjs`

Expected: exit code `0`, including `ok - 侧栏标签必须配对，导航与主内容不得被挤出应用骨架`.

- [ ] **Step 3: Run the complete repository test suite**

Run:

```bash
python3 -m unittest discover tests -p 'test_*.py' -v
node tests/render.test.mjs
node tests/dual-track-render.test.mjs
```

Expected: all Python and Node tests pass with no failures.

### Task 3: Verify the rendered site and deploy

**Files:**
- Modify: none
- Verify: `index.html` and the generated DOM from `assets/scripts/v2/render/shell.js`

- [ ] **Step 1: Start a local static server**

Run: `python3 -m http.server 8765 --bind 127.0.0.1`

Expected: the server listens on `http://127.0.0.1:8765/`.

- [ ] **Step 2: Inspect desktop and narrow layouts in a real browser**

At desktop width, confirm `.app-shell` contains `.sidebar` and `.content`, `.main` begins within the initial viewport, and the navigation remains inside the sidebar. At a narrow viewport, confirm navigation and main content remain visible and no horizontal overflow blocks the page.

- [ ] **Step 3: Commit the tested hotfix**

Run:

```bash
git add tests/render.test.mjs assets/scripts/v2/render/shell.js docs/superpowers/plans/2026-08-29-shell-layout-hotfix.md
git commit -m "fix: restore stock report shell layout"
```

Expected: one commit containing the regression test, one-line renderer fix, and this plan.

- [ ] **Step 4: Push and verify GitHub Pages**

Run: `git push origin main`

Expected: push succeeds. After GitHub Pages publishes the new commit, reload `https://fisher-admin.github.io/stock-report/` and confirm the live desktop homepage displays the navigation and main content in the initial viewport with no console errors.
