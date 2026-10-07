"""登记核心规则测试：归并、修订链、审核理由、访客可见性、重启恢复。"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from task_domain_002.errors import RegistryError
from task_domain_002.registry import Registry
from task_domain_002.store import EventStore

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def fixed_clock(moment=NOW):
    return lambda: moment


def sample_record(**overrides):
    record = {
        "title": "城市内涝智能预警模型",
        "summary": "基于多源传感数据的内涝预警模型，已在三个城区试点。",
        "source": {
            "org_name": "云图数据科技有限公司",
            "org_type": "enterprise",
            "contact": "张工 zhang@example.com",
            "statement": "本单位声明该成果为自主研发，权属清晰。",
        },
        "evidence": [
            {
                "evidence_id": "EV-001",
                "kind": "test_report",
                "uri": "https://example.org/reports/EV-001",
                "sha256": "a" * 64,
            }
        ],
        "license": {
            "license_type": "CC-BY-4.0",
            "scope": "public-display",
            "valid_from": "2026-01-01",
            "valid_until": "2027-12-31",
        },
        "notes": "内部备注：待补充试点城市清单",
    }
    record.update(overrides)
    return record


def envelope(**overrides):
    env = {"version": "1.0.0", "record": sample_record()}
    env.update(overrides)
    return env


class RegistryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EventStore(self.tmp.name)
        self.registry = Registry(self.store, clock=fixed_clock())

    def reopen(self, moment=NOW):
        """模拟系统重启：用同一存储重新构建登记簿。"""
        return Registry(self.store, clock=fixed_clock(moment))

    def submit_and_accept(self, reg=None, env=None, reviewer="auditor-1"):
        reg = reg or self.registry
        result = reg.submit_record(env or envelope(), "reporter-1")
        reg.review(result["record_code"], 1, "accept", "材料齐全，予以采纳", reviewer)
        return result["record_code"]

    def assert_error(self, code, fn, *args, **kwargs):
        with self.assertRaises(RegistryError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception


class SubmitAndDedupTests(RegistryTestBase):
    def test_submit_creates_pending_revision(self):
        result = self.registry.submit_record(envelope(), "reporter-1")
        self.assertEqual(result["outcome"], "created")
        pending = self.registry.pending_revisions()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["record_code"], result["record_code"])
        self.assertEqual(pending[0]["state"], "pending")

    def test_identical_resubmission_is_merged(self):
        first = self.registry.submit_record(envelope(), "reporter-1")
        second = self.registry.submit_record(envelope(), "reporter-2")
        self.assertEqual(second["outcome"], "merged_identical")
        self.assertEqual(second["record_code"], first["record_code"])
        record = self.registry.records[first["record_code"]]
        self.assertEqual(len(record.revisions), 1)  # 没有制造第二条有效记录

    def test_same_fingerprint_merges_even_with_different_claimed_code(self):
        first = self.registry.submit_record(envelope(), "reporter-1")
        env = envelope(record_code="ACH-OTHER-999")
        second = self.registry.submit_record(env, "reporter-2")
        self.assertEqual(second["outcome"], "merged_identical")
        self.assertEqual(second["record_code"], first["record_code"])
        self.assertEqual(len(self.registry.records), 1)

    def test_divergent_resubmission_reports_field_conflicts(self):
        # 同一 record_code 再次上报但内容分歧 → 冲突，不另立记录
        self.registry.submit_record(envelope(record_code="ACH-PARK-001"), "reporter-1")
        changed = sample_record(title="同名成果但内容被改动")
        exc = self.assert_error(
            "duplicate_conflict",
            self.registry.submit_record,
            envelope(record_code="ACH-PARK-001", record=changed),
            "reporter-2",
        )
        paths = [d["path"] for d in exc.details["diff"]]
        self.assertIn("title", paths)
        self.assertEqual(len(self.registry.records), 1)  # 冲突不会另立记录

    def test_same_fingerprint_divergent_content_conflicts(self):
        # 未提供 record_code 时按（机构+标题）指纹归并；内容分歧同样报冲突
        first = self.registry.submit_record(envelope(), "reporter-1")
        changed = sample_record(summary="简介被改动，但机构与标题相同")
        exc = self.assert_error(
            "duplicate_conflict", self.registry.submit_record, envelope(record=changed), "reporter-2"
        )
        self.assertEqual(exc.details["record_code"], first["record_code"])
        self.assertEqual(len(self.registry.records), 1)

    def test_invalid_payloads_are_rejected(self):
        self.assert_error(
            "invalid_payload",
            self.registry.submit_record,
            envelope(record=sample_record(source={"org_name": "x"})),
            "reporter-1",
        )
        bad_license = sample_record()
        bad_license["license"]["valid_from"] = "2027-01-01"
        bad_license["license"]["valid_until"] = "2026-01-01"
        self.assert_error(
            "invalid_payload", self.registry.submit_record, envelope(record=bad_license), "reporter-1"
        )

    def test_expired_license_cannot_be_registered_as_active(self):
        record = sample_record()
        record["license"]["valid_until"] = "2026-01-01"  # 早于固定时钟 2026-10-07
        self.assert_error(
            "license_expired", self.registry.submit_record, envelope(record=record), "reporter-1"
        )

    def test_unknown_fields_are_rejected(self):
        record = sample_record(extra_field="不该出现")
        self.assert_error(
            "invalid_payload", self.registry.submit_record, envelope(record=record), "reporter-1"
        )


class ReviewTests(RegistryTestBase):
    def test_accept_publishes_and_records_reason(self):
        code = self.submit_and_accept()
        decisions = self.registry.decisions(code)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "accept")
        self.assertEqual(decisions[0]["reason"], "材料齐全，予以采纳")
        self.assertEqual(decisions[0]["reviewer"], "auditor-1")

    def test_reason_is_mandatory_for_accept_and_reject(self):
        code = self.registry.submit_record(envelope(), "reporter-1")["record_code"]
        self.assert_error("invalid_payload", self.registry.review, code, 1, "accept", "  ", "auditor-1")
        self.assert_error("invalid_payload", self.registry.review, code, 1, "reject", "", "auditor-1")

    def test_reject_keeps_record_unpublished_but_traceable(self):
        code = self.registry.submit_record(envelope(), "reporter-1")["record_code"]
        self.registry.review(code, 1, "reject", "证据摘要与原文不符", "auditor-1")
        self.assertIsNone(self.registry.public_view(code))  # 未发布
        detail = self.registry.record_detail(code)
        self.assertEqual(detail["revisions"][0]["state"], "rejected")
        self.assertEqual(detail["revisions"][0]["review"]["reason"], "证据摘要与原文不符")

    def test_self_review_is_forbidden(self):
        code = self.registry.submit_record(envelope(), "reporter-1")["record_code"]
        self.assert_error(
            "self_review_forbidden", self.registry.review, code, 1, "accept", "理由", "reporter-1"
        )

    def test_decision_is_final(self):
        code = self.submit_and_accept()
        self.assert_error(
            "already_reviewed", self.registry.review, code, 1, "reject", "想改判", "auditor-2"
        )


class ChangeAndVersionChainTests(RegistryTestBase):
    def test_correction_creates_new_version_and_keeps_published_basis(self):
        code = self.submit_and_accept()
        result = self.registry.submit_change(
            code, "correction", "1.0.1", {"summary": "更正后的成果简介"}, "reporter-1"
        )
        self.assertEqual(result["revision_no"], 2)
        # 待审期间，对外发布的依据仍是第 1 版
        self.assertEqual(self.registry.public_view(code)["summary"], "基于多源传感数据的内涝预警模型，已在三个城区试点。")
        self.registry.review(code, 2, "accept", "更正内容属实", "auditor-1")
        self.assertEqual(self.registry.public_view(code)["summary"], "更正后的成果简介")
        # 历史版本没有被抹掉
        detail = self.registry.record_detail(code)
        self.assertEqual(len(detail["revisions"]), 2)
        self.assertEqual(detail["revisions"][0]["state"], "accepted")
        self.assertEqual(
            detail["revisions"][0]["payload"]["summary"], "基于多源传感数据的内涝预警模型，已在三个城区试点。"
        )

    def test_change_requires_published_basis(self):
        code = self.registry.submit_record(envelope(), "reporter-1")["record_code"]
        self.assert_error(
            "no_published_basis",
            self.registry.submit_change,
            code, "correction", "1.0.1", {"summary": "x"}, "reporter-1",
        )

    def test_only_one_pending_revision_per_record(self):
        code = self.submit_and_accept()
        self.registry.submit_change(code, "correction", "1.0.1", {"summary": "甲"}, "reporter-1")
        self.assert_error(
            "pending_revision_exists",
            self.registry.submit_change,
            code, "correction", "1.0.2", {"summary": "乙"}, "reporter-1",
        )

    def test_version_must_increase(self):
        code = self.submit_and_accept()
        self.assert_error(
            "version_not_monotonic",
            self.registry.submit_change,
            code, "correction", "1.0.0", {"summary": "x"}, "reporter-1",
        )
        self.assert_error(
            "version_not_monotonic",
            self.registry.submit_change,
            code, "correction", "0.9", {"summary": "x"}, "reporter-1",
        )

    def test_rejected_change_does_not_touch_published_view(self):
        code = self.submit_and_accept()
        self.registry.submit_change(code, "correction", "1.0.1", {"title": "被驳回的标题"}, "reporter-1")
        self.registry.review(code, 2, "reject", "更正依据不足", "auditor-1")
        self.assertEqual(self.registry.public_view(code)["title"], "城市内涝智能预警模型")
        self.assertEqual(self.registry.decisions(code)[-1]["reason"], "更正依据不足")

    def test_correction_can_renew_license(self):
        code = self.submit_and_accept()
        self.registry.submit_change(
            code,
            "correction",
            "2.0",
            {"license": {"valid_until": "2028-12-31", "status": "active"}},
            "reporter-1",
        )
        self.registry.review(code, 2, "accept", "续期材料已核验", "auditor-1")
        self.assertEqual(self.registry.public_view(code)["license"]["valid_until"], "2028-12-31")


class LicenseExpiryTests(RegistryTestBase):
    def test_license_expiry_generates_new_version_instead_of_erasing(self):
        # 提交时许可仍有效（时钟固定在 2026-09-01）
        early = Registry(self.store, clock=fixed_clock(datetime(2026, 9, 1, tzinfo=timezone.utc)))
        record = sample_record()
        record["license"]["valid_until"] = "2026-09-30"
        result = early.submit_record(envelope(record=record), "reporter-1")
        code = result["record_code"]
        early.review(code, 1, "accept", "材料齐全", "auditor-1")

        # 时间推进到 2026-10-07，许可已过期：巡查自动登记到期修订
        late = self.reopen()
        created = late.sweep_expired()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["revision_no"], 2)
        # 审核前对外依据仍是有效的第 1 版
        self.assertEqual(late.public_view(code)["license"]["status"], "active")
        late.review(code, 2, "accept", "到期事实已核实", "auditor-1")
        self.assertEqual(late.public_view(code)["license"]["status"], "expired")
        # 已发布依据未被抹掉：第 1 版仍是 accepted，链路完整
        report = late.chain_report(code)
        self.assertTrue(report["ok"])
        self.assertEqual(report["accepted_count"], 2)

    def test_manual_expiry_before_deadline_is_rejected(self):
        code = self.submit_and_accept()
        self.assert_error(
            "invalid_change",
            self.registry.submit_change,
            code, "license_expired", "1.0.1", {}, "reporter-1",
        )

    def test_sweep_skips_records_with_pending_revision(self):
        early = Registry(self.store, clock=fixed_clock(datetime(2026, 9, 1, tzinfo=timezone.utc)))
        record = sample_record()
        record["license"]["valid_until"] = "2026-09-30"
        code = early.submit_record(envelope(record=record), "reporter-1")["record_code"]
        early.review(code, 1, "accept", "材料齐全", "auditor-1")
        early.submit_change(code, "correction", "1.0.1", {"summary": "先更正"}, "reporter-1")
        late = self.reopen()
        self.assertEqual(late.sweep_expired(), [])  # 有待审修订时不叠加到期修订


class EvidenceWithdrawalTests(RegistryTestBase):
    def test_withdrawal_creates_new_version_and_keeps_evidence_in_history(self):
        code = self.submit_and_accept()
        self.registry.submit_change(
            code, "evidence_withdrawn", "1.0.1", {"evidence_id": "EV-001"}, "reporter-1"
        )
        self.registry.review(code, 2, "accept", "证据提供方已确认撤回", "auditor-1")
        public = self.registry.public_view(code)
        self.assertEqual(public["evidence"][0]["status"], "withdrawn")
        # 历史修订中证据仍是 active：撤回不等于抹掉
        detail = self.registry.record_detail(code)
        self.assertEqual(detail["revisions"][0]["payload"]["evidence"][0]["status"], "active")
        self.assertEqual(detail["revisions"][1]["payload"]["evidence"][0]["withdrawn_at"], "2026-10-07")

    def test_withdrawing_unknown_evidence_is_rejected(self):
        code = self.submit_and_accept()
        self.assert_error(
            "invalid_change",
            self.registry.submit_change,
            code, "evidence_withdrawn", "1.0.1", {"evidence_id": "EV-404"}, "reporter-1",
        )


class VisibilityTests(RegistryTestBase):
    def test_visitor_sees_only_public_fields(self):
        code = self.submit_and_accept()
        view = self.registry.public_view(code)
        self.assertEqual(
            set(view),
            {"record_code", "version", "title", "summary", "source", "license", "evidence", "published_at"},
        )
        self.assertEqual(set(view["source"]), {"org_name", "org_type"})
        blob = json.dumps(view, ensure_ascii=False)
        for secret in ("zhang@example.com", "内部备注", "auditor-1", "材料齐全", "reporter-1", "digest"):
            self.assertNotIn(secret, blob)

    def test_visitor_cannot_see_unpublished_record(self):
        code = self.registry.submit_record(envelope(), "reporter-1")["record_code"]
        self.assertIsNone(self.registry.public_view(code))
        self.assertEqual(self.registry.public_list(), [])


class PersistenceAndChainTests(RegistryTestBase):
    def test_pending_items_survive_restart(self):
        code = self.registry.submit_record(envelope(), "reporter-1")["record_code"]
        reopened = self.reopen()
        pending = reopened.pending_revisions()
        self.assertEqual([item["record_code"] for item in pending], [code])
        reopened.review(code, 1, "accept", "重启后复核通过", "auditor-1")
        self.assertEqual(self.reopen().public_view(code)["record_code"], code)

    def test_chain_report_verifies_full_lifecycle(self):
        code = self.submit_and_accept()
        self.registry.submit_change(code, "correction", "1.0.1", {"summary": "链路测试"}, "reporter-1")
        self.registry.review(code, 2, "reject", "理由充分性不足", "auditor-1")
        report = self.reopen().chain_report(code)
        self.assertTrue(report["ok"])
        self.assertEqual(report["revision_count"], 2)
        self.assertEqual(report["published_revision_no"], 1)
        states = [entry["state"] for entry in report["revisions"]]
        self.assertEqual(states, ["accepted", "rejected"])
        reasons = [entry["review"]["reason"] for entry in report["revisions"]]
        self.assertEqual(reasons, ["材料齐全，予以采纳", "理由充分性不足"])

    def test_chain_report_detects_tampered_history(self):
        code = self.submit_and_accept()
        events_path = self.store.events_path
        text = events_path.read_text(encoding="utf-8")
        events_path.write_text(text.replace("城市内涝智能预警模型", "被篡改的标题", 1), encoding="utf-8")
        report = self.reopen().chain_report(code)
        self.assertFalse(report["ok"])
        self.assertFalse(report["revisions"][0]["checks"]["digest_ok"])

    def test_chain_report_unknown_record(self):
        self.assert_error("not_found", self.registry.chain_report, "ACH-NOPE")


if __name__ == "__main__":
    unittest.main()
