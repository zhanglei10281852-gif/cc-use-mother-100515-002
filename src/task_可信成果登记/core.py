# 兼容层：正式实现位于 task_domain_002 包，这里仅做转发，避免两处实现漂移。
from task_domain_002.core import Record, detect_conflicts, stable_summary

__all__ = ["Record", "detect_conflicts", "stable_summary"]
