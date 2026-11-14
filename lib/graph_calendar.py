# -*- coding: utf-8 -*-
"""用 Microsoft Graph 直接写 Outlook 日历，不依赖本机 Outlook。

为什么不用别的：本机 Outlook 的 COM 只能在装了 Outlook 的 Windows 桌面上跑，`.ics`
又要人手动导入，都不适合无人值守。Graph 是普通 HTTPS 接口，Windows / Linux / 容器里
都能调；登录做一次，之后用缓存的 refresh_token 自动续。

认证走**设备代码流**（delegated + offline_access）：第一次手动登录一次，把 refresh_token
存本地（`state/`），之后每次跑用它换 access_token，过期自动续。

本模块同时管三件事，都不依赖邮件那一侧：

    认证     设备码登录 / refresh 换票 / token 缓存（state/）
    Graph    /me、建日历组 / 日历、批量写事件
    同步策略  sync() 的 auto / graph / none 与演练（plan_only）

调试入口：`python scripts/graph_auth_check.py`（验票）与 `python scripts/send_digest.py --graph-login`。
Azure 应用注册、报错对照、token 存放位置见 docs/outlook-graph-calendar.md。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from constants import UTC
from paths import STATE_DIR

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/{tenant}"
# 个人账户拿到的不透明票（EwB…）Graph 也接受，scope 写法不影响能不能用；
# 留环境变量只是方便临时换权限试
SCOPES = os.environ.get("MAILER_GRAPH_SCOPE") or "offline_access Calendars.ReadWrite"
# token 本身就是密码：放 state/（已 gitignore），不要混在 downloads/ 里
DEFAULT_TOKEN = STATE_DIR / ".graph_token.json"
# 工具建的日历统一放在这个日历组（Outlook 里的目录）下；可用环境变量覆盖
CALENDAR_GROUP = os.environ.get("MAILER_CALENDAR_GROUP") or "Automation"


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


def token_path() -> Path:
    """当前 token 缓存位置；`MAILER_GRAPH_TOKEN` 可以覆盖。"""
    return Path(os.environ.get("MAILER_GRAPH_TOKEN") or DEFAULT_TOKEN)


def load_creds() -> Optional[Creds]:
    client_id = os.environ.get("MAILER_GRAPH_CLIENT_ID")
    if not client_id:
        return None
    return Creds(
        client_id=client_id,
        tenant=os.environ.get("MAILER_GRAPH_TENANT", "common"),
        token_path=token_path(),
    )


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

_OPENER = None


def set_proxy(url: str) -> None:
    """让本模块的请求统一走代理（大陆直连 login.microsoftonline.com 常超时）。"""
    global _OPENER
    _OPENER = build_opener(ProxyHandler({"http": url, "https": url}))


def _open(request: Request, timeout: int = 30):
    """不设代理时交给 urllib 默认行为（会读 http_proxy 等环境变量）。"""
    return _OPENER.open(request, timeout=timeout) if _OPENER else urlopen(request, timeout=timeout)


def fetch_json(url: str, timeout: int = 30) -> Dict:
    """GET 一个不需要 token 的 JSON（取签名公钥、deviclogin 页面之类）。"""
    try:
        with _open(Request(url), timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise GraphError("HTTP {}：{}".format(exc.code, url), code=str(exc.code))
    except URLError as exc:
        raise GraphError("连不上 {}：{}".format(url.split("/")[2], exc.reason))


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
        with _open(request, timeout) as response:
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
        with _open(request, timeout) as response:
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


def device_token(creds: Creds, echo=print, scope: str = "") -> Dict:
    """跑完设备码流，返回**原始** token 响应（不落盘）。

    不落盘是为了让诊断工具（scripts/graph_auth_check.py）能在验签/解码之后再决定要不要存。
    ``scope`` 留空则用模块级的 `SCOPES`。
    """
    base = AUTHORITY.format(tenant=creds.tenant)
    device = _post_form(base + "/oauth2/v2.0/devicecode",
                       {"client_id": creds.client_id, "scope": scope or SCOPES})
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
        access = token.get("access_token") or ""
        if access and not _looks_like_jwt(access):
            # 实测过：个人账户（MSA）拿到的是 EwB… 不透明票，Graph 照样接受，不是错误。
            echo("提示：access_token 是不透明票（{} 字符），个人账户的常态，Graph 认这种票".format(len(access)))
        return token
    raise GraphError("等满 {} 秒还没完成授权，重新跑一次".format(int(device.get("expires_in", 900))))


def device_login(creds: Creds, echo=print, scope: str = "") -> Dict:
    """设备代码流：打印短码让用户去浏览器确认，成功后把 token 存盘。"""
    token = device_token(creds, echo, scope)
    store_token(creds, token)
    echo("登录成功，token 已存到 {}".format(creds.token_path))
    return token


def exchange_refresh_token(client_id: str, tenant: str, scope: str, refresh_token: str) -> Dict:
    """用 refresh_token 换新票（不重新走设备码）。scope 可以随便换，诊断工具靠它试权限。"""
    return _post_form(
        AUTHORITY.format(tenant=tenant) + "/oauth2/v2.0/token",
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": scope,
        },
    )


def store_token(creds: Creds, token: Dict) -> None:
    """把 token 响应写进缓存（refresh/access/过期时间）。"""
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
        raise GraphError("没配 MAILER_GRAPH_CLIENT_ID，注册步骤见 docs/outlook-graph-calendar.md")
    cache = _read_cache(creds.token_path)
    cached = cache.get("access_token") or ""
    if cached and cache.get("expires_at", 0) > time.time() + 120:
        return cached
    if not cache.get("refresh_token"):
        raise GraphError("还没有登录过：先跑 python scripts/send_digest.py --graph-login")

    token = exchange_refresh_token(
        creds.client_id, creds.tenant, SCOPES, cache["refresh_token"]
    )
    store_token(creds, token)
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


def load_token_cache(creds: Optional[Creds] = None) -> Dict:
    """读缓存原文（含 refresh_token）。只给诊断工具用，别拿去打印。"""
    return _read_cache(creds.token_path if creds else token_path())


def me(token: str) -> Dict:
    """用一枚现成的 access_token 调 /me——票能不能用，最终由 Graph 说了算。"""
    return _call(
        "GET",
        "{}/me?$select=displayName,userPrincipalName,mail".format(GRAPH),
        token,
    )


def whoami(creds: Optional[Creds] = None) -> Dict:
    """当前 token 属于哪个账号——日历就写到它名下。"""
    return me(access_token(creds))


def _find_calendar_item(token: str, name: str) -> Optional[Dict]:
    """按名字找日历，返回完整对象（含 isDefaultCalendar），没找到返回 None。"""
    listing = _call(
        "GET",
        "{}/me/calendars?$select=id,name,isDefaultCalendar&$top=100".format(GRAPH),
        token,
    )
    for item in listing.get("value", []):
        if item.get("name") == name:
            return item
    return None


def find_calendar(token: str, name: str) -> Optional[str]:
    """按名字找日历文件夹；没有返回 None（**不会新建**）。"""
    item = _find_calendar_item(token, name)
    return item["id"] if item else None


def find_calendar_group(token: str, name: str) -> Optional[str]:
    """按名字找日历组（Outlook 的「目录」）；没有返回 None。"""
    listing = _call(
        "GET",
        "{}/me/calendarGroups?$select=id,name&$top=100".format(GRAPH),
        token,
    )
    for item in listing.get("value", []):
        if item.get("name") == name:
            return item["id"]
    return None


def ensure_calendar_group(token: str, name: str) -> str:
    """找到同名日历组，没有就新建一个。"""
    existing = find_calendar_group(token, name)
    if existing:
        return existing
    created = _call("POST", "{}/me/calendarGroups".format(GRAPH), token, {"name": name})
    return created["id"]


def _group_calendars(token: str, group_id: str) -> List[Dict]:
    listing = _call(
        "GET",
        "{}/me/calendarGroups/{}/calendars?$select=id,name&$top=100".format(GRAPH, group_id),
        token,
    )
    return listing.get("value", [])


def _copy_events(token: str, source_id: str, target_id: str) -> int:
    """把旧日历里的事件原样复制到新日历，返回复制的条数。"""
    url = (
        "{}/me/calendars/{}/events"
        "?$select=subject,body,start,end,location,isReminderOn,"
        "reminderMinutesBeforeStart,showAs&$top=200".format(GRAPH, source_id)
    )
    target = "{}/me/calendars/{}/events".format(GRAPH, target_id)
    copied = 0
    while url:
        data = _call("GET", url, token)
        for item in data.get("value", []):
            _call(
                "POST",
                target,
                token,
                {
                    "subject": item.get("subject") or "",
                    "body": item.get("body") or {"contentType": "text", "content": ""},
                    "start": item.get("start") or {},
                    "end": item.get("end") or {},
                    "location": item.get("location") or {"displayName": ""},
                    "isReminderOn": bool(item.get("isReminderOn", True)),
                    "reminderMinutesBeforeStart": item.get("reminderMinutesBeforeStart", 30),
                    "showAs": item.get("showAs", "free"),
                },
            )
            copied += 1
        url = data.get("@odata.nextLink") or ""
    return copied


def _rename_calendar(token: str, calendar_id: str, name: str) -> None:
    _call("PATCH", "{}/me/calendars/{}".format(GRAPH, calendar_id), token, {"name": name})


def _migrate_calendar(
    token: str, name: str, old_id: str, group: str, group_id: str
) -> str:
    """Graph 没有「移动日历」动作：改旧名 → 新组建原名 → 复制事件 → 删旧。

    Graph 不允许同名日历共存，所以必须先给旧日历腾出名字；
    中途失败就删掉新日历、把旧名字改回去，保证不丢数据。
    """
    backup = "{} (迁移中 {})".format(name, int(time.time()))
    _rename_calendar(token, old_id, backup)
    new_id = ""
    try:
        created = _call(
            "POST",
            "{}/me/calendarGroups/{}/calendars".format(GRAPH, group_id),
            token,
            {"name": name},
        )
        new_id = created["id"]
        copied = _copy_events(token, old_id, new_id)
    except Exception:
        if new_id:
            try:
                _call("DELETE", "{}/me/calendars/{}".format(GRAPH, new_id), token)
            except Exception:
                log.warning("清理未完成的新日历失败：%s", new_id)
        try:
            _rename_calendar(token, old_id, name)
        except Exception as exc:
            log.warning("旧日历改回原名失败（现在叫「%s」）：%s", backup, exc)
        raise
    try:
        _call("DELETE", "{}/me/calendars/{}".format(GRAPH, old_id), token)
    except Exception as exc:
        log.warning("旧日历已改名为「%s」但删除失败，可在 Outlook 里手动删除：%s", backup, exc)
    log.info("已把日历「%s」迁移到「%s」，复制 %d 条事件", name, group, copied)
    return new_id


def ensure_calendar(token: str, name: str, group: str = CALENDAR_GROUP) -> str:
    """找到同名日历，没有就在 group 下发新建一个。

    已有日历在别的组时，用「复制事件 + 删旧日历」迁移过去（Graph 没有移动接口）；
    默认日历不动，避免误删。
    """
    group_id = ensure_calendar_group(token, group)
    for item in _group_calendars(token, group_id):
        if item.get("name") == name:
            return item["id"]
    item = _find_calendar_item(token, name)
    if item is not None:
        if item.get("isDefaultCalendar"):
            log.warning("「%s」是默认日历，无法迁移到「%s」", name, group)
            return item["id"]
        return _migrate_calendar(token, name, item["id"], group, group_id)
    created = _call(
        "POST",
        "{}/me/calendarGroups/{}/calendars".format(GRAPH, group_id),
        token,
        {"name": name},
    )
    return created["id"]


def _minute(value: str) -> str:
    """把 Graph 的 dateTime 裁到分钟（带 7 位小数）作为匹配键。

    只给时间用：它会把任意字符串砍到 16 个字符，别拿它处理标题。
    """
    return (value or "")[:16]


def _utc(value: datetime) -> str:
    """Graph 要的 ISO8601 UTC 写法，例如 2026-11-17T12:00:00Z。"""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _match_key(subject, start_text) -> Tuple[str, str]:
    """去重键：完整标题 + 裁到分钟的开始时间。

    标题必须原样比，不能截断——截断会让「曼城 vs 曼联」和「曼城 vs 曼联二队」撞车。
    """
    return ((subject or ""), _minute(start_text))


def _unique_events(events) -> List:
    """同一批里标题 + 开始时间相同的只留最后一条，避免一次就写两份。"""
    unique = {}
    for event in events:
        unique[_match_key(event.subject, _utc(event.start))] = event
    return list(unique.values())


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
    """写进 Graph 上的日历。返回 {calendar, created, updated, deleted}。

    去重键是 `subject + 开始时间（分钟）`：同一批里重复的只留一条；日历里
    已经同键的事件只更新第一条，多出来的副本删掉——重复跑不会越写越多。
    """
    creds = load_creds()
    if creds is None:
        raise GraphError("没配 MAILER_GRAPH_CLIENT_ID，注册步骤见 docs/outlook-graph-calendar.md")
    events = _unique_events(events)
    if plan_only:
        return {"calendar": name, "created": len(events), "updated": 0, "deleted": 0}

    token = access_token(creds)
    calendar_id = ensure_calendar(token, name)

    low = min(e.start for e in events).astimezone(UTC) - timedelta(days=1)
    high = max(e.end for e in events).astimezone(UTC) + timedelta(days=1)
    url = "{}/me/calendars/{}/calendarView?startDateTime={}&endDateTime={}&$select=id,subject,start&$top=200".format(
        GRAPH, calendar_id, _utc(low), _utc(high)
    )
    existing: Dict[Tuple[str, str], List[str]] = {}
    for item in _call("GET", url, token).get("value", []):
        key = _match_key(item.get("subject"), (item.get("start") or {}).get("dateTime"))
        existing.setdefault(key, []).append(item["id"])

    created = updated = deleted = 0
    for event in events:
        body = _payload(event)
        key = _match_key(event.subject, body["start"]["dateTime"])
        ids = existing.get(key) or []
        if ids:
            _call("PATCH", "{}/me/events/{}".format(GRAPH, ids[0]), token, body)
            updated += 1
            for extra in ids[1:]:
                _call("DELETE", "{}/me/events/{}".format(GRAPH, extra), token)
                deleted += 1
        else:
            _call("POST", "{}/me/calendars/{}/events".format(GRAPH, calendar_id), token, body)
            created += 1
        existing[key] = []  # 同一批里再来一条也不会重复 POST
    return {"calendar": name, "created": created, "updated": updated, "deleted": deleted}


def create_event(calendar_id: str, event, creds: Optional[Creds] = None) -> str:
    """直接建一条事件，不走去重。测试里用来故意造副本、验证清理。"""
    data = _call(
        "POST",
        "{}/me/calendars/{}/events".format(GRAPH, calendar_id),
        access_token(creds),
        _payload(event),
    )
    return data.get("id", "")


def list_events(calendar_id: str, start: datetime, end: datetime,
                creds: Optional[Creds] = None) -> List[Dict]:
    """列出某个日历在 [start, end] 窗口内的事件。核对写入结果、清理测试数据用。"""
    url = "{}/me/calendars/{}/calendarView?startDateTime={}&endDateTime={}" \
          "&$select=id,subject,start,end&$top=200".format(
              GRAPH, calendar_id, _utc(start), _utc(end))
    return _call("GET", url, access_token(creds)).get("value", [])


def delete_event(event_id: str, creds: Optional[Creds] = None) -> None:
    """删一条事件（进「已删除邮件」，不是永久删除）。"""
    _call("DELETE", "{}/me/events/{}".format(GRAPH, event_id), access_token(creds))


# ---------------------------------------------------------------- 同步策略


@dataclass
class CalendarEvent:
    """要写进日历的一条日程。插件组好这个对象交给 sync()。

    幂等靠 `subject + 开始时间` 去重（见 sync_graph），不需要插件再给 key：
    同批重复只留一条，日历里已有的副本只保留一条，多余的会被删掉。
    """

    subject: str
    start: datetime
    end: datetime
    location: str = ""
    body: str = ""
    reminder_minutes: int = 30


@dataclass
class CalendarResult:
    backend: str
    target: str = ""
    created: int = 0
    updated: int = 0
    deleted: int = 0
    note: str = ""
    plan_only: bool = False

    def line(self) -> str:
        """给邮件正文的一句话；返回空串表示不用提。"""
        if self.backend == "none":
            return "日历：未写入（{}）".format(self.note) if self.note else ""
        if self.plan_only:
            return "日历：演练，将同步 {} 场到「{}」".format(self.created, self.target)
        if self.created or self.updated or self.deleted:
            text = "日历：新建 {} 场、更新 {} 场".format(self.created, self.updated)
            if self.deleted:
                text += "、清理重复 {} 条".format(self.deleted)
            return text + "（「{}」）".format(self.target)
        return "日历：已是最新（「{}」）".format(self.target)


def graph_ready() -> bool:
    """配了 client id 且登录过（有 token 缓存）才算可用——cron 里没法交互登录。"""
    return has_token(load_creds())


def sync(events: List[CalendarEvent], name: str, backend: str = "auto",
         plan_only: bool = False) -> CalendarResult:
    """把 events 写进 Graph 上的日历。plan_only 只报告、不联网、不落地。"""
    if backend == "none":
        return CalendarResult("none", name)
    if not events:
        return CalendarResult("none", name, note="没有可写的比赛")
    if backend == "auto" and not graph_ready():
        # 没配置就安静跳过：CLI 会提示一次，邮件正文不必每封都写一行
        return CalendarResult("none", name)

    try:
        data = sync_graph(events, name, plan_only=plan_only)
    except Exception as exc:
        log.warning("Graph 写入失败：%s", exc)
        if backend == "auto":
            return CalendarResult("none", name, note="Graph 写入失败")
        raise

    return CalendarResult(
        "graph",
        target=data.get("calendar", name),
        created=int(data.get("created", 0)),
        updated=int(data.get("updated", 0)),
        deleted=int(data.get("deleted", 0)),
        plan_only=plan_only,
    )
