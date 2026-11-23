#!/usr/bin/env bash
# Codex 写开发文档；断连自动 resume，额度/余额类错误则停止
cd /workspace/chatbi-poc
COMMON=(--skip-git-repo-check -s workspace-write -c sandbox_workspace_write.network_access=true -c web_search='"live"' -c tools.web_search=true -m gpt-6.2-astra -c model_reasoning_effort='"high"')
FIRST="请阅读 docs/00-任务说明.md 并严格按其要求完成。参考资料在 refs/，数据在 data/。只写文档，不写项目代码。"
RESUME="如果中断过，请从上次进度继续完成 docs/00-任务说明.md 的全部交付，已写好的文件不要重写，只补缺。"
for i in $(seq 1 12); do
  if [ $i -eq 1 ]; then codex exec "${COMMON[@]}" -o design-output.md "$FIRST" > design-$i.log 2>&1
  else codex exec "${COMMON[@]}" -o design-output.md resume --last "$RESUME" > design-$i.log 2>&1; fi
  if [ -s design-output.md ]; then echo "OK on try $i"; exit 0; fi
  if grep -qiE "quota|insufficient|balance|余额|额度|billing|payment required|402" design-$i.log; then echo "QUOTA_STOP on try $i"; exit 2; fi
  echo "try $i interrupted at $(date +%H:%M)"; sleep 90
done
echo "FAILED"; exit 1
