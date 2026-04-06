#!/bin/bash
# ============================================================
# AI Morning Briefing - 部署到 GitHub Pages
# 用法: GH_TOKEN=xxx bash deploy.sh
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/output"
REPO_NAME="ai-morning-briefing"

if [ -z "$GH_TOKEN" ]; then
    echo "请设置环境变量 GH_TOKEN: export GH_TOKEN=your_github_token"
    exit 1
fi

REMOTE_URL="https://${GH_TOKEN}@github.com/hawaha112/$REPO_NAME.git"

echo "开始部署..."

cd "$OUTPUT_DIR"
rm -f .git/index.lock 2>/dev/null

if [ ! -d ".git" ]; then
    git init
    git remote add origin "$REMOTE_URL" 2>/dev/null || git remote set-url origin "$REMOTE_URL"
    git checkout -b main
fi

git config user.email "hawaha113@protonmail.com"
git config user.name "hawaha112"
git remote set-url origin "$REMOTE_URL"

git add -A
if git diff --cached --quiet; then
    echo "没有新的变更需要部署"
else
    DATE=$(date "+%Y-%m-%d %H:%M")
    git commit -m "Daily update: $DATE"
    if ! git push origin main; then
        git pull --rebase origin main
        git push origin main
    fi
    echo "部署成功！"
fi
