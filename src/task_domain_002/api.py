"""基于标准库的 HTTP 接口。

角色与令牌：
- 访客（无令牌）：只能访问 /public/* 与 /health，且只返回公开字段；
- 报送方（reporter）：提交登记与变更；
- 审核员（auditor）：审核、查看完整链路、决定台账与待复核队列。

令牌通过环境变量 REGISTRY_TOKENS 配置，格式：
    "姓名:角色:令牌;姓名2:角色1,角色2:令牌2"
未配置时使用仅供本地开发的默认令牌（见 DEFAULT_TOKENS）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .errors import RegistryError
from .registry import Registry
from .store import EventStore

DEFAULT_TOKENS = {
    "dev-auditor-token": {"name": "dev-auditor", "roles": {"auditor", "reporter"}},
    "dev-reporter-token": {"name": "dev-reporter", "roles": {"reporter"}},
}

ERROR_STATUS = {
    "invalid_payload": 400,
    "invalid_version": 400,
    "invalid_change": 400,
    "license_expired": 400,
    "no_effective_change": 400,
    "not_found": 404,
    "duplicate_conflict": 409,
    "pending_revision_exists": 409,
    "version_not_monotonic": 409,
    "already_reviewed": 409,
    "no_published_basis": 409,
    "self_review_forbidden": 403,
}


def load_tokens(env_value: Optional[str] = None) -> dict[str, dict[str, Any]]:
    if not env_value:
        return {token: dict(info, roles=set(info["roles"])) for token, info in DEFAULT_TOKENS.items()}
    tokens: dict[str, dict[str, Any]] = {}
    for part in env_value.split(";"):
        part = part.strip()
        if not part:
            continue
        name, roles, token = part.split(":", 2)
        tokens[token.strip()] = {
            "name": name.strip(),
            "roles": {role.strip() for role in roles.split(",") if role.strip()},
        }
    return tokens


def make_server(
    store: EventStore,
    host: str = "127.0.0.1",
    port: int = 8080,
    tokens: Optional[dict[str, dict[str, Any]]] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> ThreadingHTTPServer:
    """构建 HTTP 服务。每个请求都在存储锁内重放事件后处理，保证与 CLI 视图一致。"""
    token_map = tokens if tokens is not None else load_tokens()

    class Handler(BaseHTTPRequestHandler):
        server_version = "TrustedRegistry/0.1"

        # ---------------- 基础工具 ----------------
        def log_message(self, fmt: str, *args: Any) -> None:  # 保持安静，审计以事件日志为准
            pass

        def _send(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RegistryError("invalid_payload", "请求体不是合法的 JSON") from None

        def _identity(self) -> Optional[dict[str, Any]]:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None
            return token_map.get(auth[len("Bearer "):].strip())

        def _require_role(self, role: str) -> dict[str, Any]:
            identity = self._identity()
            if identity is None:
                self._send({"error": "unauthorized", "message": "缺少有效的访问令牌"}, 401)
                return None
            if role not in identity["roles"]:
                self._send(
                    {"error": "forbidden", "message": f"该操作需要 {role} 角色"},
                    403,
                )
                return None
            return identity

        # ---------------- 路由 ----------------
        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                with store.locked():
                    registry = Registry(store, clock=clock)
                    self._route(registry, method, path, query)
            except RegistryError as exc:
                self._send(exc.to_dict(), ERROR_STATUS.get(exc.code, 400))
            except BrokenPipeError:
                pass
            except Exception as exc:  # 兜底，避免泄露堆栈给调用方
                self._send({"error": "internal", "message": f"服务内部错误: {exc}"}, 500)

        def _route(self, reg: Registry, method: str, path: str, query: dict) -> None:
            if method == "GET" and path == "/health":
                self._send({"ok": True, "records": len(reg.records)})
                return

            # ---- 访客端点：无需令牌，只返回公开字段 ----
            if method == "GET" and path == "/public/records":
                self._send({"records": reg.public_list()})
                return
            match = re.fullmatch(r"/public/records/([^/]+)", path)
            if method == "GET" and match:
                view = reg.public_view(match.group(1))
                if view is None:
                    self._send({"error": "not_found", "message": "成果不存在或尚未发布"}, 404)
                else:
                    self._send(view)
                return

            # ---- 报送方端点 ----
            if method == "POST" and path == "/records":
                identity = self._require_role("reporter")
                if identity:
                    self._send(reg.submit_record(self._body(), identity["name"]), 201)
                return
            match = re.fullmatch(r"/records/([^/]+)/changes", path)
            if method == "POST" and match:
                identity = self._require_role("reporter")
                if identity:
                    body = self._body()
                    self._send(
                        reg.submit_change(
                            match.group(1),
                            body.get("change_kind"),
                            body.get("version"),
                            body.get("changes") or {},
                            identity["name"],
                        ),
                        201,
                    )
                return

            # ---- 审核员端点 ----
            if method == "GET" and path == "/records":
                if self._require_role("auditor"):
                    self._send({"records": reg.list_records()})
                return
            match = re.fullmatch(r"/records/([^/]+)", path)
            if method == "GET" and match:
                if self._require_role("auditor"):
                    self._send(reg.record_detail(match.group(1)))
                return
            match = re.fullmatch(r"/records/([^/]+)/chain", path)
            if method == "GET" and match:
                if self._require_role("auditor"):
                    self._send(reg.chain_report(match.group(1)))
                return
            match = re.fullmatch(r"/records/([^/]+)/revisions/(\d+)/review", path)
            if method == "POST" and match:
                identity = self._require_role("auditor")
                if identity:
                    body = self._body()
                    self._send(
                        reg.review(
                            match.group(1),
                            int(match.group(2)),
                            body.get("decision"),
                            body.get("reason"),
                            identity["name"],
                        )
                    )
                return
            if method == "GET" and path == "/reviews/pending":
                if self._require_role("auditor"):
                    self._send({"pending": reg.pending_revisions()})
                return
            if method == "GET" and path == "/audit/decisions":
                if self._require_role("auditor"):
                    code = query.get("record_code", [None])[0]
                    self._send({"decisions": reg.decisions(code)})
                return
            if method == "POST" and path == "/maintenance/sweep":
                identity = self._require_role("auditor")
                if identity:
                    self._send({"created": reg.sweep_expired(identity["name"])}, 201)
                return

            self._send({"error": "not_found", "message": "接口不存在"}, 404)

    server = ThreadingHTTPServer((host, port), Handler)
    server.registry_store = store  # type: ignore[attr-defined]
    return server


def serve(
    data_dir: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    tokens_env: Optional[str] = None,
) -> None:
    store = EventStore(data_dir)
    server = make_server(store, host=host, port=port, tokens=load_tokens(tokens_env))
    actual_host, actual_port = server.server_address[:2]
    print(f"可信登记服务已启动: http://{actual_host}:{actual_port} (数据目录: {data_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
