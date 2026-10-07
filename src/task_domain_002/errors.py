"""登记系统的业务错误类型。

每个错误都带一个机器可读的 ``code``，HTTP 层据此映射状态码，
CLI 据此决定退出码，调用方不应依赖错误文案做判断。
"""
from __future__ import annotations

from typing import Any


class RegistryError(Exception):
    """登记规则被违反时抛出的业务错误。"""

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}
