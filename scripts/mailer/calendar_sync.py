# -*- coding: utf-8 -*-
"""把比赛写进日历——只走 Microsoft Graph。

为什么只有这一条路：

    Outlook 本机 COM   只能在装了 Outlook 的 Windows 桌面上跑，服务器上没有，删了
    .ics 文件/附件     得靠人手动导入，或者挂成邮件附件，都不要

于是剩下 Graph：`https://graph.microsoft.com` 是 HTTP 接口，Windows、Linux 服务器、
容器里都能调。认证用设备代码流登录一次，把 refresh_token 存本地，之后无人值守跑，
过期自动续。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from .core import CST

log = logging.getLogger("mailer")


@dataclass
class CalendarEvent:
    key: str
    subject: str
    start: datetime
    end: datetime
    location: str = ""
    body: str = ""
    reminder_minutes: int = 30

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "subject": self.subject,
            # Graph 那边统一按 UTC 发，这里保留北京时间给日志看
            "start": self.start.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
            "end": self.end.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
            "location": self.location or "",
            "body": self.body or "",
            "reminder": self.reminder_minutes,
        }


@dataclass
class CalendarResult:
    backend: str
    target: str = ""
    created: int = 0
    updated: int = 0
    note: str = ""
    plan_only: bool = False

    @property
    def ok(self) -> bool:
        return self.backend == "graph"

    def line(self) -> str:
        """给邮件正文用的一句话；返回空串表示不用提。"""
        if self.backend == "none":
            return "日历没写：{}".format(self.note) if self.note else ""
        prefix = "演练：" if self.plan_only else ""
        if self.plan_only:
            return "{}将同步到 Outlook 日历「{}」{} 场".format(prefix, self.target, self.created)
        if self.created or self.updated:
            return "已同步到 Outlook 日历「{}」：新建 {} 场、更新 {} 场".format(
                self.target, self.created, self.updated
            )
        return "Outlook 日历「{}」已是最新".format(self.target)


def graph_ready() -> bool:
    """配了 client id 且登录过（有 token 缓存）才算可用——cron 里没法交互登录。"""
    try:
        from .graph_calendar import has_token, load_creds
    except Exception:
        return False
    return has_token(load_creds())


def sync(
    events: List[CalendarEvent],
    name: str,
    backend: str = "auto",
    plan_only: bool = False,
) -> CalendarResult:
    """把 events 写进 Graph 上的日历。plan_only 只报告、不联网、不落地。"""
    if backend == "none":
        return CalendarResult("none", name)
    if not events:
        return CalendarResult("none", name, note="没有可写的比赛")
    if backend == "auto" and not graph_ready():
        return CalendarResult("none", name, note="还没配置日历凭据")

    try:
        from .graph_calendar import sync_graph

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
        plan_only=plan_only,
    )
