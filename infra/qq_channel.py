"""
infra/qq_channel.py — QQ 开放平台 C2C 主动推送通道（v1.13 Phase 2A）

职责：让 Scout 能直接把一条文本消息 POST 到 QQ 用户（单点，C2C）。
定位：push_consumer_agent 的发送后端；token/rate-limit 在本模块内完成。

协议（参考 scripts/test_qq_push.py 验证过的路径 B）：
    Step 1: POST https://bots.qq.com/app/getAppAccessToken
            body = {"appId": ..., "clientSecret": ...}
            → {"access_token": "...", "expires_in": "<seconds as str>"}
    Step 2: POST https://api.sgroup.qq.com/v2/users/{openid}/messages
            header  Authorization: QQBot <access_token>
            body    {"content": "<text>", "msg_type": 0}
            → 200 + {"id": ..., "timestamp": ...}

**不 raise**：失败返回 (False, detail)。Scout 的消费端应把 detail 写入
`message_queue.error_message`，由 PushQueue.mark_failed 控制重试。

Token 缓存：
    - 进程内存，实例变量（不跨进程持久化）
    - 到期前 30 秒主动刷新（QQ 官方 expires_in 通常 7200s，有时 ≈ 155s — 保守刷新）
    - 线程不安全（Scout 单 event loop，不会并发 send；手动并发请加锁）

Rate limit：
    - 实例内滑动窗口，默认 1 分钟 ≤ 10 条
    - 超限 → (False, {"error": "rate_limited"}) 由调用方决定是否跳过/延迟
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple


QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_API_BASE = "https://api.sgroup.qq.com"
DEFAULT_HTTP_TIMEOUT = 10.0
TOKEN_REFRESH_SKEW_SECONDS = 30


class QQPushChannel:
    """单用户 C2C 主动推送通道。

    典型用法（secrets 从 env 读，不硬编码）:
        channel = QQPushChannel(
            app_id=os.getenv("QQ_BOT_APP_ID"),
            client_secret=os.getenv("QQ_BOT_SECRET"),
            user_openid=os.getenv("QQ_USER_OPENID"),
            rate_limit_per_minute=10,
        )
        ok, detail = channel.send("[Scout] 测试消息")
    """

    def __init__(
        self,
        app_id: str,
        client_secret: str,
        user_openid: str,
        rate_limit_per_minute: int = 10,
        max_content_length: int = 900,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
    ):
        if not app_id or not client_secret or not user_openid:
            raise ValueError("app_id / client_secret / user_openid required")
        self.app_id = app_id
        self.client_secret = client_secret
        self.user_openid = user_openid
        self.rate_limit_per_minute = max(1, int(rate_limit_per_minute))
        self.max_content_length = max(10, int(max_content_length))
        self.http_timeout = float(http_timeout)

        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._recent_sends: Deque[float] = deque()

    # ─────────────── 对外主 API ───────────────

    def send(self, content: str) -> Tuple[bool, Dict[str, Any]]:
        """发送一条文本消息到配置的 user_openid。

        Returns:
            (ok, detail) — detail 字段（成功 / 失败均可含）:
                http_status: int
                body:        str (原始响应前 500 字符)
                error:       str (失败时)
                msg_id:      str (成功时，QQ 返回的消息 id)
                timestamp:   str (成功时)
        """
        if not isinstance(content, str) or not content.strip():
            return False, {"error": "empty_content"}

        text = content.strip()
        if len(text) > self.max_content_length:
            text = text[: self.max_content_length - 3] + "..."

        if not self._rate_limit_ok():
            return False, {
                "error": "rate_limited",
                "limit_per_minute": self.rate_limit_per_minute,
                "window_count": len(self._recent_sends),
            }

        token = self._get_token()
        if not token:
            return False, {"error": "token_fetch_failed"}

        url = f"{QQ_API_BASE}/v2/users/{self.user_openid}/messages"
        headers = {"Authorization": f"QQBot {token}"}
        body = {"content": text, "msg_type": 0}

        status, resp_body, err = _http_post_json(
            url, body, headers=headers, timeout=self.http_timeout
        )
        if err:
            return False, {"error": f"network: {err}"}

        # 记录 send 时间戳（无论业务成败都算一次 call，防止被 QQ 侧限流）
        self._recent_sends.append(time.time())

        detail: Dict[str, Any] = {
            "http_status": status,
            "body": (resp_body or "")[:500],
        }
        if 200 <= (status or 0) < 300:
            try:
                parsed = json.loads(resp_body) if resp_body else {}
                detail["msg_id"] = parsed.get("id")
                detail["timestamp"] = parsed.get("timestamp")
            except Exception:
                pass
            return True, detail

        detail["error"] = f"http_{status}"
        return False, detail

    # ─────────────── token 管理 ───────────────

    def _get_token(self) -> Optional[str]:
        """返回有效 token；过期（或差 <30s 过期）时刷新。"""
        now = time.time()
        if self._token and now < (self._token_expires_at - TOKEN_REFRESH_SKEW_SECONDS):
            return self._token
        return self._refresh_token()

    def _refresh_token(self) -> Optional[str]:
        status, resp_body, err = _http_post_json(
            QQ_TOKEN_URL,
            {"appId": self.app_id, "clientSecret": self.client_secret},
            headers=None,
            timeout=self.http_timeout,
        )
        if err or status is None or not (200 <= status < 300):
            return None
        try:
            parsed = json.loads(resp_body)
        except Exception:
            return None
        token = parsed.get("access_token")
        if not token:
            return None
        try:
            expires_in = float(parsed.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0.0
        if expires_in <= 0:
            expires_in = 60.0
        self._token = token
        self._token_expires_at = time.time() + expires_in
        return token

    # ─────────────── rate limit ───────────────

    def _rate_limit_ok(self) -> bool:
        now = time.time()
        window_start = now - 60.0
        while self._recent_sends and self._recent_sends[0] < window_start:
            self._recent_sends.popleft()
        return len(self._recent_sends) < self.rate_limit_per_minute


# ═══════════════════ 构造器（给 push_consumer 用） ═══════════════════

def build_qq_channel_from_config(qq_cfg: Any) -> QQPushChannel:
    """从 ScoutConfig.qq_push 段构造 channel。

    支持 Pydantic QQPushConfig 对象（推荐）或 dict。不做 enabled 检查
    （调用方决定是否启用）。
    """
    def _get(name: str) -> Any:
        if isinstance(qq_cfg, dict):
            return qq_cfg.get(name)
        return getattr(qq_cfg, name, None)

    return QQPushChannel(
        app_id=_get("app_id"),
        client_secret=_get("client_secret"),
        user_openid=_get("user_openid"),
        rate_limit_per_minute=_get("rate_limit_per_minute") or 10,
        max_content_length=_get("max_content_length") or 900,
    )


# ═══════════════════ 内部辅助 ═══════════════════

def _http_post_json(
    url: str,
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> Tuple[Optional[int], str, Optional[str]]:
    """POST JSON → (status, body_text, network_error_or_none)。

    HTTP 4xx/5xx 不算 network error — 返回 (status, body, None)。
    只有 socket/urlopen 级别异常才返 (None, "", error_msg)。
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, raw, None
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}"
