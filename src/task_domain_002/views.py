"""访客可见性策略：只投影公开字段，内部字段永不进入访客视图。

公开字段采用显式白名单，新增字段默认不公开。联系方式、来源声明全文、
内部备注、提交人、审核理由、修订历史等均不对访客开放。
"""
from __future__ import annotations

from typing import Any

from .models import RecordState, Revision

PUBLIC_LICENSE_FIELDS = ("license_type", "scope", "valid_from", "valid_until", "status")
PUBLIC_EVIDENCE_FIELDS = ("evidence_id", "kind", "uri", "sha256", "status")


def public_revision_view(record: RecordState, revision: Revision) -> dict[str, Any]:
    """把一条已采纳修订投影为访客可见的公开视图。"""
    payload = revision.payload
    license_info = payload["license"]
    return {
        "record_code": record.record_code,
        "version": revision.version,
        "title": payload["title"],
        "summary": payload["summary"],
        "source": {
            "org_name": payload["source"]["org_name"],
            "org_type": payload["source"]["org_type"],
        },
        "license": {key: license_info[key] for key in PUBLIC_LICENSE_FIELDS},
        "evidence": [
            {key: entry[key] for key in PUBLIC_EVIDENCE_FIELDS if key in entry}
            for entry in payload["evidence"]
        ],
        "published_at": revision.review.decided_at if revision.review else None,
    }
