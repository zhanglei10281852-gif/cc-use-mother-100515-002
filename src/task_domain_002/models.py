"""领域模型：成果记录、修订、审核决定与哈希链。

一条成果（RecordState）由一串不可变的修订（Revision）组成，
修订之间通过 ``prev_digest`` 首尾相接形成哈希链。任何已经发生的
修订都不会被修改或删除，授权到期、证据撤回、更正只会追加新修订。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any, Optional

# 变更类型：首次登记 / 更正 / 授权到期 / 证据撤回
CHANGE_KINDS = ("initial", "correction", "license_expired", "evidence_withdrawn")
# 审核决定：采纳 / 驳回
DECISIONS = ("accept", "reject")
# 修订状态：待复核 / 已采纳 / 已驳回
REVISION_STATES = ("pending", "accepted", "rejected")


def canonical_json(obj: Any) -> str:
    """生成结构稳定的 JSON 文本，用于摘要与指纹计算。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def compute_revision_digest(
    record_code: str,
    revision_no: int,
    version: str,
    change_kind: str,
    payload: dict[str, Any],
    prev_digest: str,
) -> str:
    """修订摘要：把修订的身份、内容与前一修订摘要绑定，形成哈希链。"""
    return sha256_hex(
        canonical_json(
            {
                "record_code": record_code,
                "revision_no": revision_no,
                "version": version,
                "change_kind": change_kind,
                "payload": payload,
                "prev_digest": prev_digest,
            }
        )
    )


@dataclass(frozen=True)
class ReviewDecision:
    """一次审核决定。理由为必填，审核员与访客看到的信息范围不同。"""

    decision: str  # accept | reject
    reason: str
    reviewer: str
    decided_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewDecision":
        return cls(
            decision=data["decision"],
            reason=data["reason"],
            reviewer=data["reviewer"],
            decided_at=data["decided_at"],
        )


@dataclass(frozen=True)
class Revision:
    """成果记录的一个不可变修订版本。"""

    record_code: str
    revision_no: int
    version: str
    change_kind: str
    payload: dict[str, Any]
    prev_digest: str  # 首个修订为空串
    digest: str
    submitted_by: str
    submitted_at: str
    state: str = "pending"  # pending | accepted | rejected
    review: Optional[ReviewDecision] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Revision":
        review = data.get("review")
        return cls(
            record_code=data["record_code"],
            revision_no=data["revision_no"],
            version=data["version"],
            change_kind=data["change_kind"],
            payload=data["payload"],
            prev_digest=data["prev_digest"],
            digest=data["digest"],
            submitted_by=data["submitted_by"],
            submitted_at=data["submitted_at"],
            state=data.get("state", "pending"),
            review=ReviewDecision.from_dict(review) if review else None,
        )


@dataclass
class RecordState:
    """一条成果的当前状态：全部修订 + 归并指纹。"""

    record_code: str
    fingerprint: str
    created_at: str
    revisions: list[Revision] = field(default_factory=list)

    @property
    def latest(self) -> Revision:
        return self.revisions[-1]

    @property
    def latest_accepted(self) -> Optional[Revision]:
        """当前对外发布的依据：编号最大的已采纳修订。"""
        for rev in reversed(self.revisions):
            if rev.state == "accepted":
                return rev
        return None

    @property
    def pending_revision(self) -> Optional[Revision]:
        for rev in self.revisions:
            if rev.state == "pending":
                return rev
        return None

    def find_revision(self, revision_no: int) -> Optional[Revision]:
        for rev in self.revisions:
            if rev.revision_no == revision_no:
                return rev
        return None
