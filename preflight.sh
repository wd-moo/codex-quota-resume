#!/bin/zsh
# Codex 续航机器人 —— 环境自检
# 用法：zsh preflight.sh
PY=/Users/int/.workbuddy/binaries/python/versions/3.13.12/bin/python3
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "===== Codex 续跑机器人 自检 ====="
echo

echo "1) Codex 桌面版是否在运行"
if pgrep -f "ChatGPT.app/Contents/MacOS/ChatGPT" >/dev/null 2>&1; then
  echo "   ✅ Codex 桌面版在运行"
else
  echo "   ❌ Codex 桌面版没在运行 —— 请把要续跑的对话窗口打开"
fi

echo
echo "2) 辅助功能权限（UI 注入必需）"
if osascript -e 'tell application "System Events" to get name of first process' >/dev/null 2>&1; then
  echo "   ✅ 已授权，可以模拟按键"
  echo "      → 续跑方式：激活 Codex 窗口 + 粘贴「继续」+ 回车"
else
  echo "   ❌ 未授权，无法模拟按键"
  echo "      → 需要手动开：系统设置 → 隐私与安全性 → 辅助功能 → 打开 WorkBuddy"
  echo "      → 未授权时会自动降级为 CLI 续跑（codex exec resume），任务照样能续上"
fi

echo
echo "3) 防休眠"
if pgrep -x caffeinate >/dev/null 2>&1; then
  echo "   ✅ caffeinate 已在运行"
else
  echo "   ⚠️  caffeinate 未运行（每轮任务会自己拉起一次，覆盖 65 分钟）"
fi

echo
echo "4) 当前额度状态"
$PY "$DIR/codex_resume_watch.py" --no-act 2>&1 | sed 's/^/   /'

echo
echo "===== 自检结束 ====="
