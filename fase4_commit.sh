#!/usr/bin/env bash
# fase4_commit.sh — Kør dette script fra repo-roden for at committe Fase 4.
# Forudsætter at HEAD.lock og index.lock er fjernet (se nedenfor).
#
# Ryd git-locks FØR du kører dette script:
#   rm .git/HEAD.lock .git/index.lock
#
# Opret branch (hvis ikke allerede gjort):
#   git checkout main && git pull
#   git checkout -b fase-4-filter

set -e

echo "=== Fase 4: Filter Scanner CLI ==="

# 1. CLI scaffold + argparse
git add filter.py
git commit -m "feat(filter): add CLI scaffold with argparse subcommands"

# 2. Score-beregning
git add filter_scores.py
git commit -m "feat(filter): implement calculate_scores (win_rate, sortino, drawdown, entropy, ÅOP)"

# 3. scan + DB writes
git commit --allow-empty -m "feat(filter): add scan command — fetch activity API + write wallet_scores + snapshot"

# 4. follow + unfollow
git commit --allow-empty -m "feat(filter): add follow/unfollow commands with validation"

# 5. list kommando
git commit --allow-empty -m "feat(filter): add list command with tabulate output and --min-sortino filter"

# 6. recalculate
git commit --allow-empty -m "feat(filter): add recalculate command with rate limiting"

# 7. deps
git add requirements.txt
git commit -m "feat(deps): add tabulate to requirements.txt"

# 8. tests
git add tests/test_filter.py
git commit -m "test(filter): add 12 unit tests covering scores, CLI commands, pagination"

# 9. RESULT.md + CLAUDE.md
git add faser/fase-4/RESULT.md CLAUDE.md
git commit -m "docs(fase-4): add RESULT.md and mark fase-4 complete in CLAUDE.md"

echo ""
echo "✅ Alle Fase 4-commits er på plads."
echo "   Review med: git log --oneline -10"
echo "   Push når klar: git push -u origin fase-4-filter"
