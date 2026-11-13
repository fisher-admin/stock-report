#!/bin/bash
# S3 failed its frozen validation and is retained only as a historical control.
# The scheduler may still invoke this compatibility entrypoint; it performs no data pulls,
# creates no new signals, and exits successfully so the rest of the daily closure is unaffected.
set -uo pipefail

echo "S3 archived_historical_only: forward tracking and signal expansion are disabled."
exit 0
