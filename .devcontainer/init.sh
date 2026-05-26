#!/usr/bin/env bash
set -e

echo "==> Installing Python dependencies..."
pip install --quiet -r /workspace/backend/requirements-dev.txt

echo "==> Installing Node dependencies..."
cd /workspace/frontend && npm install --silent

echo ""
echo "Dev environment ready."
echo ""
echo "  Run backend tests : cd /workspace/backend && pytest"
echo "  Run frontend tests : cd /workspace/frontend && npx vitest run"
echo ""
