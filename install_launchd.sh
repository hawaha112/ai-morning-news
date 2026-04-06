#!/bin/bash
# ============================================================
# 安装 AI Morning Briefing 本机定时任务
# 用法: bash install_launchd.sh
# ============================================================

PLIST_NAME="com.hawaha.ai-morning-briefing"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
RUN_SCRIPT="$(cd "$(dirname "$0")" && pwd)/run_daily.sh"

echo "🔧 安装 AI Morning Briefing 定时任务..."

# 给运行脚本加执行权限
chmod +x "$RUN_SCRIPT"
echo "  ✅ run_daily.sh 已设置执行权限"

# 如果已有旧任务，先卸载
if launchctl list | grep -q "$PLIST_NAME"; then
    echo "  📦 卸载旧版本..."
    launchctl unload "$PLIST_DST" 2>/dev/null
fi

# 复制 plist 到 LaunchAgents
cp "$PLIST_SRC" "$PLIST_DST"
echo "  ✅ plist 已复制到 $PLIST_DST"

# 加载定时任务
launchctl load "$PLIST_DST"
echo "  ✅ 定时任务已加载"

# 验证
if launchctl list | grep -q "$PLIST_NAME"; then
    echo ""
    echo "🎉 安装成功！"
    echo "  ⏰ 每天早上 7:00 自动运行"
    echo "  📋 日志文件: $(dirname "$RUN_SCRIPT")/daily_run.log"
    echo ""
    echo "常用命令："
    echo "  手动运行一次:  bash $RUN_SCRIPT"
    echo "  查看日志:      tail -50 $(dirname "$RUN_SCRIPT")/daily_run.log"
    echo "  卸载定时任务:  launchctl unload $PLIST_DST"
    echo "  重新加载:      launchctl load $PLIST_DST"
else
    echo "  ❌ 加载失败，请检查 plist 文件格式"
    exit 1
fi
