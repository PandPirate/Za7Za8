# -*- coding: utf-8 -*-
"""用 Microsoft Graph 直接写 Outlook 日历，给服务器 / Linux 部署用。

和 outlook 后端的区别：

    outlook  PowerShell + COM，只能跑在装了 Outlook 的 Windows 上
    graph    直接调 graph.microsoft.com，只要能出网就行（服务器、容器、树莓派都行）

认证走**设备代码流**（delegated + offline_access）：第一次手动登录一次，把 refresh_token
存本地，之后每次跑用它换 access_token，过期自动续。

    export MAILER_GRAPH_CLIENT_ID=<Azure 应用的客户端 ID>
    python scripts/send_digest.py --graph-login      # 打印验证地址和代码，浏览器里输入

需要先在 Azure 门户注册应用（详见 README）：支持个人 Microsoft 账户的公共客户端、
API 权限勾 Calendars.ReadWrite（委托），**不需要管理员同意**。
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .core import UTC

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/{tenant}"
# 个人账户拿到的不透明票（EwB…）Graph 也接受，scope 写法不影响能不能用；
# 留着环境变量只是方便临时换权限试
SCOPES = os.environ.get("MAILER_GRAPH_SCOPE") or "offline_access Calendars.ReadWrite"
DEFAULT_TOKEN = Path(__file__).resolve().parents[2] / "downloads" / ".graph_token.json"


class GraphError(RuntimeError):
    """带上 OAuth 的 error 码（authorization_pending 之类），光看描述文字不够可靠。"""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass
class Creds:
    client_id: str
    tenant: str
    token_path: Path


# ---------------------------------------------------------------- 配置


def load_creds() -> Optional[Creds]:
    client_id = os.environ.get("MAILER_GRAPH_CLIENT_ID")
    if not client_id:
        return None
    token_path = Path(os.environ.get("MAILER_GRAPH_TOKEN") or DEFAULT_TOKEN)
    return Creds(client_id=client_id, tenant=os.environ.get("MAILER_GRAPH_TENANT", "common"), token_path=token_path)


def has_token(creds: Optional[Creds] = None) -> bool:
    creds = creds or load_creds()
    return bool(creds and creds.token_path.exists())


def _read_cache(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_cache(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:  # token 就是密码，收紧权限（Windows 上会静默失败，无所谓）
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ---------------------------------------------------------------- HTTP


def _detail(exc: HTTPError) -> "tuple[str, str]":
    """把 HTTP 错误拆成 (error 码, 描述)。OAuth 的 error 是字符串，Graph 的是对象。"""
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return "", "HTTP {}".format(getattr(exc, "code", "?"))
    raw = body.get("error")
    if isinstance(raw, dict):
        code = raw.get("code") or ""
        message = raw.get("message") or ""
    else:
        code = raw or ""
        message = ""
    return code, body.get("error_description") or message or str(raw)


def _post_form(url: str, data: Dict, timeout: int = 30) -> Dict:
    request = Request(
        url,
        data=urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        code, message = _detail(exc)
        raise GraphError(message, code=code)
    except URLError as exc:
        raise GraphError("连不上 {}：{}".format(url.split("/")[2], exc.reason))


def _call(method: str, url: str, token: str, payload: Optional[Dict] = None, timeout: int = 30) -> Dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        if exc.code == 401:
            code, message = _detail(exc)
            raise GraphError(
                "token 被拒（HTTP 401）：{} {}".format(code or "?", message or "").strip(),
                code="unauthorized",
            )
        code, message = _detail(exc)
        raise GraphError(message, code=code)
    except URLError as exc:
        raise GraphError("连不上 graph.microsoft.com：{}".format(exc.reason))


# ---------------------------------------------------------------- 登录


def device_login(creds: Creds, echo=print) -> None:
    """设备代码流：打印一个短码，用户在浏览器里输入即可。"""
    base = AUTHORITY.format(tenant=creds.tenant)
    device = _post_form(base + "/oauth2/v2.0/devicecode", {"client_id": creds.client_id, "scope": SCOPES})
    code = device.get("user_code") or ""
    # 老接口有时回 https://login.microsoft.com/device，那个入口在「组织+个人」应用上会路由错，
    # 统一用现代的 devicelogin 页面。
    page = "https://microsoft.com/devicelogin"
    legacy = device.get("verification_uri") or ""
    complete = device.get("verification_uri_complete") or ""
    echo("=" * 52)
    if complete:
        echo("浏览器打开这个链接（码已带上）：")
        echo("  " + complete)
    else:
        echo("浏览器打开：")
        echo("  " + page)
        echo("输入代码：")
        echo("  " + code)
    if legacy and "devicelogin" not in legacy:
        echo("（接口另给了 {}，上面那个打不开时再试它）".format(legacy))
    echo("用要写入日历的那个邮箱登录并同意，{} 秒内有效".format(device.get("expires_in")))
    echo("=" * 52)
    echo("等待授权中…")

    interval = int(device.get("interval", 5))
    deadline = time.time() + int(device.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            token = _post_form(
                base + "/oauth2/v2.0/token",
                {
                    "client_id": creds.client_id,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device["device_code"],
                },
            )
        except GraphError as exc:
            # 关键：微软把「还没输码」报成 AADSTS70016 + error=authorization_pending，
            # 描述里没有 "authorization_pending" 这个词，只能靠 error 码判断。
            code = getattr(exc, "code", "") or ""
            if code == "authorization_pending" or "AADSTS70016" in str(exc):
                continue
            if code == "slow_down" or "AADSTS70006" in str(exc):
                interval += 5
                continue
            if code == "expired_token" or "AADSTS70020" in str(exc):
                raise GraphError("设备代码过期了，重新跑一次 --graph-login")
            if code == "authorization_declined" or "AADSTS70021" in str(exc):
                raise GraphError("你在浏览器里拒绝了授权，重新跑一次 --graph-login")
            raise
        _store(creds, token)
        access = token.get("access_token") or ""
        if access and not _looks_like_jwt(access):
            # 实测过：个人账户（MSA）拿到的是 EwB… 不透明票，Graph 照样接受，不是错误。
            echo("提示：access_token 是不透明票（{} 字符），个人账户的常态，Graph 认这种票".format(len(access)))
        echo("登录成功，token 已存到 {}".format(creds.token_path))
        return
    raise GraphError("等满 {} 秒还没完成授权，重新跑一次".format(int(device.get("expires_in", 900))))


def _store(creds: Creds, token: Dict) -> None:
    _write_cache(
        creds.token_path,
        {
            "client_id": creds.client_id,
            "tenant": creds.tenant,
            "refresh_token": token.get("refresh_token", ""),
            "access_token": token.get("access_token", ""),
            "expires_at": int(time.time()) + int(token.get("expires_in", 3600)),
        },
    )


def _looks_like_jwt(token: str) -> bool:
    """只是描述形状，不能当「能不能用」的判断。

    个人账户（MSA）的 access_token 是 EwB… 不透明票，不是 JWT，但 Graph 接受它；
    组织账户才稳定是三段 JWT。所以这个函数只拿来做提示文案，别拿来拦截。
    """
    return bool(token) and token.count(".") == 2


def access_token(creds: Optional[Creds] = None) -> str:
    creds = creds or load_creds()
    if creds is None:
        raise GraphError("没配 MAILER_GRAPH_CLIENT_ID，先看 README 注册 Azure 应用")
    cache = _read_cache(creds.token_path)
    cached = cache.get("access_token") or ""
    if cached and cache.get("expires_at", 0) > time.time() + 120:
        return cached
    if not cache.get("refresh_token"):
        raise GraphError("还没有登录过：先跑 python scripts/send_digest.py --graph-login")

    token = _post_form(
        AUTHORITY.format(tenant=creds.tenant) + "/oauth2/v2.0/token",
        {
            "client_id": creds.client_id,
            "grant_type": "refresh_token",
            "refresh_token": cache["refresh_token"],
            "scope": SCOPES,
        },
    )
    _store(creds, token)
    fresh = token.get("access_token") or ""
    if not fresh:
        raise GraphError("刷新后没拿到 access_token，重新跑一次 --graph-login")
    return fresh


# ---------------------------------------------------------------- 日历


def token_claims(creds: Optional[Creds] = None) -> Dict:
    """解出缓存里 access_token 的声明（不验签，只看 aud/scp 这类字段）。"""
    creds = creds or load_creds()
    if creds is None:
        return {}
    token = _read_cache(creds.token_path).get("access_token") or ""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def cache_info(creds: Optional[Creds] = None) -> Dict:
    """缓存的 token 是什么状态（不返回 token 本身）。"""
    creds = creds or load_creds()
    if creds is None:
        return {}
    cache = _read_cache(creds.token_path)
    return {
        "path": str(creds.token_path),
        "client_id": cache.get("client_id", ""),
        "tenant": cache.get("tenant", ""),
        "has_refresh": bool(cache.get("refresh_token")),
        "left": int(cache.get("expires_at", 0) - time.time()),
    }


def whoami(creds: Optional[Creds] = None) -> Dict:
    """当前 token 属于哪个账号——日历就写到它名下。"""
    return _call(
        "GET",
        "{}/me?$select=displayName,userPrincipalName,mail".format(GRAPH),
        access_token(creds),
    )


def ensure_calendar(token: str, name: str) -> str:
    """找到同名日历，没有就新建一个。"""
    listing = _call("GET", "{}/me/calendars?$select=id,name&$top=100".format(GRAPH), token)
    for item in listing.get("value", []):
        if item.get("name") == name:
            return item["id"]
    created = _call("POST", "{}/me/calendars".format(GRAPH), token, {"name": name})
    return created["id"]


def _stamp(value: str) -> str:
    """Graph 的 dateTime 带 7 位小数，统一成到分钟，方便做匹配键。"""
    return (value or "")[:16]


def _payload(event) -> Dict:
    return {
        "subject": event.subject,
        "body": {"contentType": "text", "content": event.body},
        "start": {"dateTime": event.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
        "end": {"dateTime": event.end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
        "location": {"displayName": event.location},
        "isReminderOn": bool(event.reminder_minutes),
        "reminderMinutesBeforeStart": event.reminder_minutes,
        "showAs": "free",
    }


def sync_graph(events, name: str, plan_only: bool = False) -> Dict:
    """写进 Graph 上的日历。返回 {calendar, created, updated}。"""
    creds = load_creds()
    if creds is None:
        raise GraphError("没配 MAILER_GRAPH_CLIENT_ID，先看 README 注册 Azure 应用")
    if plan_only:
        return {"calendar": name, "created": len(events), "updated": 0}

    token = access_token(creds)
    calendar_id = ensure_calendar(token, name)

    low = min(e.start for e in events).astimezone(UTC) - timedelta(days=1)
    high = max(e.end for e in events).astimezone(UTC) + timedelta(days=1)
    url = "{}/me/calendars/{}/calendarView?startDateTime={}&endDateTime={}&$select=id,subject,start&$top=200".format(
        GRAPH, calendar_id, low.strftime("%Y-%m-%dT%H:%M:%SZ"), high.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    existing = {}
    for item in _call("GET", url, token).get("value", []):
        existing[(_stamp(item.get("subject")), _stamp(item.get("start", {}).get("dateTime")))] = item["id"]

    created = updated = 0
    for event in events:
        body = _payload(event)
        key = (event.subject, _stamp(body["start"]["dateTime"]))
        if key in existing:
            _call("PATCH", "{}/me/events/{}".format(GRAPH, existing[key]), token, body)
            updated += 1
        else:
            _call("POST", "{}/me/calendars/{}/events".format(GRAPH, calendar_id), token, body)
            created += 1
    return {"calendar": name, "created": created, "updated": updated}
