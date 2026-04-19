"""Scout 主动推送 QQ 测试脚本

目标：不走 MCP / 不走 plugin tool，直接通过 HTTP 把一条测试消息送到用户 QQ。

两条路径，顺序尝试：
  路径 A — OpenClaw Gateway channel API (loopback)
    POST http://127.0.0.1:18789/api/channels/qqbot/send
    Header: Authorization: Bearer <gateway_token>
    Body:   {"user_openid": "...", "content": "...", "msg_type": 0}

  路径 B — 直接 QQ 开放平台 API (fallback)
    Step 1: POST https://bots.qq.com/app/getAppAccessToken
            Body: {"appId": "...", "clientSecret": "..."}
            → {"access_token": "...", "expires_in": "..."}
    Step 2: POST https://api.sgroup.qq.com/v2/users/{openid}/messages
            Header: Authorization: QQBot <access_token>
            Body:   {"content": "...", "msg_type": 0}

运行:  python scripts\test_qq_push.py

用户 QQ 收到 "[Scout测试] 主动推送测试, 请忽略" = 成功。
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---- .env 加载（python-dotenv 未装时 fallback 到 shell env） ----
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# ---- Windows console UTF-8 ----
if sys.platform.startswith("win"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---- 配置（从 .env / shell env 读取；不再硬编码） ----
USER_OPENID = os.getenv("QQ_USER_OPENID", "")
TEST_CONTENT = "[Scout测试] 主动推送测试, 请忽略"

GATEWAY_URL = "http://127.0.0.1:18789"
GATEWAY_TOKEN = os.getenv("QQ_GATEWAY_TOKEN", "")

QQ_APP_ID = os.getenv("QQ_BOT_APP_ID", "")
QQ_CLIENT_SECRET = os.getenv("QQ_BOT_SECRET", "")
QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_API_BASE = "https://api.sgroup.qq.com"

if not (USER_OPENID and QQ_APP_ID and QQ_CLIENT_SECRET):
    print("❌ 缺少环境变量 QQ_USER_OPENID / QQ_BOT_APP_ID / QQ_BOT_SECRET")
    print("   请 cp .env.example .env 后填入真实值")
    sys.exit(1)

HTTP_TIMEOUT = 10.0


def _http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> tuple[int, str, dict[str, str]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, raw, dict(e.headers or {})


def _print_result(label: str, status: int, body: str) -> None:
    print(f"[{label}] HTTP {status}")
    print(f"[{label}] body: {body[:500]}")


def try_gateway() -> bool:
    """路径 A：OpenClaw Gateway channel API"""
    print("\n=== 路径 A: OpenClaw Gateway /api/channels/qqbot/send ===")
    url = f"{GATEWAY_URL}/api/channels/qqbot/send"
    headers = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
    body = {
        "user_openid": USER_OPENID,
        "content": TEST_CONTENT,
        "msg_type": 0,
    }
    try:
        status, resp, _ = _http_request("POST", url, headers=headers, body=body)
    except Exception as e:
        print(f"[GW] 请求异常: {type(e).__name__}: {e}")
        return False
    _print_result("GW", status, resp)
    return 200 <= status < 300


def get_qq_access_token() -> str | None:
    """QQ 开放平台：换 access_token"""
    print("\n=== 路径 B-1: getAppAccessToken ===")
    try:
        status, resp, _ = _http_request(
            "POST",
            QQ_TOKEN_URL,
            body={"appId": QQ_APP_ID, "clientSecret": QQ_CLIENT_SECRET},
        )
    except Exception as e:
        print(f"[TOKEN] 请求异常: {type(e).__name__}: {e}")
        return None
    _print_result("TOKEN", status, resp)
    if not (200 <= status < 300):
        return None
    try:
        data = json.loads(resp)
    except Exception:
        return None
    token = data.get("access_token")
    if not token:
        print("[TOKEN] 响应里没 access_token 字段")
        return None
    print(f"[TOKEN] OK，长度={len(token)}，expires_in={data.get('expires_in')}")
    return token


def try_direct_qq(access_token: str) -> bool:
    """路径 B-2：POST /v2/users/{openid}/messages"""
    print("\n=== 路径 B-2: QQ C2C /v2/users/{openid}/messages ===")
    url = f"{QQ_API_BASE}/v2/users/{USER_OPENID}/messages"
    headers = {"Authorization": f"QQBot {access_token}"}
    body = {"content": TEST_CONTENT, "msg_type": 0}
    try:
        status, resp, _ = _http_request("POST", url, headers=headers, body=body)
    except Exception as e:
        print(f"[C2C] 请求异常: {type(e).__name__}: {e}")
        return False
    _print_result("C2C", status, resp)
    return 200 <= status < 300


def main() -> int:
    print("=" * 60)
    print("Scout → QQ 主动推送测试")
    print(f"目标 openid: {USER_OPENID}")
    print(f"消息内容:    {TEST_CONTENT}")
    print("=" * 60)

    # 路径 A
    if try_gateway():
        print("\n✅ 路径 A 成功（OpenClaw Gateway）。请检查 QQ 是否收到消息。")
        return 0
    print("\n⚠️  路径 A 失败，切到路径 B（直接 QQ API）。")

    # 路径 B
    token = get_qq_access_token()
    if not token:
        print("\n❌ 路径 B 拿不到 access_token，放弃。")
        return 2
    if try_direct_qq(token):
        print("\n✅ 路径 B 成功（直接 QQ API）。请检查 QQ 是否收到消息。")
        return 0
    print("\n❌ 路径 B 也失败。")
    return 3


if __name__ == "__main__":
    sys.exit(main())
