#!/bin/bash
# ============================================================
# 安装 AI Morning Briefing 定时任务
# 用法: bash install_launchd.sh
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.hawaha.ai-morning-briefing"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
RUN_SCRIPT="$SCRIPT_DIR/run_daily.sh"

echo "安装 AI Morning Briefing 定时任务..."

chmod +x "$RUN_SCRIPT"

# 卸载旧版本
if launchctl list 2>/dev/null | grep -q "$PLIST_NAME"; then
    echo "  卸载旧版本..."
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi

# 从模板生成 plist（替换路径占位符为当前实际路径）
sed \
    -e "s|__SCRIPT_PATH__|$RUN_SCRIPT|g" \
    -e "s|__LOG_DIR__|$SCRIPT_DIR|g" \
    "$PLIST_SRC" > "$PLIST_DST"

echo "  plist 已安装到 $PLIST_DST"

launchctl load "$PLIST_DST"

if launchctl list 2>/dev/null | grep -q "$PLIST_NAME"; then
    echo ""
    echo "安装成功！每天早上 7:00 自动运行"
    echo ""
    echo "常用命令："
    echo "  手动运行:    bash $RUN_SCRIPT"
    echo "  查看日志:    tail -50 $SCRIPT_DIR/daily_run.log"
    echo "  卸载任务:    launchctl unload $PLIST_DST"
else
    echo "  加载失败，请检查 plist 文件"
    exit 1
fi
