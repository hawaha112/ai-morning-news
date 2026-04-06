#!/bin/bash
# ============================================================
# AI Morning Briefing - 部署到 GitHub Pages
# 用法: bash deploy.sh
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/output"
REPO_NAME="ai-morning-briefing"
# ⚠️ 安全提醒：不要在脚本中硬编码 token！
# 请通过环境变量设置：export GH_TOKEN=ghp_your_token
# 或使用 gh auth login 配置 GitHub CLI 认证
if [ -z "$GH_TOKEN" ]; then
    echo "❌ 请设置环境变量 GH_TOKEN: export GH_TOKEN=your_github_token"
    exit 1
fi
REMOTE_URL="https://${GH_TOKEN}@github.com/hawaha112/$REPO_NAME.git"

echo "🚀 开始部署 AI Morning Briefing..."

cd "$OUTPUT_DIR"

# 清理可能残留的 lock 文件
rm -f .git/index.lock

# 初始化或更新 git
if [ ! -d ".git" ]; then
    echo "📦 初始化 Git 仓库..."
    git init
    git remote add origin "$REMOTE_URL" 2>/dev/null || git remote set-url origin "$REMOTE_URL"
    git checkout -b main
fi

# 确保 git 身份配置
git config user.email "hawaha113@protonmail.com"
git config user.name "hawaha112"

# 确保 remote URL 包含 token
git remote set-url origin "$REMOTE_URL"

# 提交并推送
git add -A
if git diff --cached --quiet; then
    echo "ℹ️  没有新的变更需要部署"
else
    DATE=$(date "+%Y-%m-%d %H:%M")
    git commit -m "📰 Daily update: $DATE"
    git push -u origin main --force
    echo "✅ 部署成功！"
    echo "🌐 访问: https://hawaha112.github.io/$REPO_NAME/"
fi
