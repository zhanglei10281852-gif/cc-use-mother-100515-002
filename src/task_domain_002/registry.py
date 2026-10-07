"""登记核心：提交归并、变更、审核、链路核对与到期巡查。

规则要点：
- 同一成果（按归并指纹或显式 record_code 判定）重复上报时，内容一致则
  幂等归并，内容分歧则报冲突，绝不产生第二条有效记录。
- 授权到期、证据撤回、更正只能通过 submit_change 生成新修订；历史修订
  （尤其是已采纳、已对外发布的依据）永不被修改或删除。
- 每条成果同一时刻至多一条待复核修订；变更始终基于当前已采纳版本。
- 采纳与驳回都必须填写理由，提交人不能审核自己提交的修订。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .errors import RegistryError
from .models import (
    CHANGE_KINDS,
    DECISIONS,
    RecordState,
    Revision,
    ReviewDecision,
    canonical_json,
    compute_revision_digest,
    sha256_hex,
)
from .store import EVENT_REVISION_REVIEWED, EVENT_REVISION_SUBMITTED, EventStore
from .validate import (
    bump_version,
    norm_text,
    normalize_evidence,
    normalize_payload,
    normalize_record_code,
    parse_date,
    parse_version,
    version_gt,
)

CORRECTION_FIELDS = {"title", "summary", "source", "license", "notes", "evidence_add"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def fingerprint_of(payload: dict[str, Any]) -> str:
    """成果归并指纹：同一机构名下的同名成果视为同一成果。"""
    identity = {
        "org_name": norm_text(payload["source"]["org_name"]),
        "title": norm_text(payload["title"]),
    }
    return sha256_hex(canonical_json(identity))


def _flatten(obj: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            _flatten(value, f"{prefix}{key}.", out)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _flatten(value, f"{prefix}{index}.", out)
    else:
        out[prefix[:-1]] = obj


def diff_payloads(registered: dict[str, Any], reported: dict[str, Any]) -> list[dict[str, Any]]:
    """字段级差异，用于重复上报冲突时向报送方说明哪里不一致。"""
    flat_old: dict[str, Any] = {}
    flat_new: dict[str, Any] = {}
    _flatten(registered, "", flat_old)
    _flatten(reported, "", flat_new)
    diffs = []
    for path in sorted(set(flat_old) | set(flat_new)):
        old, new = flat_old.get(path, "<缺失>"), flat_new.get(path, "<缺失>")
        if old != new:
            diffs.append({"path": path, "registered": old, "reported": new})
    return diffs[:50]


class Registry:
    """成果登记簿。构造时重放事件日志，恢复全部状态（含待复核队列）。"""

    def __init__(self, store: EventStore, clock: Optional[Callable[[], datetime]] = None):
        self.store = store
        self.clock = clock or _utcnow
        self.records: dict[str, RecordState] = {}
        self.fingerprint_index: dict[str, str] = {}
        for event in store.read_all():
            self._apply(event)

    # ------------------------------------------------------------------
    # 事件重放
    # ------------------------------------------------------------------
    def _apply(self, event: dict[str, Any]) -> None:
        etype, data = event.get("type"), event.get("data", {})
        if etype == EVENT_REVISION_SUBMITTED:
            revision = Revision.from_dict(data["revision"])
            if revision.revision_no == 1:
                record = RecordState(
                    record_code=revision.record_code,
                    fingerprint=data["fingerprint"],
                    created_at=revision.submitted_at,
                )
                self.records[revision.record_code] = record
                self.fingerprint_index[data["fingerprint"]] = revision.record_code
            self.records[revision.record_code].revisions.append(revision)
        elif etype == EVENT_REVISION_REVIEWED:
            record = self.records[data["record_code"]]
            index = data["revision_no"] - 1
            revision = record.revisions[index]
            record.revisions[index] = replace(
                revision,
                state="accepted" if data["decision"] == "accept" else "rejected",
                review=ReviewDecision(
                    decision=data["decision"],
                    reason=data["reason"],
                    reviewer=data["reviewer"],
                    decided_at=data["decided_at"],
                ),
            )
        else:
            raise RegistryError("store_corrupt", f"未知事件类型: {etype}")

    def _append(self, event_type: str, data: dict[str, Any]) -> None:
        self._apply(self.store.append(event_type, data))

    # ------------------------------------------------------------------
    # 提交与归并
    # ------------------------------------------------------------------
    def submit_record(self, envelope: dict[str, Any], submitted_by: str) -> dict[str, Any]:
        """首次登记一条成果；重复上报时归并到既有记录。

        envelope: {"record_code"?: str, "version": str, "record": {...}}
        """
        if not isinstance(envelope, dict):
            raise RegistryError("invalid_payload", "提交内容必须是 JSON 对象")
        now = self.clock()
        payload = normalize_payload(envelope.get("record"), now.date())
        version = envelope.get("version")
        parse_version(version)  # 仅校验格式
        submitted_by = self._require_actor(submitted_by, "submitted_by")

        fingerprint = fingerprint_of(payload)
        code = envelope.get("record_code")
        code = normalize_record_code(code) if code else f"ACH-{fingerprint[:12]}"

        existing = self.records.get(code)
        if existing is None and fingerprint in self.fingerprint_index:
            code = self.fingerprint_index[fingerprint]
            existing = self.records[code]
        if existing is not None:
            if canonical_json(existing.latest.payload) == canonical_json(payload):
                return {
                    "outcome": "merged_identical",
                    "record_code": code,
                    "revision_no": existing.latest.revision_no,
                    "digest": existing.latest.digest,
                    "message": "内容与已登记记录一致，已归并，未产生新记录",
                }
            raise RegistryError(
                "duplicate_conflict",
                "同一成果已登记但内容不一致；请通过变更接口提交更正，不能另立新记录",
                {
                    "record_code": code,
                    "diff": diff_payloads(existing.latest.payload, payload),
                },
            )

        revision = self._new_revision(code, 1, version, "initial", payload, "", submitted_by, now)
        self._append(
            EVENT_REVISION_SUBMITTED,
            {"revision": revision.to_dict(), "fingerprint": fingerprint},
        )
        return {
            "outcome": "created",
            "record_code": code,
            "revision_no": 1,
            "digest": revision.digest,
            "message": "已受理，等待审核",
        }

    # ------------------------------------------------------------------
    # 变更：更正 / 授权到期 / 证据撤回
    # ------------------------------------------------------------------
    def submit_change(
        self,
        record_code: str,
        change_kind: str,
        version: Any,
        changes: Optional[dict[str, Any]],
        submitted_by: str,
    ) -> dict[str, Any]:
        record = self._require_record(record_code)
        if change_kind not in CHANGE_KINDS or change_kind == "initial":
            raise RegistryError(
                "invalid_change",
                f"change_kind 必须是 correction / license_expired / evidence_withdrawn 之一",
                {"change_kind": change_kind},
            )
        basis = record.latest_accepted
        if basis is None:
            raise RegistryError(
                "no_published_basis",
                "该成果尚无已采纳版本，首次登记审核通过前不能提交变更",
            )
        pending = record.pending_revision
        if pending is not None:
            raise RegistryError(
                "pending_revision_exists",
                "该成果已有待复核修订，须先完成审核再提交新变更",
                {"pending_revision_no": pending.revision_no},
            )
        if not version_gt(version, record.latest.version):
            raise RegistryError(
                "version_not_monotonic",
                "新版本号必须大于当前最新版本号",
                {"current": record.latest.version, "reported": version},
            )
        submitted_by = self._require_actor(submitted_by, "submitted_by")

        now = self.clock()
        base = deepcopy(basis.payload)
        if change_kind == "correction":
            updated = self._apply_correction(base, changes)
        elif change_kind == "evidence_withdrawn":
            updated = self._apply_evidence_withdrawal(base, changes, now.date())
        else:  # license_expired
            updated = self._apply_license_expiry(base, now.date())

        payload = normalize_payload(updated, now.date())
        if canonical_json(payload) == canonical_json(basis.payload):
            raise RegistryError("no_effective_change", "变更未产生任何实际内容差异")

        revision = self._new_revision(
            record.record_code,
            len(record.revisions) + 1,
            version,
            change_kind,
            payload,
            record.latest.digest,
            submitted_by,
            now,
        )
        self._append(
            EVENT_REVISION_SUBMITTED,
            {"revision": revision.to_dict(), "fingerprint": record.fingerprint},
        )
        return {
            "outcome": "created",
            "record_code": record.record_code,
            "revision_no": revision.revision_no,
            "digest": revision.digest,
            "message": "变更已受理，等待审核",
        }

    def _apply_correction(self, base: dict[str, Any], changes: Any) -> dict[str, Any]:
        if not isinstance(changes, dict) or not changes:
            raise RegistryError("invalid_change", "更正必须提供非空的 changes 对象")
        unknown = sorted(set(changes) - CORRECTION_FIELDS)
        if unknown:
            raise RegistryError(
                "invalid_change",
                f"更正包含不支持的字段: {', '.join(unknown)}",
                {"unknown_fields": unknown},
            )
        for key in ("title", "summary", "notes"):
            if key in changes:
                base[key] = changes[key]
        for key in ("source", "license"):
            if key in changes:
                if not isinstance(changes[key], dict):
                    raise RegistryError("invalid_change", f"changes.{key} 必须是对象")
                base[key].update(changes[key])
        if "evidence_add" in changes:
            additions = changes["evidence_add"]
            if not isinstance(additions, list) or not additions:
                raise RegistryError("invalid_change", "changes.evidence_add 必须是非空列表")
            existing_ids = {e["evidence_id"] for e in base["evidence"]}
            for i, item in enumerate(additions):
                entry = normalize_evidence(item, i)
                if entry["evidence_id"] in existing_ids:
                    raise RegistryError(
                        "invalid_change",
                        f"证据 {entry['evidence_id']} 已存在，不能重复添加",
                    )
                base["evidence"].append(entry)
                existing_ids.add(entry["evidence_id"])
        return base

    def _apply_evidence_withdrawal(self, base: dict[str, Any], changes: Any, today) -> dict[str, Any]:
        evidence_id = changes.get("evidence_id") if isinstance(changes, dict) else None
        if not evidence_id:
            raise RegistryError("invalid_change", "证据撤回必须提供 changes.evidence_id")
        for entry in base["evidence"]:
            if entry["evidence_id"] == evidence_id:
                if entry["status"] == "withdrawn":
                    raise RegistryError("invalid_change", f"证据 {evidence_id} 此前已撤回")
                entry["status"] = "withdrawn"
                entry["withdrawn_at"] = today.isoformat()
                return base
        raise RegistryError("invalid_change", f"证据 {evidence_id} 在该成果中不存在")

    def _apply_license_expiry(self, base: dict[str, Any], today) -> dict[str, Any]:
        license_info = base["license"]
        if license_info["status"] != "active":
            raise RegistryError("invalid_change", "许可已处于到期状态，无需重复登记")
        if parse_date(license_info["valid_until"], "license.valid_until") >= today:
            raise RegistryError(
                "invalid_change",
                "许可有效期尚未截止，不能登记授权到期",
                {"valid_until": license_info["valid_until"]},
            )
        license_info["status"] = "expired"
        return base

    # ------------------------------------------------------------------
    # 审核
    # ------------------------------------------------------------------
    def review(
        self,
        record_code: str,
        revision_no: int,
        decision: str,
        reason: str,
        reviewer: str,
    ) -> dict[str, Any]:
        record = self._require_record(record_code)
        revision = record.find_revision(revision_no)
        if revision is None:
            raise RegistryError("not_found", f"修订 {record_code}#{revision_no} 不存在")
        if revision.state != "pending":
            raise RegistryError(
                "already_reviewed",
                "该修订已完成审核，审核决定不可更改",
                {"state": revision.state},
            )
        if decision not in DECISIONS:
            raise RegistryError("invalid_payload", "decision 必须是 accept 或 reject")
        reason = (reason or "").strip()
        if not reason:
            raise RegistryError("invalid_payload", "采纳或驳回都必须填写理由")
        reviewer = self._require_actor(reviewer, "reviewer")
        if reviewer == revision.submitted_by:
            raise RegistryError(
                "self_review_forbidden",
                "提交人不能审核自己提交的修订，须由他人复核",
            )

        decided_at = _iso(self.clock())
        self._append(
            EVENT_REVISION_REVIEWED,
            {
                "record_code": record.record_code,
                "revision_no": revision_no,
                "decision": decision,
                "reason": reason,
                "reviewer": reviewer,
                "decided_at": decided_at,
            },
        )
        updated = record.find_revision(revision_no)
        return {
            "record_code": record.record_code,
            "revision_no": revision_no,
            "state": updated.state,
            "review": updated.review.to_dict(),
        }

    # ------------------------------------------------------------------
    # 巡查：授权到期自动生成新修订
    # ------------------------------------------------------------------
    def sweep_expired(self, submitted_by: str = "system") -> list[dict[str, Any]]:
        """找出许可已过期但尚未登记到期的成果，自动发起 license_expired 修订。"""
        today = self.clock().date()
        created = []
        for record in sorted(self.records.values(), key=lambda r: r.record_code):
            basis = record.latest_accepted
            if basis is None or record.pending_revision is not None:
                continue
            license_info = basis.payload["license"]
            if license_info["status"] == "active" and parse_date(
                license_info["valid_until"], "license.valid_until"
            ) < today:
                created.append(
                    self.submit_change(
                        record.record_code,
                        "license_expired",
                        bump_version(record.latest.version),
                        {},
                        submitted_by,
                    )
                )
        return created

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def pending_revisions(self) -> list[dict[str, Any]]:
        """待复核队列：重启后由事件日志重放恢复。"""
        items = []
        for record in self.records.values():
            rev = record.pending_revision
            if rev is not None:
                items.append(self._revision_summary(record, rev))
        items.sort(key=lambda item: (item["submitted_at"], item["record_code"]))
        return items

    def decisions(self, record_code: Optional[str] = None) -> list[dict[str, Any]]:
        """审核决定台账：每次采纳或驳回及其理由，供审核员查阅。"""
        items = []
        for record in self.records.values():
            if record_code and record.record_code != record_code:
                continue
            for rev in record.revisions:
                if rev.review is not None:
                    items.append(
                        {
                            "record_code": record.record_code,
                            "revision_no": rev.revision_no,
                            "version": rev.version,
                            "change_kind": rev.change_kind,
                            **rev.review.to_dict(),
                        }
                    )
        items.sort(key=lambda item: (item["decided_at"], item["record_code"], item["revision_no"]))
        return items

    def list_records(self) -> list[dict[str, Any]]:
        return [self._record_summary(record) for record in sorted(
            self.records.values(), key=lambda r: r.record_code
        )]

    def record_detail(self, record_code: str) -> dict[str, Any]:
        """审核员视角的完整记录：全部修订、理由与内部字段。"""
        record = self._require_record(record_code)
        detail = self._record_summary(record)
        detail["fingerprint"] = record.fingerprint
        detail["revisions"] = [self._revision_summary(record, rev, full=True) for rev in record.revisions]
        return detail

    def public_view(self, record_code: str) -> Optional[dict[str, Any]]:
        """访客视图：仅当前已采纳版本的公开字段，无已采纳版本时返回 None。"""
        from .views import public_revision_view

        record = self.records.get(record_code)
        if record is None:
            return None
        basis = record.latest_accepted
        if basis is None:
            return None
        return public_revision_view(record, basis)

    def public_list(self) -> list[dict[str, Any]]:
        views = []
        for record in sorted(self.records.values(), key=lambda r: r.record_code):
            view = self.public_view(record.record_code)
            if view is not None:
                views.append(view)
        return views

    # ------------------------------------------------------------------
    # 链路核对
    # ------------------------------------------------------------------
    def chain_report(self, record_code: str) -> dict[str, Any]:
        """核对一条成果从提交到发布的完整链路。

        逐项检查：修订编号连续、哈希链衔接、摘要可重算、每次采纳或驳回
        都有理由、已发布依据仍然完整存在。任何一项不满足都会反映到 ok。
        """
        record = self._require_record(record_code)
        entries = []
        ok = True
        prev_digest = ""
        for expected_no, rev in enumerate(record.revisions, 1):
            checks = {
                "sequence_ok": rev.revision_no == expected_no,
                "link_ok": rev.prev_digest == prev_digest,
                "digest_ok": rev.digest
                == compute_revision_digest(
                    rev.record_code,
                    rev.revision_no,
                    rev.version,
                    rev.change_kind,
                    rev.payload,
                    rev.prev_digest,
                ),
                "review_ok": (
                    (rev.state == "pending" and rev.review is None)
                    or (
                        rev.state in ("accepted", "rejected")
                        and rev.review is not None
                        and bool(rev.review.reason.strip())
                    )
                ),
            }
            if not all(checks.values()):
                ok = False
            entries.append(
                {
                    "revision_no": rev.revision_no,
                    "version": rev.version,
                    "change_kind": rev.change_kind,
                    "state": rev.state,
                    "digest": rev.digest,
                    "submitted_by": rev.submitted_by,
                    "submitted_at": rev.submitted_at,
                    "review": rev.review.to_dict() if rev.review else None,
                    "checks": checks,
                }
            )
            prev_digest = rev.digest

        basis = record.latest_accepted
        accepted = [rev for rev in record.revisions if rev.state == "accepted"]
        return {
            "record_code": record.record_code,
            "ok": ok,
            "revision_count": len(record.revisions),
            "accepted_count": len(accepted),
            "pending_count": 1 if record.pending_revision else 0,
            "published_revision_no": basis.revision_no if basis else None,
            "published_version": basis.version if basis else None,
            "revisions": entries,
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _new_revision(
        self,
        code: str,
        revision_no: int,
        version: Any,
        change_kind: str,
        payload: dict[str, Any],
        prev_digest: str,
        submitted_by: str,
        now: datetime,
    ) -> Revision:
        digest = compute_revision_digest(code, revision_no, version, change_kind, payload, prev_digest)
        return Revision(
            record_code=code,
            revision_no=revision_no,
            version=version,
            change_kind=change_kind,
            payload=payload,
            prev_digest=prev_digest,
            digest=digest,
            submitted_by=submitted_by,
            submitted_at=_iso(now),
        )

    def _require_record(self, record_code: str) -> RecordState:
        record = self.records.get(record_code)
        if record is None:
            raise RegistryError("not_found", f"成果 {record_code} 不存在", {"record_code": record_code})
        return record

    @staticmethod
    def _require_actor(name: Any, field: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise RegistryError("invalid_payload", f"{field} 缺失或为空")
        return name.strip()

    def _record_summary(self, record: RecordState) -> dict[str, Any]:
        basis = record.latest_accepted
        pending = record.pending_revision
        return {
            "record_code": record.record_code,
            "title": record.latest.payload["title"],
            "org_name": record.latest.payload["source"]["org_name"],
            "revision_count": len(record.revisions),
            "latest_version": record.latest.version,
            "published_revision_no": basis.revision_no if basis else None,
            "published_version": basis.version if basis else None,
            "pending_revision_no": pending.revision_no if pending else None,
            "created_at": record.created_at,
        }

    def _revision_summary(self, record: RecordState, rev: Revision, full: bool = False) -> dict[str, Any]:
        summary = {
            "record_code": record.record_code,
            "revision_no": rev.revision_no,
            "version": rev.version,
            "change_kind": rev.change_kind,
            "state": rev.state,
            "digest": rev.digest,
            "submitted_by": rev.submitted_by,
            "submitted_at": rev.submitted_at,
            "review": rev.review.to_dict() if rev.review else None,
        }
        if full:
            summary["prev_digest"] = rev.prev_digest
            summary["payload"] = rev.payload
        return summary
