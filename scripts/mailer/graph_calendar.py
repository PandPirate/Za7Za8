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
SCOPES = "offline_access Calendars.ReadWrite"
DEFAULT_TOKEN = Path(__file__).resolve().parents[2] / "downloads" / ".graph_token.json"


class GraphError(RuntimeError):
    pass


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


def _detail(exc: HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return "HTTP {}".format(getattr(exc, "code", "?"))
    return (
        body.get("error_description")
        or (body.get("error") or {}).get("message")
        or str(body.get("error"))
    )


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
        raise GraphError(_detail(exc))
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
            raise GraphError("登录已失效，重新跑一次 --graph-login")
        raise GraphError(_detail(exc))
    except URLError as exc:
        raise GraphError("连不上 graph.microsoft.com：{}".format(exc.reason))


# ---------------------------------------------------------------- 登录


def device_login(creds: Creds, echo=print) -> None:
    """设备代码流：打印一个短码，用户在浏览器里输入即可。"""
    base = AUTHORITY.format(tenant=creds.tenant)
    device = _post_form(base + "/oauth2/v2.0/devicecode", {"client_id": creds.client_id, "scope": SCOPES})
    echo("1) 打开 {}".format(device.get("verification_uri")))
    echo("2) 输入代码 {}".format(device.get("user_code")))
    echo("3) 用要写入日历的那个邮箱登录并同意（{} 秒内有效）".format(device.get("expires_in")))

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
            message = str(exc)
            if "authorization_pending" in message:
                continue
            if "slow_down" in message:
                interval += 5
                continue
            raise
        _store(creds, token)
        echo("登录成功，token 已存到 {}".format(creds.token_path))
        return
    raise GraphError("设备代码过期了，重新跑一次")


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


def access_token(creds: Optional[Creds] = None) -> str:
    creds = creds or load_creds()
    if creds is None:
        raise GraphError("没配 MAILER_GRAPH_CLIENT_ID，先看 README 注册 Azure 应用")
    cache = _read_cache(creds.token_path)
    if cache.get("access_token") and cache.get("expires_at", 0) > time.time() + 120:
        return cache["access_token"]
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
    return token["access_token"]


# ---------------------------------------------------------------- 日历


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
