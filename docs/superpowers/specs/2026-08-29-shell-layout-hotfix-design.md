# Shell Layout Hotfix Design

## Problem

The deployed desktop page loads its data and produces no JavaScript errors, but the main content begins below the viewport. The sidebar brand remains visible while navigation and content are laid out as full-width blocks underneath it.

The regression was introduced in commit `2b582628`. `renderShell()` emits one extra `</div>` immediately after the brand block. The browser repairs the malformed markup by closing `.sidebar` and `.app-shell` early, moving `.side-nav`, `.side-subnav`, and `.content` outside the flex shell.

## Scope

Make the smallest production change that restores the existing layout:

- Remove the unmatched closing tag from `assets/scripts/v2/render/shell.js`.
- Add a regression assertion to the existing Node render test so the rendered shell must keep navigation and content inside their intended parents.
- Do not change stock data, ranking logic, visual styling, navigation labels, or publishing contracts.

## Verification

Verification must cover both structure and the deployed symptom:

1. Run the new regression test before the fix and confirm it fails because `.sidebar` closes before `.side-nav`.
2. Apply the one-line markup fix and confirm the regression test passes.
3. Run the complete Node and Python test suites already present in the repository.
4. Serve the site locally and inspect desktop and narrow viewports in a real browser.
5. Push the fix to `main`, wait for GitHub Pages to publish it, and confirm the live homepage has visible main content at the top of the page with no console errors.

## Rollback

If the hotfix causes an unexpected regression, revert the hotfix commit. The change is isolated to one closing tag and one test assertion, so rollback does not affect published stock data.
