#!/bin/bash
set -euo pipefail
git add data/
git diff --staged --quiet && echo none || echo changes
