# 庭审笔录中的争点、引述与证据引用整理

本项目整理法律援助团队需要把分段录入的庭审笔录整理成复核清单。书记员更正姓名或引述对象时，原来的争点引用可能仍挂在错误当事人名下；若直接生成结论性摘要，复核人员无法区分证人陈述、律师主张和法庭已经确认的事实。

`tarchive` 提供可重复处理的文本归档能力：复核人员沿着争点找到对应发言与材料，并维护后续核阅状态。项目只使用合成庭审材料，系统不作任何法律结论。

演练资料位于 `data/`：`court.json` 为原始合成记录，`hearing_batch1.json` / `hearing_batch2.json` 为分批录入演练（含缺页、未识别人员、书记员更正与补录）。项目环境使用 Python 3.11 标准库，无第三方依赖。

## 设计原则

- **主张不等于认定**：每条主张都携带发言人、形态（直接陈述/转述/逐字引语）与被引述者；系统没有、也不会生成“法庭认定”类别。
- **提及不等于采信**：证据编号被提及只记为“提及”；程序记录（如“宣布采信”）仅作为程序动作存档；采信结论只能由复核人员用 `evidence confirm` 显式录入。
- **并列保留**：不同当事人对同一事实的陈述按发言人分组并列展示，不做合并、取舍或改写。
- **核阅门控**：争点状态为 草稿 → 核阅中 → 团队认可；只有负责人员完成核阅后才能认可；存在未处理缺口或待复核引用时必须显式确认（`--acknowledge-gaps`），确认内容写入核阅记录。
- **缺口可见**：缺页、未识别发言人、未识别被引述者、来源不明的引语、无法落实的更正，都登记为具体缺口，而不是被摘要掩盖。
- **版本可回溯**：更正产生新版本，旧版本与其上的主张标记“被取代”后保留；争点引用记录整理依据版本，可从更正后的笔录回到先前版本对照。
- **更正联动**：补录或更正姓名、落实书记员更正后，相关主张与争点引用自动置为“待复核”，由复核人员逐项确认后恢复有效。

## 快速开始

```bash
# 运行端到端演示（建案 → 录入 → 身份映射 → 争点 → 更正 → 核阅 → 认可）
examples/demo.sh

# 运行测试
python3 -m unittest discover -s tests -v
```

手工操作示例：

```bash
python3 -m tarchive --db archive.db case create --no 2026-演-001 --title 合成演练案
python3 -m tarchive --db archive.db ingest --case 2026-演-001 --file data/court.json --note 第一批
python3 -m tarchive --db archive.db gaps --case 2026-演-001
python3 -m tarchive --db archive.db person add --case 2026-演-001 --name 周某 --role 证人
python3 -m tarchive --db archive.db alias --case 2026-演-001 --label 周某 --person 周某 --actor 归档员
python3 -m tarchive --db archive.db issue create --case 2026-演-001 --title 文件是否交付
python3 -m tarchive --db archive.db issue link --id 1 --record court-1 --actor 归档员
python3 -m tarchive --db archive.db issue show --id 1
```

## 批次格式

录入文件为 JSON 数组，每个元素为一条记录：

```json
{
  "id": "h4",
  "kind": "transcript",
  "version": 1,
  "page": 2,
  "seg": 2,
  "speaker": "证人甲",
  "text": "证人甲称，听值班员小杜说“仓库的灯整夜亮着”。"
}
```

- `id`（必填）：记录号，争点引用与更正都按记录号定位。
- `kind`（必填）：`transcript` 笔录陈述 / `counsel_statement` 代理人主张 / `procedural` 程序动作 / `clerk_correction` 书记员更正 / `review_note` 核阅记录 / `other` 其他。
- `version`（必填）：正整数。同一 `id` 的更高版本构成更正；同版本内容一致重复录入是幂等跳过；同版本内容不一致或版本回退会整批报错回滚。
- `page` / `seg`（可选）：页码、段号，用于缺页检查与定位。
- `speaker`（可选）：发言人简称；缺省时按规则从句首识别（如“证人周某称”）。
- `text`（必填）：原文。

书记员更正指令（`clerk_correction`）支持两种写法：

```
更正：h4 被引述者为值班员老杜，非值班员小杜。     （按记录号）
更正：第一段被引述者为刘某，非李某。             （本批次内第 N 段笔录）
更正：h7 发言人为被告，非被代。                  （发言人更正）
```

无法解析或无法落实的更正会形成“更正未落实”缺口，绝不静默跳过。

## 提取规则（确定性、保守）

- 发言人：句首“（角色）＋简称＋称/说”，或显式 `speaker` 字段；识别不出即登记缺口。
- 转述：出现“听/据某说（称）”时整句记为转述，并记录被引述者简称。
- 逐字引语：引号“…”内容单独成条；能识别来源则归属被引述者，否则登记“引语来源不明”缺口。
- 证据编号：识别“证据N号”“N号证据”“E-17”形态；只做登记，不作任何采信推断。
- 程序动作：按“出示/采信/休庭/核对/开庭”等关键词归类存档。

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `case create/list` | 建立、列出案件 |
| `person add` / `alias` / `persons` | 登记人员、登记或更正简称映射、查看映射 |
| `ingest --case --file` | 录入批次（幂等，可重复执行） |
| `records` / `record --key` | 列出当前记录 / 查看一条记录的版本历史与提取结果 |
| `issue create/list/show/link/attach/recheck/approve` | 争点建立、链接发言、关联材料、复核引用、认可 |
| `review start/complete/reject` | 核阅流程（负责人开始、完成、退回） |
| `evidence list/confirm` | 查看材料（提及≠采信）、显式录入复核结论 |
| `gaps` / `gap resolve` | 查看缺口、按 id 或缺口键处理缺口 |
| `overview` / `events` | 案件总览、身份与更正事件留痕 |

## 目录结构

```
tarchive/          归档包（纯标准库）
  models.py        常量、数据类、异常
  store.py         SQLite 存储与 schema
  extract.py       规则化提取（发言人/转述/引语/证据编号/更正指令）
  identity.py      人员与简称登记、更正后的重新核对
  ingest.py        批次录入管线（幂等、版本化、更正落实、缺口检测）
  issues.py        争点、引用链接与核阅流程
  gaps.py          缺口登记与关闭
  report.py        面向复核人员的文本报告
  cli.py           命令行入口
data/              合成演练材料
examples/demo.sh   端到端演示
tests/             unittest 测试
```

## 限制

- 提取规则是确定性的关键词/形态匹配，不是语言模型；识别不出的内容一律进入缺口，由人工处理。
- 系统不生成事实认定或法律意见；“团队认可”仅表示团队内部的整理状态。
- 所有演练数据均为合成材料。
