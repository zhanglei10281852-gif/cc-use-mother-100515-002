"""成果记录的校验与规范化规则。

所有进入系统的记录（首次登记或变更产生的新版本）都必须通过
``normalize_payload``：字段缺失、类型错误、许可自相矛盾、版本号
无法比较等情况都会被拒绝，并携带机器可读的错误码。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .errors import RegistryError

ORG_TYPES = ("enterprise", "laboratory", "public_institution")
EVIDENCE_STATUSES = ("active", "withdrawn")
LICENSE_STATUSES = ("active", "expired")

RECORD_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

RECORD_FIELDS = {"title", "summary", "source", "evidence", "license", "notes"}
SOURCE_FIELDS = {"org_name", "org_type", "contact", "statement"}
LICENSE_FIELDS = {"license_type", "scope", "valid_from", "valid_until", "status"}
EVIDENCE_FIELDS = {"evidence_id", "kind", "uri", "sha256", "status", "withdrawn_at"}


def parse_version(version: Any) -> tuple[int, ...]:
    """把 ``v2`` / ``1.0`` / ``1.2.3`` 形式的版本号解析为可比较的元组。"""
    if not isinstance(version, str):
        raise RegistryError("invalid_version", "版本号必须是字符串", {"version": version})
    text = version.strip().lower()
    if text.startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if not text or any(not part.isdigit() for part in parts):
        raise RegistryError(
            "invalid_version",
            "版本号格式无效，应为 v1、1.0、1.2.3 这类数字形式",
            {"version": version},
        )
    return tuple(int(part) for part in parts)


def version_gt(new: str, old: str) -> bool:
    """判断 new 是否严格大于 old，长度不一致时按补零对齐。"""
    a, b = parse_version(new), parse_version(old)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


def bump_version(version: str) -> str:
    """系统代拟新版本号：末位加一，保持原有分段数量与前缀风格。"""
    prefix = "v" if version.strip().lower().startswith("v") else ""
    parts = list(parse_version(version))
    parts[-1] += 1
    return prefix + ".".join(str(p) for p in parts)


def parse_date(value: Any, where: str) -> date:
    if not isinstance(value, str):
        raise RegistryError("invalid_payload", f"{where} 必须是 YYYY-MM-DD 字符串", {"value": value})
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise RegistryError("invalid_payload", f"{where} 不是合法的 YYYY-MM-DD 日期", {"value": value}) from None


def _require_str(obj: dict[str, Any], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError("invalid_payload", f"{where}.{key} 缺失或为空", {"field": f"{where}.{key}"})
    return value.strip()


def _reject_unknown(obj: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise RegistryError(
            "invalid_payload",
            f"{where} 包含未登记的字段: {', '.join(unknown)}",
            {"unknown_fields": unknown},
        )


def normalize_evidence(item: Any, index: int) -> dict[str, Any]:
    where = f"evidence[{index}]"
    if not isinstance(item, dict):
        raise RegistryError("invalid_payload", f"{where} 必须是对象")
    _reject_unknown(item, EVIDENCE_FIELDS, where)
    entry = {
        "evidence_id": _require_str(item, "evidence_id", where),
        "kind": _require_str(item, "kind", where),
        "uri": _require_str(item, "uri", where),
        "sha256": _require_str(item, "sha256", where).lower(),
        "status": item.get("status", "active"),
    }
    if not SHA256_RE.match(entry["sha256"]):
        raise RegistryError("invalid_payload", f"{where}.sha256 必须是 64 位十六进制摘要")
    if entry["status"] not in EVIDENCE_STATUSES:
        raise RegistryError("invalid_payload", f"{where}.status 必须是 active 或 withdrawn")
    if entry["status"] == "withdrawn":
        withdrawn_at = item.get("withdrawn_at")
        if not isinstance(withdrawn_at, str) or not withdrawn_at.strip():
            raise RegistryError("invalid_payload", f"{where} 已撤回但缺少 withdrawn_at")
        entry["withdrawn_at"] = withdrawn_at.strip()
    return entry


def normalize_payload(raw: Any, today: date) -> dict[str, Any]:
    """校验并规范化一条成果记录的内容，返回只含登记字段的干净副本。

    ``today`` 由调用方注入，便于测试与重放时保持确定性。
    """
    if not isinstance(raw, dict):
        raise RegistryError("invalid_payload", "记录内容必须是 JSON 对象")
    _reject_unknown(raw, RECORD_FIELDS, "record")

    title = _require_str(raw, "title", "record")
    summary = _require_str(raw, "summary", "record")

    source = raw.get("source")
    if not isinstance(source, dict):
        raise RegistryError("invalid_payload", "record.source 缺失或不是对象")
    _reject_unknown(source, SOURCE_FIELDS, "source")
    clean_source = {
        "org_name": _require_str(source, "org_name", "source"),
        "org_type": _require_str(source, "org_type", "source"),
        "contact": _require_str(source, "contact", "source"),
        "statement": _require_str(source, "statement", "source"),
    }
    if clean_source["org_type"] not in ORG_TYPES:
        raise RegistryError(
            "invalid_payload",
            "source.org_type 必须是 enterprise / laboratory / public_institution 之一",
            {"org_type": clean_source["org_type"]},
        )

    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise RegistryError("invalid_payload", "record.evidence 至少包含一条证据引用")
    clean_evidence = [normalize_evidence(item, i) for i, item in enumerate(evidence)]
    seen_ids = [e["evidence_id"] for e in clean_evidence]
    if len(set(seen_ids)) != len(seen_ids):
        raise RegistryError("invalid_payload", "record.evidence 中存在重复的 evidence_id")

    license_raw = raw.get("license")
    if not isinstance(license_raw, dict):
        raise RegistryError("invalid_payload", "record.license 缺失或不是对象")
    _reject_unknown(license_raw, LICENSE_FIELDS, "license")
    clean_license = {
        "license_type": _require_str(license_raw, "license_type", "license"),
        "scope": _require_str(license_raw, "scope", "license"),
        "valid_from": _require_str(license_raw, "valid_from", "license"),
        "valid_until": _require_str(license_raw, "valid_until", "license"),
        "status": license_raw.get("status", "active"),
    }
    if clean_license["status"] not in LICENSE_STATUSES:
        raise RegistryError("invalid_payload", "license.status 必须是 active 或 expired")
    valid_from = parse_date(clean_license["valid_from"], "license.valid_from")
    valid_until = parse_date(clean_license["valid_until"], "license.valid_until")
    if valid_from > valid_until:
        raise RegistryError("invalid_payload", "license.valid_from 不能晚于 license.valid_until")
    if clean_license["status"] == "active" and valid_until < today:
        raise RegistryError(
            "license_expired",
            "许可有效期已过，不能按有效状态登记；请先登记授权到期变更",
            {"valid_until": clean_license["valid_until"]},
        )
    if clean_license["status"] == "expired" and valid_until >= today:
        raise RegistryError(
            "invalid_payload",
            "许可状态为 expired 但有效期尚未截止，状态与日期矛盾",
            {"valid_until": clean_license["valid_until"]},
        )

    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise RegistryError("invalid_payload", "record.notes 必须是字符串")

    return {
        "title": title,
        "summary": summary,
        "source": clean_source,
        "evidence": clean_evidence,
        "license": clean_license,
        "notes": notes,
    }


def normalize_record_code(code: Any) -> str:
    if not isinstance(code, str) or not RECORD_CODE_RE.match(code.strip()):
        raise RegistryError(
            "invalid_payload",
            "record_code 必须是 3-64 位字母数字开头的标识（可含 . _ -）",
            {"record_code": code},
        )
    return code.strip()


def norm_text(text: str) -> str:
    """归并指纹用的文本规范化：压缩空白并忽略大小写。"""
    return " ".join(text.split()).casefold()
