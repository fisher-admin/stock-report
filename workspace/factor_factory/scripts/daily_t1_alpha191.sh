#!/bin/bash
# Daily T1 Alpha191 strategy run
# Runs after market close (15:30 CST)

set -e

LOG_DIR="/Users/fisher/.hermes/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/t1_alpha191_$(date +%Y%m%d).log"

echo "=== T1 Alpha191 Daily Run: $(date) ===" >> "$LOG_FILE"

# Set environment
export PYTHONPATH="/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts:$PYTHONPATH"
PYTHON="/Users/fisher/.openclaw/workspace/factor_factory/qlib_lab/venv/bin/python"
SCRIPTS="/Users/fisher/.openclaw/workspace/factor_factory/scripts"
OPENCLAW_WORKSPACE="/Users/fisher/.openclaw/workspace"
OPENCLAW_PUB="$OPENCLAW_WORKSPACE/stock-report"
WORKING_REPO="$OPENCLAW_WORKSPACE/stock_data/03-working/stock-report-repo"
STRATEGY_RESEARCH="$OPENCLAW_WORKSPACE/stock_data/03-working/strategy_research/t1_factor_v1"

# Step 1: Build factor panel (if needed)
echo "Step 1: Building factor panel..." >> "$LOG_FILE"
cd "$SCRIPTS"
$PYTHON build_gtja_alpha191_panel.py >> "$LOG_FILE" 2>&1

# Step 2: Generate T1 recommendation
echo "Step 2: Generating T1 recommendation..." >> "$LOG_FILE"
$PYTHON build_t1_portfolio.py >> "$LOG_FILE" 2>&1

# Step 3: Copy research state to publication directories
echo "Step 3: Copying research state..." >> "$LOG_FILE"
mkdir -p "$OPENCLAW_PUB/data/latest" "$WORKING_REPO/data/latest" "$STRATEGY_RESEARCH"
cp "$SCRIPTS/../outputs/t1_alpha191/research_state_t1.json" \
   "$OPENCLAW_PUB/data/latest/research_state_t1.json"
cp "$SCRIPTS/../outputs/t1_alpha191/research_state_t1.json" \
   "$WORKING_REPO/data/latest/research_state_t1.json"
cp "$SCRIPTS/../outputs/t1_alpha191/research_state_t1.json" \
   "$STRATEGY_RESEARCH/research_state.json"

# Step 4: Generate T1 execution data
echo "Step 4: Generating T1 execution data..." >> "$LOG_FILE"
$PYTHON generate_t1_execution.py >> "$LOG_FILE" 2>&1

# Step 5: Run publication layer
echo "Step 5: Running publication layer..." >> "$LOG_FILE"
cd /Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts
$PYTHON strategy_publication_layer.py >> "$LOG_FILE" 2>&1

# Step 6: Sync to web app
echo "Step 6: Syncing to web app..." >> "$LOG_FILE"
rsync -av "$OPENCLAW_PUB/data/latest/" "$WORKING_REPO/data/latest/" >> "$LOG_FILE" 2>&1
rsync -av "$OPENCLAW_PUB/data/recommendation_analytics/" "$WORKING_REPO/data/recommendation_analytics/" >> "$LOG_FILE" 2>&1

# Step 7: Push to GitHub Pages
echo "Step 7: Pushing to GitHub Pages..." >> "$LOG_FILE"
cd "$OPENCLAW_PUB"
git add -A
git commit -m "T1 Alpha191 daily update $(date +%Y%m%d)" >> "$LOG_FILE" 2>&1 || true
git push >> "$LOG_FILE" 2>&1 || echo "Git push failed (may need auth)" >> "$LOG_FILE"

echo "=== T1 Alpha191 Daily Run Complete: $(date) ===" >> "$LOG_FILE"
