from __future__ import annotations
from dataclasses import dataclass, asdict
from hashlib import sha256
import json

@dataclass(frozen=True, slots=True)
class Record:
    code: str
    version: str
    state: str
    owner: str
    payload: tuple[tuple[str, str], ...] = ()

    def canonical(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return sha256(self.canonical().encode("utf-8")).hexdigest()

def detect_conflicts(records: list[Record]) -> list[tuple[str, str]]:
    seen: dict[str, Record] = {}
    conflicts: list[tuple[str, str]] = []
    for record in records:
        old = seen.get(record.code)
        if old is not None and old.digest() != record.digest():
            conflicts.append((record.code, old.version + "->" + record.version))
        seen[record.code] = record
    return conflicts

def stable_summary(record: Record) -> dict[str, str]:
    return {"code": record.code, "version": record.version, "state": record.state, "digest": record.digest()}
