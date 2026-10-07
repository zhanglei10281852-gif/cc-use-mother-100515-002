"""命令行测试：提交、审核、链路核对与退出码。"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from task_domain_002.cli import main


def envelope_file(tmp, **overrides):
    env = {
        "version": "1.0.0",
        "record": {
            "title": "基层数据治理工具箱",
            "summary": "面向街道社区的数据治理流程工具集。",
            "source": {
                "org_name": "民生公共数据服务中心",
                "org_type": "public_institution",
                "contact": "王工 wang@example.com",
                "statement": "本单位承诺成果来源合法。",
            },
            "evidence": [
                {
                    "evidence_id": "EV-C",
                    "kind": "deployment_report",
                    "uri": "https://example.org/deploy/1",
                    "sha256": "c" * 64,
                }
            ],
            "license": {
                "license_type": "CC0-1.0",
                "scope": "public-display",
                "valid_from": "2026-01-01",
                "valid_until": "2028-01-01",
            },
        },
    }
    env.update(overrides)
    import os

    path = os.path.join(tmp, "record.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(env, handle, ensure_ascii=False)
    return path


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = self.tmp.name

    def cli(self, *argv):
        return run_cli(["--data-dir", self.data_dir, *argv])

    def test_full_lifecycle_via_cli(self):
        path = envelope_file(self.tmp.name)

        code, out, _ = self.cli("submit", path, "--submitted-by", "reporter-a")
        self.assertEqual(code, 0)
        created = json.loads(out)
        self.assertEqual(created["outcome"], "created")
        record_code = created["record_code"]

        # 重复提交 → 归并
        code, out, _ = self.cli("submit", path, "--submitted-by", "reporter-b")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["outcome"], "merged_identical")

        # 待复核队列在“重启”（新进程视角）后仍在
        code, out, _ = self.cli("pending")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["pending"][0]["record_code"], record_code)

        # 无理由审核被拒绝
        code, _, err = self.cli(
            "review", record_code, "1", "--decision", "accept", "--reason", " ",
            "--reviewer", "auditor-a",
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid_payload", err)

        code, out, _ = self.cli(
            "review", record_code, "1", "--decision", "accept",
            "--reason", "材料齐全", "--reviewer", "auditor-a",
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state"], "accepted")

        # 访客视图不含内部字段
        code, out, _ = self.cli("public", record_code)
        self.assertEqual(code, 0)
        public = json.loads(out)
        self.assertEqual(set(public["source"]), {"org_name", "org_type"})
        self.assertNotIn("wang@example.com", out)

        # 更正产生新版本
        code, out, _ = self.cli(
            "change", record_code, "--kind", "correction", "--version", "1.0.1",
            "--changes", '{"summary": "更正后的简介"}', "--submitted-by", "reporter-a",
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["revision_no"], 2)
        self.cli(
            "review", record_code, "2", "--decision", "accept",
            "--reason", "更正属实", "--reviewer", "auditor-a",
        )

        # 链路核对：退出码 0 且每步都有审核理由
        code, out, _ = self.cli("chain", record_code)
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report["ok"])
        self.assertEqual(report["revision_count"], 2)
        self.assertEqual(
            [r["review"]["reason"] for r in report["revisions"]],
            ["材料齐全", "更正属实"],
        )

        # 决定台账
        code, out, _ = self.cli("decisions", record_code)
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["decisions"]), 2)

    def test_chain_exit_code_reflects_integrity(self):
        path = envelope_file(self.tmp.name)
        self.cli("submit", path, "--submitted-by", "reporter-a")
        _, out, _ = self.cli("list")
        record_code = json.loads(out)["records"][0]["record_code"]
        self.cli(
            "review", record_code, "1", "--decision", "accept",
            "--reason", "材料齐全", "--reviewer", "auditor-a",
        )
        # 篡改事件日志后链路核对必须失败且退出码为 1
        import os

        events_path = os.path.join(self.data_dir, "events.jsonl")
        with open(events_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        with open(events_path, "w", encoding="utf-8") as handle:
            handle.write(text.replace("基层数据治理工具箱", "被篡改", 1))
        code, out, _ = self.cli("chain", record_code)
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_business_error_exit_code(self):
        code, _, err = self.cli("show", "ACH-NOPE")
        self.assertEqual(code, 2)
        self.assertIn("not_found", err)


if __name__ == "__main__":
    unittest.main()
