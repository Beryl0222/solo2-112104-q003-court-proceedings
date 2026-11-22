#!/usr/bin/env bash
# 端到端演示：从分批录入到争点核阅的完整流程（全部使用合成材料）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
DB="examples/demo.db"
rm -f "$DB"

run() {
  echo
  echo "\$ python3 -m tarchive --db $DB $*"
  python3 -m tarchive --db "$DB" "$@"
}

echo "=== 1. 建案并录入第一批笔录（含缺页、未识别人员、书记员更正） ==="
run case create --no 2026-演-002 --title 仓库租赁纠纷（合成演练）
run ingest --case 2026-演-002 --file data/hearing_batch1.json --note 第一批录入
run gaps --case 2026-演-002

echo
echo "=== 2. 登记人员并建立简称映射（未识别缺口随之关闭） ==="
run person add --case 2026-演-002 --name 周正 --role 审判长
run person add --case 2026-演-002 --name 赵谨 --role 代理人
run person add --case 2026-演-002 --name 孙衡 --role 代理人
run person add --case 2026-演-002 --name 王五 --role 证人
run person add --case 2026-演-002 --name 陈默 --role 当事人
run person add --case 2026-演-002 --name 杜某 --role 值班员
run alias --case 2026-演-002 --label 审判长 --person 周正 --actor 归档员
run alias --case 2026-演-002 --label 原代 --person 赵谨 --actor 归档员
run alias --case 2026-演-002 --label 被代 --person 孙衡 --actor 归档员
run alias --case 2026-演-002 --label 证人甲 --person 王五 --actor 归档员
run alias --case 2026-演-002 --label 被告 --person 陈默 --actor 归档员
run alias --case 2026-演-002 --label 值班员老杜 --person 杜某 --actor 归档员

echo
echo "=== 3. 建立争点并链接发言、关联材料 ==="
run issue create --case 2026-演-002 --title 仓库门是否上锁 \
  --statement 证人转述与代理人主张需并列核对 --actor 归档员
run issue link --id 1 --record h3 --actor 归档员
run issue link --id 1 --record h4 --actor 归档员
run issue attach --id 1 --evidence E-17 --note 目录已收录，质证记录待核
run issue create --case 2026-演-002 --title 证言时间与监控是否矛盾 --actor 归档员
run issue link --id 2 --record h7 --actor 归档员
run issue show --id 1

echo
echo "=== 4. 第二批：补录缺页、更正陈述与发言人（相关引用自动待复核） ==="
run ingest --case 2026-演-002 --file data/hearing_batch2.json --note 第二批：补录与更正
run issue show --id 1
run record --case 2026-演-002 --key h3

echo
echo "=== 5. 复核待复核引用，处理遗留缺口 ==="
run issue recheck --link 1 --actor 复核员 --note 已对照v2，仍为同一发言
run issue recheck --link 2 --actor 复核员 --note 转述内容未变，仅被引述者经更正确认
run issue recheck --link 3 --actor 复核员 --note 发言人经书记员更正为被告
run gap resolve --key 被引述者未识别:值班员小杜 --actor 复核员 \
  --note 书记员已更正为值班员老杜，原简称系误记
run gaps --case 2026-演-002

echo
echo "=== 6. 核阅流程：未完成核阅不能认可 ==="
run review start --issue 1 --reviewer 负责人
run issue approve --id 1 --reviewer 负责人 || echo "（预期内：未完成核阅不能认可）"
run review complete --issue 1 --reviewer 负责人 --note 已逐项对照笔录与材料
run issue approve --id 1 --reviewer 负责人 --note 同意进入团队处理

echo
echo "=== 7. 证据复核结论只能显式录入；提及不等于采信 ==="
run evidence list --case 2026-演-002
run evidence confirm --case 2026-演-002 --no E-17 --reviewer 负责人 \
  --decision 存疑 --note 质证记录尚未补齐
run evidence list --case 2026-演-002

echo
echo "=== 8. 总览与事件留痕 ==="
run overview --case 2026-演-002
run events --case 2026-演-002
