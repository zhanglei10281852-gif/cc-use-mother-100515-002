"""命令行入口：与服务端共用同一事件存储，可直接核对完整链路。

示例：
    python3 run_cli.py submit record.json
    python3 run_cli.py review ACH-xxx 1 --decision accept --reason "材料齐全"
    python3 run_cli.py chain ACH-xxx        # 核对从提交到发布的完整链路
    python3 run_cli.py serve --port 8080
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

from .api import serve as serve_http
from .errors import RegistryError
from .registry import Registry
from .store import EventStore


def _load_json_arg(value: str) -> Any:
    """支持内联 JSON、@文件路径 或既有文件路径三种形式。"""
    if value.startswith("@"):
        with open(value[1:], "r", encoding="utf-8") as handle:
            return json.load(handle)
    if os.path.exists(value):
        with open(value, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(value)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_cli.py",
        description="数智成果可信登记：登记、审核、巡查与链路核对",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("REGISTRY_DATA_DIR", ".registry_data"),
        help="事件存储目录（默认 .registry_data，可用 REGISTRY_DATA_DIR 覆盖）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="启动 HTTP 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)

    p = sub.add_parser("submit", help="提交成果登记（重复上报自动归并）")
    p.add_argument("file", help="登记信封 JSON 文件：{version, record, record_code?}")
    p.add_argument("--submitted-by", default="cli-operator")

    p = sub.add_parser("change", help="提交变更：更正 / 授权到期 / 证据撤回（只生成新版本）")
    p.add_argument("record_code")
    p.add_argument("--kind", required=True, choices=["correction", "license_expired", "evidence_withdrawn"])
    p.add_argument("--version", required=True, help="新版本号，必须大于当前最新版本")
    p.add_argument("--changes", default="{}", help="变更内容 JSON，或 @文件 / 文件路径")
    p.add_argument("--submitted-by", default="cli-operator")

    p = sub.add_parser("review", help="审核一条待复核修订（采纳或驳回都必须给理由）")
    p.add_argument("record_code")
    p.add_argument("revision_no", type=int)
    p.add_argument("--decision", required=True, choices=["accept", "reject"])
    p.add_argument("--reason", required=True)
    p.add_argument("--reviewer", default="cli-auditor")

    sub.add_parser("pending", help="列出待复核事项")

    sub.add_parser("list", help="列出全部成果概要")

    p = sub.add_parser("show", help="审核员视角查看成果全部修订与理由")
    p.add_argument("record_code")

    p = sub.add_parser("public", help="访客视角查看成果公开字段")
    p.add_argument("record_code")

    p = sub.add_parser("chain", help="核对一条成果从提交到发布的完整链路")
    p.add_argument("record_code")

    p = sub.add_parser("decisions", help="查看采纳 / 驳回决定及理由台账")
    p.add_argument("record_code", nargs="?")

    p = sub.add_parser("sweep", help="巡查授权到期的成果并自动登记到期修订")
    p.add_argument("--submitted-by", default="system")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "serve":
        serve_http(args.data_dir, host=args.host, port=args.port, tokens_env=os.environ.get("REGISTRY_TOKENS"))
        return 0

    store = EventStore(args.data_dir)
    try:
        with store.locked():
            registry = Registry(store)
            if args.command == "submit":
                _print(registry.submit_record(_load_json_arg(args.file), args.submitted_by))
            elif args.command == "change":
                _print(
                    registry.submit_change(
                        args.record_code,
                        args.kind,
                        args.version,
                        _load_json_arg(args.changes),
                        args.submitted_by,
                    )
                )
            elif args.command == "review":
                _print(
                    registry.review(
                        args.record_code, args.revision_no, args.decision, args.reason, args.reviewer
                    )
                )
            elif args.command == "pending":
                _print({"pending": registry.pending_revisions()})
            elif args.command == "list":
                _print({"records": registry.list_records()})
            elif args.command == "show":
                _print(registry.record_detail(args.record_code))
            elif args.command == "public":
                view = registry.public_view(args.record_code)
                if view is None:
                    _print({"error": "not_found", "message": "成果不存在或尚未发布"})
                    return 1
                _print(view)
            elif args.command == "chain":
                report = registry.chain_report(args.record_code)
                _print(report)
                return 0 if report["ok"] else 1
            elif args.command == "decisions":
                _print({"decisions": registry.decisions(args.record_code)})
            elif args.command == "sweep":
                _print({"created": registry.sweep_expired(args.submitted_by)})
    except RegistryError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(json.dumps({"error": "file_not_found", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
