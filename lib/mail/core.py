# -*- coding: utf-8 -*-
"""框架核心：插件协议、内容区块、MIME 组装、投递。

约定很简单——插件实现一个 build(ctx) -> Section | None：

* 返回 Section 就进邮件，返回 None 就当这次没有内容（框架会跳过）；
* 插件抛异常也不会拖垮整封邮件，记一条警告继续下一个；
* 区块按 priority 排序（小的在前），同一 priority 按 key 排。

插件只负责内容，发信、编码、版式、失败兜底都归框架。
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence

from . import __version__
from .template import render_document

log = logging.getLogger(__name__)


@dataclass
class Section:
    """一个内容区块：标题 + HTML 正文 + 纯文本正文。

    邮件只发正文，不带附件——日历走 Graph 写进账户，不靠附文件。
    """

    key: str
    title: str
    html: str
    text: str
    priority: int = 100


class Plugin(Protocol):
    """插件契约。名字用于 --plugin 选择，enabled_by_default 决定默认是否上场。"""

    name: str
    title: str
    description: str
    enabled_by_default: bool

    def build(self, ctx: "Context") -> Optional[Section]:
        ...


@dataclass
class Context:
    """运行时上下文：时间、网络超时、日历设置。"""

    now: datetime
    timeout: float = 15.0
    calendar_backend: str = "auto"      # auto / graph / none
    calendar_name: str = ""             # 空则由插件自己决定
    calendar_plan_only: bool = False    # True 只演练、不落地

    def http_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url, headers={"User-Agent": "mail/{}".format(__version__), "Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def warn(self, message: str) -> None:
        log.warning(message)


def load_plugins() -> Dict[str, Plugin]:
    """扫描 `mail.plugins` 包，把每个模块里的 PLUGIN 对象收集起来。"""
    import importlib
    import pkgutil

    from . import plugins as plugins_package  # 静态导入：包改名也不会默默扫到 0 个

    found: Dict[str, Plugin] = {}
    for info in pkgutil.iter_modules(plugins_package.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module("{}.{}".format(plugins_package.__name__, info.name))
        plugin = getattr(module, "PLUGIN", None)
        if plugin is None:
            continue
        if not all(hasattr(plugin, attr) for attr in ("name", "title", "description", "build")):
            log.warning("模块 %s 里的 PLUGIN 不符合契约，已跳过", module.__name__)
            continue
        found[plugin.name] = plugin
    return dict(sorted(found.items()))


def collect(ctx: Context, plugins: Sequence[Plugin]) -> List[Section]:
    sections: List[Section] = []
    for plugin in plugins:
        try:
            section = plugin.build(ctx)
        except Exception as exc:  # 一个插件崩了不该毁掉整封邮件
            log.warning("插件 %s 失败，已跳过：%s", plugin.name, exc)
            continue
        if section is None:
            log.info("插件 %s 这次没有内容", plugin.name)
            continue
        sections.append(section)
    sections.sort(key=lambda s: (s.priority, s.key))
    return sections


def default_subject(sections: Sequence[Section], now: datetime) -> str:
    titles = "、".join(s.title for s in sections) or "摘要"
    return "{:%Y-%m-%d} {}".format(now, titles)


def build_message(
    cfg: Any,
    sections: Sequence[Section],
    now: datetime,
    subject: Optional[str] = None,
) -> EmailMessage:
    html, text = render_document(sections, now, cfg.brand)

    message = EmailMessage()
    message["Subject"] = subject or default_subject(sections, now)
    # 预览时可能还没配发件人，给个占位地址，别留出空尖括号
    message["From"] = formataddr((cfg.brand, cfg.sender or "noreply@localhost"))
    if cfg.to:
        message["To"] = ", ".join(cfg.to)
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain="za7za8.local")
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def send_message(message: EmailMessage, cfg: Any) -> None:
    log.info("投递到 %s", cfg.masked())
    if cfg.tls == "ssl":
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=context) as smtp:
            if cfg.user:
                smtp.login(cfg.user, cfg.password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as smtp:
        smtp.ehlo()
        if cfg.tls == "starttls":
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if cfg.user:
            smtp.login(cfg.user, cfg.password)
        smtp.send_message(message)


def save_preview(message: EmailMessage, outdir: Path, stem: str = "digest") -> "tuple[Path, Path]":
    """把邮件存成 .eml（可直接拖进 Outlook 看）和 .html（浏览器预览）。"""
    outdir.mkdir(parents=True, exist_ok=True)
    eml = outdir / (stem + ".eml")
    eml.write_bytes(bytes(message))

    html_part = None
    for part in message.walk():
        if part.get_content_type() == "text/html":
            html_part = part.get_content()
            break
    page = outdir / (stem + ".html")
    page.write_text(html_part or "<p>没有 HTML 部分</p>", encoding="utf-8")
    return eml, page
