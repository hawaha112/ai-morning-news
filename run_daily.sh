#!/bin/bash
# ============================================================
# AI Morning Briefing - 每日定时任务运行脚本
# 由 launchd 在每天早上 7:00 自动调用
# ============================================================

set -e

# 项目路径：自动定位到脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
LOG_FILE="$PROJECT_DIR/daily_run.log"

# 从外部 .env 文件加载敏感配置（TG_BOT_TOKEN, TG_CHAT_ID, BRIEFING_URL）
ENV_FILE="$HOME/.config/ai-briefing/.env"
if [ -f "$ENV_FILE" ]; then
    # shellcheck source=/dev/null
    source "$ENV_FILE"
fi

# Telegram 发送函数（token 未配置时静默跳过）
send_tg() {
    if [ -z "$TG_BOT_TOKEN" ] || [ -z "$TG_CHAT_ID" ]; then
        return 0
    fi
    local message="$1"
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "{\"chat_id\": \"${TG_CHAT_ID}\", \"text\": $(printf '%s' "$message" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'), \"parse_mode\": \"HTML\", \"disable_web_page_preview\": false}" \
        > /dev/null 2>&1 || true
}

# 记录时间戳
{
    echo ""
    echo "========================================"
    echo "运行时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "========================================"
} >> "$LOG_FILE"

cd "$PROJECT_DIR"

# 第一步：检查 LLM 服务是否可用
LLM_URL=$(python3 -c "import json; print(json.load(open('config.json'))['llm']['base_url'])" 2>/dev/null || echo "http://127.0.0.1:10531/v1")
echo "检查 LLM 服务 ($LLM_URL)..." >> "$LOG_FILE"
if curl -s --connect-timeout 5 "${LLM_URL}/models" > /dev/null 2>&1; then
    echo "  LLM 服务正常" >> "$LOG_FILE"
else
    echo "  LLM 服务不可用，使用 --no-llm 模式" >> "$LOG_FILE"
    NO_LLM="--no-llm"
fi

# 第二步：运行新闻抓取脚本
echo "开始抓取新闻..." >> "$LOG_FILE"
if ! python3 "$PROJECT_DIR/fetch_news.py" ${NO_LLM:-} >> "$LOG_FILE" 2>&1; then
    FETCH_STATUS=$?
    echo "  新闻抓取失败，退出码: $FETCH_STATUS" >> "$LOG_FILE"
    send_tg "<b>AI 早报生成失败</b>
退出码: $FETCH_STATUS，请检查 daily_run.log"
    exit 1
fi
echo "  新闻抓取完成" >> "$LOG_FILE"

# 从 stats.json 读取文章数量
STATS_FILE="$PROJECT_DIR/output/stats.json"
if [ -f "$STATS_FILE" ]; then
    ARTICLE_COUNT=$(python3 -c "import json; print(json.load(open('$STATS_FILE'))['article_count'])" 2>/dev/null || echo "?")
else
    ARTICLE_COUNT="?"
fi

# 健康检查：文章数过低时告警
HEALTH_WARNING=""
if [ "$ARTICLE_COUNT" != "?" ] && [ "$ARTICLE_COUNT" -lt 3 ] 2>/dev/null; then
    echo "  文章数异常偏低: $ARTICLE_COUNT" >> "$LOG_FILE"
    HEALTH_WARNING="文章数异常偏低（仅 ${ARTICLE_COUNT} 条），部分 RSS 源可能不可用"
fi

# 第三步：部署到 GitHub Pages
DEPLOY_OK=false
echo "开始部署..." >> "$LOG_FILE"
cd "$PROJECT_DIR/output"
if [ -d ".git" ]; then
    rm -f .git/index.lock .git/HEAD.lock 2>/dev/null
    git add -A
    if ! git diff --cached --quiet; then
        git commit -m "Daily update: $(date '+%Y-%m-%d %H:%M')" >> "$LOG_FILE" 2>&1
        if git push origin main >> "$LOG_FILE" 2>&1; then
            echo "  部署成功" >> "$LOG_FILE"
            DEPLOY_OK=true
        else
            echo "  push 失败，尝试 pull --rebase 后重试" >> "$LOG_FILE"
            git pull --rebase origin main >> "$LOG_FILE" 2>&1 || true
            git push origin main >> "$LOG_FILE" 2>&1 && DEPLOY_OK=true
        fi
    else
        echo "  没有新的变更需要部署" >> "$LOG_FILE"
        DEPLOY_OK=true
    fi
else
    echo "  output 目录没有 git 配置，跳过部署" >> "$LOG_FILE"
fi

# 第四步：Telegram 通知
TODAY=$(date '+%Y年%m月%d日')
BRIEFING_URL="${BRIEFING_URL:-}"
if [ "$DEPLOY_OK" = true ]; then
    send_tg "<b>AI 早报 · ${TODAY}</b>

今日共收录 ${ARTICLE_COUNT} 条 AI 资讯
${NO_LLM:+LLM 服务不可用，本次跳过了深度分析
}${HEALTH_WARNING:+${HEALTH_WARNING}
}
<a href=\"${BRIEFING_URL}\">点击阅读今日早报</a>"
else
    send_tg "<b>AI 早报 · ${TODAY}</b>

已抓取 ${ARTICLE_COUNT} 条资讯，但部署失败
页面未更新，请检查 git 配置"
fi

echo "每日任务完成" >> "$LOG_FILE"
