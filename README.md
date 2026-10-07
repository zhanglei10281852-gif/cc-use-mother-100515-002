# 数智成果可信登记

面向大会秘书处的成果可信登记后端：接收来自企业、实验室和公共机构的成果记录
（来源声明、证据引用、许可条件、版本号），按规则校验冲突并建立可追溯的修订链。
纯 Python 3.11 标准库实现，运行时不依赖任何外部服务。

## 核心规则

- **重复上报必归并**：同一成果按显式 `record_code` 或（机构名 + 标题）指纹判定。
  内容一致 → 幂等归并，不产生新记录；内容分歧 → 返回 `duplicate_conflict` 及
  字段级差异，绝不制造第二条有效记录。
- **历史不可抹除**：授权到期、证据撤回、更正只能通过变更接口生成**新版本**；
  已采纳、已对外发布的依据永久保留，修订之间以 `prev_digest` 哈希链衔接。
- **先审后发**：每条成果同一时刻至多一条待复核修订；变更始终基于当前已采纳版本；
  版本号必须单调递增。
- **审核留痕**：采纳或驳回都必须填写理由；提交人不能审核自己提交的修订；
  审核决定一经作出不可更改。
- **访客最小可见**：访客只能读取当前已发布版本的公开字段（白名单投影），
  联系方式、来源声明全文、内部备注、提交人、审核理由一律不对外。
- **重启可恢复**：全部事实写入追加式事件日志 `events.jsonl`，重启后重放即可
  恢复包括待复核队列在内的完整状态。

## 目录结构

```
src/task_domain_002/
  core.py       # 起步能力：不可变记录、稳定摘要、冲突检测（保留兼容）
  models.py     # 修订、审核决定、哈希链
  validate.py   # 记录校验与规范化（来源/证据/许可/版本号）
  store.py      # 追加式事件存储（events.jsonl + 文件锁）
  registry.py   # 登记核心：归并、变更、审核、巡查、链路核对
  views.py      # 访客公开字段白名单投影
  api.py        # 标准库 HTTP 接口（角色令牌）
  cli.py        # 命令行
tests/          # 41 个单元/接口/命令行测试
run_cli.py      # 命令行入口
```

## 运行测试与编译检查

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run_cli.py
```

## 命令行

```bash
# 提交登记（信封：{record_code?, version, record}）
python3 run_cli.py submit record.json --submitted-by reporter-zhang

# 待复核队列（重启后仍在）
python3 run_cli.py pending

# 审核：采纳或驳回都必须给理由
python3 run_cli.py review ACH-DEMO-001 1 --decision accept --reason "证据链完整" --reviewer auditor-wang

# 变更：更正 / 授权到期 / 证据撤回（只生成新版本）
python3 run_cli.py change ACH-DEMO-001 --kind correction --version 1.0.1 \
    --changes '{"summary": "更正后的简介"}'
python3 run_cli.py change ACH-DEMO-001 --kind evidence_withdrawn --version 1.0.2 \
    --changes '{"evidence_id": "EV-001"}'

# 巡查：许可已过期的成果自动登记 license_expired 修订
python3 run_cli.py sweep

# 核对一条成果从提交到发布的完整链路（链路被破坏时退出码为 1）
python3 run_cli.py chain ACH-DEMO-001

# 访客视角 / 审核员视角 / 决定台账
python3 run_cli.py public ACH-DEMO-001
python3 run_cli.py show ACH-DEMO-001
python3 run_cli.py decisions ACH-DEMO-001

# 启动 HTTP 服务
python3 run_cli.py serve --port 8080
```

数据目录默认为 `.registry_data`，可用 `--data-dir` 或环境变量
`REGISTRY_DATA_DIR` 指定。命令行与服务端共用同一事件存储。

## HTTP 接口

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/records` | reporter | 提交登记；重复上报自动归并或返回 409 冲突 |
| POST | `/records/{code}/changes` | reporter | 提交变更（correction / license_expired / evidence_withdrawn） |
| POST | `/records/{code}/revisions/{n}/review` | auditor | 审核（decision + reason 必填） |
| GET | `/records` · `/records/{code}` | auditor | 成果列表 / 完整修订史 |
| GET | `/records/{code}/chain` | auditor | 核对提交到发布的完整链路 |
| GET | `/reviews/pending` | auditor | 待复核队列 |
| GET | `/audit/decisions?record_code=` | auditor | 采纳/驳回理由台账 |
| POST | `/maintenance/sweep` | auditor | 授权到期巡查 |
| GET | `/public/records` · `/public/records/{code}` | 访客 | 仅公开字段 |
| GET | `/health` | 任何人 | 健康检查 |

认证：`Authorization: Bearer <令牌>`。令牌用环境变量 `REGISTRY_TOKENS`
配置，格式 `姓名:角色:令牌;...`（角色为 `auditor`/`reporter`，逗号分隔）。
未配置时使用仅供本地开发的默认令牌：`dev-auditor-token`（审核+报送）、
`dev-reporter-token`（报送）。访客端点无需令牌。

错误响应统一为 `{"error": 机器码, "message": 说明, "details": ...}`，
HTTP 状态码按机器码映射（如 `duplicate_conflict`→409、`not_found`→404、
`self_review_forbidden`→403）。

## 记录格式

```json
{
  "record_code": "ACH-DEMO-001",
  "version": "1.0.0",
  "record": {
    "title": "城市内涝智能预警模型",
    "summary": "基于多源传感数据的内涝预警模型。",
    "source": {
      "org_name": "云图数据科技有限公司",
      "org_type": "enterprise",
      "contact": "张工 zhang@example.com",
      "statement": "本单位声明成果为自主研发。"
    },
    "evidence": [
      {"evidence_id": "EV-001", "kind": "test_report",
       "uri": "https://example.org/r/1", "sha256": "<64位十六进制>"}
    ],
    "license": {"license_type": "CC-BY-4.0", "scope": "public-display",
                "valid_from": "2026-01-01", "valid_until": "2027-12-31"},
    "notes": "内部备注（不对外公开）"
  }
}
```

- `org_type`：`enterprise` / `laboratory` / `public_institution`
- 版本号：`v1`、`1.0`、`1.2.3` 等数字分段形式，变更时必须单调递增
- 许可状态与有效期必须一致：有效期内不能登记为 `expired`，已过期不能按
  `active` 登记（须先登记授权到期变更）
- 证据撤回后仍保留在历史修订中（状态置为 `withdrawn` 并记录撤回日期），
  不会从已发布依据中抹掉

## 设计说明

- **事件溯源**：`revision_submitted` / `revision_reviewed` 两类事件只增不改；
  状态由重放导出，因此待复核队列天然可恢复，篡改历史会导致 `chain` 核对失败。
- **性能说明**：为保证 CLI 与服务进程视图一致，每次操作在文件锁内重放事件日志，
  适合秘书处登记量级；若事件量增长，可在锁内增加快照缓存而不改变语义。
