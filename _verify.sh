#!/bin/bash
# Run lint + tests from the repo root (wherever this checkout lives).
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
.venv/bin/pip install -e . --quiet 2>&1 | tail -3
echo "=== RUFF ==="
.venv/bin/ruff check src/ tests/
echo "=== PYTEST ==="
.venv/bin/python -m pytest tests/ -v 2>&1
