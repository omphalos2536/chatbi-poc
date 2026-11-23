#!/usr/bin/env bash
# dsh 卡住（会话文件超过 25 分钟没更新）就结束它，让流水线自动重试
while pgrep -f pipeline/run.sh >/dev/null; do
  for p in $(pgrep -f "node .*dsh headless"); do
    age=$(( $(date +%s) - $(stat -c %Y /proc/$p) ))
    newest=$(find ~/.dsh/sessions/--workspace-chatbi-poc-- -name 'session.v4.jsonl.zstd' -printf '%T@\n' | sort -n | tail -1 | cut -d. -f1)
    idle=$(( $(date +%s) - ${newest:-0} ))
    if [ $age -gt 1500 ] && [ $idle -gt 1500 ]; then
      echo "- $(date '+%m-%d %H:%M') ⚠️ dsh 已 $((idle/60)) 分钟无进展，结束后自动重试" >> /workspace/chatbi-poc/pipeline/STATUS.md
      kill $p
    fi
  done
  sleep 120
done
