"""HTTP 接口测试：角色隔离、访客可见性、完整链路核对。"""
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from task_domain_002.api import make_server
from task_domain_002.store import EventStore

AUDITOR = {"Authorization": "Bearer dev-auditor-token"}
REPORTER = {"Authorization": "Bearer dev-reporter-token"}


def sample_envelope(**overrides):
    env = {
        "version": "1.0.0",
        "record": {
            "title": "园区能耗优化算法",
            "summary": "面向公共园区的能耗预测与调度优化算法。",
            "source": {
                "org_name": "清源实验室",
                "org_type": "laboratory",
                "contact": "李工 li@example.com",
                "statement": "本单位承诺成果来源合法。",
            },
            "evidence": [
                {
                    "evidence_id": "EV-A",
                    "kind": "benchmark",
                    "uri": "https://example.org/bench/1",
                    "sha256": "b" * 64,
                }
            ],
            "license": {
                "license_type": "CC-BY-4.0",
                "scope": "public-display",
                "valid_from": "2026-01-01",
                "valid_until": "2027-06-30",
            },
            "notes": "内部备注：接口人待确认",
        },
    }
    env.update(overrides)
    return env


class ApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        store = EventStore(cls.tmp.name)
        clock = lambda: datetime(2026, 10, 7, tzinfo=timezone.utc)  # noqa: E731
        cls.server = make_server(store, host="127.0.0.1", port=0, clock=clock)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        conn.request(method, path, body=payload, headers=merged)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data


class AuthAndVisibilityTests(ApiTestBase):
    def test_protected_endpoints_require_token(self):
        status, data = self.request("POST", "/records", sample_envelope())
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "unauthorized")

    def test_reporter_cannot_review(self):
        status, data = self.request(
            "POST", "/records/ACH-X/revisions/1/review",
            {"decision": "accept", "reason": "越权尝试"}, REPORTER,
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "forbidden")

    def test_visitor_cannot_reach_auditor_views(self):
        for path in ("/records", "/reviews/pending", "/audit/decisions"):
            status, _ = self.request("GET", path)
            self.assertEqual(status, 401, path)


class FullFlowTests(ApiTestBase):
    def test_submit_review_publish_chain_over_http(self):
        # 报送方提交（显式指定成果编码）
        status, created = self.request("POST", "/records", sample_envelope(record_code="ACH-PARK-001"), REPORTER)
        self.assertEqual(status, 201)
        self.assertEqual(created["outcome"], "created")
        code = created["record_code"]
        self.assertEqual(code, "ACH-PARK-001")

        # 重复上报（内容一致，未带编码）→ 按指纹归并，不产生第二条记录
        status, dup = self.request("POST", "/records", sample_envelope(), REPORTER)
        self.assertEqual(status, 201)
        self.assertEqual(dup["outcome"], "merged_identical")
        self.assertEqual(dup["record_code"], code)

        # 重复上报（同一编码但内容分歧）→ 409 冲突，附字段级差异
        divergent = sample_envelope(record_code="ACH-PARK-001")
        divergent["record"]["title"] = "被改动的同名成果"
        status, conflict = self.request("POST", "/records", divergent, REPORTER)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "duplicate_conflict")
        self.assertTrue(any(d["path"] == "title" for d in conflict["details"]["diff"]))

        # 发布前访客不可见
        status, _ = self.request("GET", f"/public/records/{code}")
        self.assertEqual(status, 404)

        # 审核员看到待复核事项，无理由不能驳回
        status, pending = self.request("GET", "/reviews/pending", headers=AUDITOR)
        self.assertEqual(status, 200)
        self.assertEqual([item["record_code"] for item in pending["pending"]], [code])
        status, err = self.request(
            "POST", f"/records/{code}/revisions/1/review",
            {"decision": "reject", "reason": " "}, AUDITOR,
        )
        self.assertEqual(status, 400)

        # 采纳并附理由
        status, reviewed = self.request(
            "POST", f"/records/{code}/revisions/1/review",
            {"decision": "accept", "reason": "证据链完整，许可有效"}, AUDITOR,
        )
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["state"], "accepted")

        # 访客只能读到公开字段
        status, public = self.request("GET", f"/public/records/{code}")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(public),
            {"record_code", "version", "title", "summary", "source", "license", "evidence", "published_at"},
        )
        blob = json.dumps(public, ensure_ascii=False)
        for secret in ("li@example.com", "内部备注", "证据链完整", "dev-reporter"):
            self.assertNotIn(secret, blob)

        # 更正 → 新版本 → 再审核 → 发布视图更新，历史版本保留
        status, change = self.request(
            "POST", f"/records/{code}/changes",
            {"change_kind": "correction", "version": "1.0.1", "changes": {"summary": "更正后的简介"}},
            REPORTER,
        )
        self.assertEqual(status, 201)
        self.assertEqual(change["revision_no"], 2)
        status, _ = self.request(
            "POST", f"/records/{code}/revisions/2/review",
            {"decision": "accept", "reason": "更正属实"}, AUDITOR,
        )
        self.assertEqual(status, 200)
        status, public = self.request("GET", f"/public/records/{code}")
        self.assertEqual(public["summary"], "更正后的简介")
        self.assertEqual(public["version"], "1.0.1")

        # 审核员核对完整链路：提交 → 采纳 → 更正 → 采纳
        status, chain = self.request("GET", f"/records/{code}/chain", headers=AUDITOR)
        self.assertEqual(status, 200)
        self.assertTrue(chain["ok"])
        self.assertEqual(chain["revision_count"], 2)
        self.assertEqual(chain["published_revision_no"], 2)
        self.assertEqual(
            [entry["review"]["reason"] for entry in chain["revisions"]],
            ["证据链完整，许可有效", "更正属实"],
        )

        # 决定台账：每次采纳或驳回都有理由
        status, decisions = self.request("GET", f"/audit/decisions?record_code={code}", headers=AUDITOR)
        self.assertEqual(status, 200)
        self.assertEqual(len(decisions["decisions"]), 2)
        self.assertTrue(all(d["reason"] for d in decisions["decisions"]))

        # 已审核的修订不可再改判
        status, err = self.request(
            "POST", f"/records/{code}/revisions/1/review",
            {"decision": "reject", "reason": "试图改判"}, AUDITOR,
        )
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "already_reviewed")

    def test_unknown_route_and_record(self):
        status, _ = self.request("GET", "/no-such-route")
        self.assertEqual(status, 404)
        status, data = self.request("GET", "/records/ACH-NOPE", headers=AUDITOR)
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
