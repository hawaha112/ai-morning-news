#!/bin/bash
# ============================================================
# AI Morning Briefing - 本机每日定时任务运行脚本
# 由 launchd 在每天早上 7:00 自动调用
# ============================================================

set -e

# 项目路径
PROJECT_DIR="$HOME/claude_workspace/pg4_FUTURE/ai-morning-news"
LOG_FILE="$PROJECT_DIR/daily_run.log"

# 从外部 .env 文件加载敏感配置（TG_BOT_TOKEN, TG_CHAT_ID, BRIEFING_URL）
ENV_FILE="$HOME/.config/ai-briefing/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
else
    echo "⚠️  配置文件不存在: $ENV_FILE，Telegram 通知将被跳过" >&2
fi

# Telegram 发送函数
send_tg() {
    local message="$1"
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "{\"chat_id\": ${TG_CHAT_ID}, \"text\": $(echo "$message" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'), \"parse_mode\": \"HTML\", \"disable_web_page_preview\": false}" \
        > /dev/null 2>&1
}

# 记录时间戳
echo "" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"
echo "🕐 运行时间: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"

cd "$PROJECT_DIR"

# 第一步：检查 Codex 代理是否在运行
echo "📡 检查 Codex 代理..." >> "$LOG_FILE"
if curl -s --connect-timeout 5 http://127.0.0.1:10531/v1/models > /dev/null 2>&1; then
    echo "  ✅ Codex 代理正常运行" >> "$LOG_FILE"
else
    echo "  ❌ Codex 代理未运行，跳过 LLM 分析" >> "$LOG_FILE"
    echo "  ℹ️  将使用 --no-llm 模式运行" >> "$LOG_FILE"
    NO_LLM="--no-llm"
fi

# 第二步：运行新闻抓取脚本
echo "📰 开始抓取新闻..." >> "$LOG_FILE"
python3 "$PROJECT_DIR/fetch_news.py" ${NO_LLM:-} >> "$LOG_FILE" 2>&1
FETCH_STATUS=$?

if [ $FETCH_STATUS -ne 0 ]; then
    echo "  ❌ 新闻抓取失败，退出码: $FETCH_STATUS" >> "$LOG_FILE"
    send_tg "❌ <b>AI 早报生成失败</b>

新闻抓取脚本出错，退出码: $FETCH_STATUS
请检查日志: daily_run.log"
    exit 1
fi
echo "  ✅ 新闻抓取完成" >> "$LOG_FILE"

# 从 stats.json 读取文章数量（由 fetch_news.py 生成）
STATS_FILE="$PROJECT_DIR/output/stats.json"
if [ -f "$STATS_FILE" ]; then
    ARTICLE_COUNT=$(python3 -c "import json; print(json.load(open('$STATS_FILE'))['article_count'])" 2>/dev/null || echo "?")
else
    ARTICLE_COUNT="?"
fi

# 健康检查：文章数过低时告警
if [ "$ARTICLE_COUNT" != "?" ] && [ "$ARTICLE_COUNT" -lt 3 ] 2>/dev/null; then
    echo "  ⚠️ 文章数异常偏低: $ARTICLE_COUNT" >> "$LOG_FILE"
    HEALTH_WARNING="⚠️ 文章数异常偏低（仅 ${ARTICLE_COUNT} 条），部分 RSS 源可能不可用"
fi

# 第三步：部署到 GitHub Pages
DEPLOY_OK=false
echo "🚀 开始部署..." >> "$LOG_FILE"
if [ -n "$GH_TOKEN" ]; then
    bash "$PROJECT_DIR/deploy.sh" >> "$LOG_FILE" 2>&1
    DEPLOY_STATUS=$?
    if [ $DEPLOY_STATUS -eq 0 ]; then
        echo "  ✅ 部署成功" >> "$LOG_FILE"
        DEPLOY_OK=true
    else
        echo "  ❌ 部署失败，退出码: $DEPLOY_STATUS" >> "$LOG_FILE"
    fi
else
    # 尝试用 output 目录已有的 git 配置直接推送
    echo "  ℹ️  GH_TOKEN 未设置，尝试用已有 git 配置推送..." >> "$LOG_FILE"
    cd "$PROJECT_DIR/output"
    if [ -d ".git" ]; then
        rm -f .git/index.lock .git/HEAD.lock 2>/dev/null
        git add -A
        if ! git diff --cached --quiet; then
            git commit -m "📰 Daily update: $(date '+%Y-%m-%d %H:%M')" >> "$LOG_FILE" 2>&1
            git push -u origin main --force >> "$LOG_FILE" 2>&1
            echo "  ✅ 部署成功（使用已有 git 配置）" >> "$LOG_FILE"
            DEPLOY_OK=true
        else
            echo "  ℹ️  没有新的变更需要部署" >> "$LOG_FILE"
            DEPLOY_OK=true
        fi
    else
        echo "  ❌ 无法部署：output 目录没有 git 配置且 GH_TOKEN 未设置" >> "$LOG_FILE"
    fi
fi

# 第四步：推送到 Telegram
TODAY=$(date '+%Y年%m月%d日')
if [ "$DEPLOY_OK" = true ]; then
    send_tg "☀️ <b>AI 早报 · ${TODAY}</b>

📰 今日共收录 ${ARTICLE_COUNT} 条 AI 资讯
${NO_LLM:+⚠️ Codex 代理未运行，本次跳过了 LLM 深度分析}
${HEALTH_WARNING:+${HEALTH_WARNING}}

👉 <a href=\"${BRIEFING_URL}\">点击阅读今日早报</a>"
    echo "  ✅ Telegram 推送成功" >> "$LOG_FILE"
else
    send_tg "⚠️ <b>AI 早报 · ${TODAY}</b>

📰 已抓取 ${ARTICLE_COUNT} 条资讯，但部署失败
页面未更新，请检查 git 配置"
    echo "  ⚠️ Telegram 推送成功（部署失败通知）" >> "$LOG_FILE"
fi

echo "🎉 每日任务完成！" >> "$LOG_FILE"
echo "🌐 ${BRIEFING_URL}" >> "$LOG_FILE"
