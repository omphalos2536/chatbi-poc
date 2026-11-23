#!/usr/bin/env bash
# 自动接力：dsh 写代码 → Codex 5.6-sol 验收 → 不过则 6.2-astra 写修复文档 → dsh 修 → 复验；通过即推 GitHub
set -u
ROOT=/workspace/chatbi-poc; cd $ROOT
LOG=$ROOT/pipeline/logs; mkdir -p $LOG
ST=$ROOT/pipeline/STATUS.md
export DSH_TELEMETRY_DISABLED=1
export CHATBI_LLM_API_KEY="$(awk '/CC_SWITCH_DSH_111111:/{print $2}' ~/.dsh/.credentials.yaml)"
MAXFIX=3
RECON=0
note(){ echo "- $(date '+%m-%d %H:%M') $*" >> $ST; echo "$*"; }
is_quota(){ grep -qiE "insufficient_quota|quota exceeded|exceeded your current quota|insufficient balance|余额不足|额度不足|额度已用完" "$1"; }
CX=(codex exec --skip-git-repo-check -s workspace-write -c sandbox_workspace_write.network_access=true -c web_search='"live"' -c tools.web_search=true)

# codex 调用：$1=模型 $2=推理 $3=输出文件 $4=日志前缀 $5=首轮prompt；断连则 resume --last
codex_run(){
  local model=$1 eff=$2 out=$3 lp=$4 prompt=$5 i
  rm -f "$out"
  for i in $(seq 1 10); do
    if [ $i -eq 1 ]; then timeout 2h "${CX[@]}" -m $model -c model_reasoning_effort="\"$eff\"" -o "$out" "$prompt" > $lp-$i.log 2>&1
    else timeout 2h "${CX[@]}" -m $model -c model_reasoning_effort="\"$eff\"" -o "$out" resume --last "如果中断过，请从上次进度继续完成原任务，已完成的部分不要重做。" > $lp-$i.log 2>&1; fi
    [ -s "$out" ] && return 0
    if is_quota $lp-$i.log; then note "❌ Codex($model) 额度/余额不足，停止（日志 $lp-$i.log）"; return 2; fi
    RECON=$((RECON+1)); sleep 90
  done
  note "❌ Codex($model) 连续 10 次中断，停止"; return 1
}
# dsh 调用
dsh_run(){
  local prompt=$1 lp=$2 i
  for i in $(seq 1 6); do
    timeout 4h dsh headless --patch ~/.dsh/profiles/web/cordis.patch.yml --patch ~/agent-tools/dsh-default-model.yml --approval-policy never - <<<"$prompt" > $lp-$i.log 2> $lp-$i.err
    local rc=$?
    [ $rc -eq 0 ] && [ -s $lp-$i.log ] && return 0
    if is_quota $lp-$i.err || is_quota $lp-$i.log; then note "❌ dsh 额度/余额不足，停止（日志 $lp-$i.err）"; return 2; fi
    RECON=$((RECON+1)); sleep 60
  done
  note "❌ dsh 连续 6 次失败，停止"; return 1
}
push(){ git add -A && git commit -qm "$1" && git push -q origin main 2>>$LOG/git.err || note "⚠️ 推送失败：$1"; }

# 0. 等设计完成
while pgrep -f run-design.sh >/dev/null; do sleep 30; done
[ -s design-output.md ] || { note "❌ 设计阶段未完成"; exit 1; }
echo "# 流水线进度" > $ST
note "✅ Codex 开发文档完成"
push "docs: Codex(6.2-astra) 分步开发文档 v1"

for f in $(ls docs/steps/step-*.md | sort); do
  s=$(basename $f .md); fixdoc=""
  for att in $(seq 0 $MAXFIX); do
    note "▶ $s 第 $((att+1)) 轮：dsh 开发"
    P="你在 $ROOT 项目里工作。先读 docs/01-总体设计.md，然后严格按 $f 完成本步全部任务和交付物。${fixdoc:+本轮是返工：先读验收问题清单 reviews/$s-attempt-$att.md 和修复说明 $fixdoc，按修复说明修改。}如果你支持 /goal 或多 agent 并行，请开启，把可并行的子任务分给子 agent。不要修改 docs/ 下的文档。refs/答案基准与清洗规则.md 只有评测步骤可以读，其他步骤的代码、提示词、术语库、示例 SQL 不得读取或包含其中的答案。大模型密钥在环境变量 CHATBI_LLM_API_KEY，不得写入任何文件或日志。完成后自己逐条运行本步验收条件，全部通过再结束，最后用中文简述完成情况。"
    dsh_run "$P" $LOG/$s-dsh-$att || exit $?
    note "▶ $s 第 $((att+1)) 轮：Sol 验收"
    R=reviews/$s-attempt-$((att+1)).md
    V="你是验收员。在 $ROOT 项目中，按 docs/验收规范.md 对 $f 的验收条件逐条执行（真实运行命令，不要只看代码）。不得修改 src/、scripts/、tests/ 等任何代码或文档；只允许写验收报告和 var/evidence 下的证据。把验收报告写入 $R：第一行必须是 'VERDICT: PASS' 或 'VERDICT: FAIL'，之后逐条列出每个验收条件的执行命令、实际结果、是否通过；不通过时给出问题清单（现象、复现命令、期望）。最终回复同样以 VERDICT 行开头。"
    codex_run gpt-5.6-sol medium $LOG/$s-verify-$att.out $LOG/$s-verify-$att "$V" || exit $?
    [ -s $R ] || cp $LOG/$s-verify-$att.out $R
    if head -3 $R | grep -q "VERDICT: PASS"; then
      note "✅ $s 验收通过（第 $((att+1)) 轮）"; push "$s: 验收通过（第 $((att+1)) 轮，dsh 开发 / 5.6-sol 验收）"; break
    fi
    if [ $att -eq $MAXFIX ]; then note "⛔ $s 返工 $MAXFIX 次仍未通过，停止，等人工决定"; push "$s: 未通过（已返工 $MAXFIX 次），流水线暂停"; exit 3; fi
    note "✖ $s 未通过，Astra 写修复说明"
    fixdoc=docs/fixes/$s-fix-$((att+1)).md
    F="你是架构师，只写文档不写代码。阅读 $f、验收报告 $R 以及当前相关代码，分析未通过的根因，写一份给编码 agent（dsh，能力中等）执行的修复说明到 $fixdoc：根因、逐条具体修改步骤（文件路径、要改什么）、修改后需重跑的验收命令。不要修改任何代码。"
    codex_run gpt-6.2-astra high $LOG/$s-fix-$att.out $LOG/$s-fix-$att "$F" || exit $?
    [ -s $fixdoc ] || cp $LOG/$s-fix-$att.out $fixdoc
    # 返工时 reviews 文件名按轮次对应
    cp $R reviews/$s-attempt-$((att+1)).md 2>/dev/null
  done
done
note "🎉 全部步骤完成；断连/重试共 $RECON 次"
push "pipeline: 全部步骤完成"
