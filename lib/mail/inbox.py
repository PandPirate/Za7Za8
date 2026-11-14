# -*- coding: utf-8 -*-
"""从 IMAP 收件箱读取回复邮件。

只用标准库：QQ / 163 / Gmail 的 IMAP 都是标准协议。默认从 SMTP 主机名推导
IMAP 主机（smtp.qq.com → imap.qq.com），也可以在配置里写 imap_host。
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import re
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import List, Optional

from .config import MailConfig
from .core import set_raw_header

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _since_date(days: int) -> str:
    value = datetime.now().astimezone().date() - timedelta(days=max(0, days))
    return "{} {}-{}".format(value.day, _MONTHS[value.month - 1], value.year)


def connect(cfg: MailConfig):
    """建立 IMAP SSL 连接并登录。"""
    import imaplib

    if not cfg.imap_host:
        raise RuntimeError("没配 IMAP 主机：设置 MAILER_IMAP_HOST，或用 smtp.* 主机名自动推导")
    if not cfg.user or not cfg.password:
        raise RuntimeError("IMAP 登录需要 MAILER_SMTP_USER / MAILER_SMTP_PASS")
    conn = imaplib.IMAP4_SSL(
        cfg.imap_host, cfg.imap_port, ssl_context=ssl.create_default_context()
    )
    conn.login(cfg.user, cfg.password)
    return conn


def search_recent(
    conn, days: int = 3, limit: int = 50, subject: str = ""
) -> List[bytes]:
    """返回最近几天的邮件 uid（新的在前，最多 limit 封）。

    给出 subject 时优先用 IMAP 的 SUBJECT 过滤（UTF-8）；服务器不支持就退回
    SINCE 搜索，由调用方再按头部过滤。
    """
    conn.select("INBOX", readonly=False)
    since = _since_date(days)
    status, data = "BAD", []
    if subject:
        try:
            status, data = conn.search(
                None,
                "CHARSET",
                "UTF-8",
                "SINCE",
                since,
                "SUBJECT",
                subject.encode("utf-8"),
            )
        except Exception:
            status, data = "BAD", []
    if status != "OK":
        status, data = conn.search(None, "SINCE", since)
    if status != "OK" or not data or not data[0]:
        return []
    return list(reversed(data[0].split()))[:limit]


def fetch_headers(conn, uid: bytes) -> Optional[EmailMessage]:
    """只取头部，用来过滤主题/发件人；比拉整封邮件快得多。"""
    status, parts = conn.fetch(
        uid,
        "(BODY.PEEK[HEADER.FIELDS "
        "(SUBJECT FROM DATE MESSAGE-ID REFERENCES THREAD-INDEX THREAD-TOPIC)])",
    )
    if status != "OK":
        return None
    for part in parts:
        if isinstance(part, tuple):
            return email.message_from_bytes(part[1], policy=email.policy.default)
    return None


def fetch_message(conn, uid: bytes) -> Optional[EmailMessage]:
    status, parts = conn.fetch(uid, "(RFC822)")
    if status != "OK":
        return None
    for part in parts:
        if isinstance(part, tuple):
            return email.message_from_bytes(part[1], policy=email.policy.default)
    return None


def mark_seen(conn, uid: bytes) -> None:
    conn.store(uid, "+FLAGS", "\\Seen")


def subject(msg: EmailMessage) -> str:
    return re.sub(r"[\r\n]+", " ", str(msg.get("Subject") or "")).strip()


def from_addr(msg: EmailMessage) -> str:
    return parseaddr(str(msg.get("From") or ""))[1].strip().lower()


def message_id(msg: EmailMessage) -> str:
    value = re.sub(r"[\r\n]+", "", str(msg.get("Message-ID") or "")).strip()
    if value:
        return value
    return "<sha1-{}@local>".format(hashlib.sha1(msg.as_bytes()).hexdigest())


def references(msg: EmailMessage) -> str:
    return re.sub(r"[\r\n]+", " ", str(msg.get("References") or "")).strip()


def _header(msg: EmailMessage, name: str, limit: int) -> str:
    """取单个头部：去掉换行、压成一行、截断，避免把用户内容原样塞回去。"""
    value = re.sub(r"[\r\n]+", " ", str(msg.get(name) or ""))
    return re.sub(r"\s+", " ", value).strip()[:limit]


def thread_topic(msg: EmailMessage) -> str:
    """Outlook 的会话主题（不带 Re: 的原始主题）。"""
    return _header(msg, "Thread-Topic", 200)


def thread_index(msg: EmailMessage) -> str:
    """Outlook 的会话索引（base64 二进制串）。"""
    return _header(msg, "Thread-Index", 400)


def apply_thread_headers(
    reply: EmailMessage, parent: EmailMessage, reply_to: str = ""
) -> None:
    """把 reply 接回 parent 所在的会话。

    Gmail / QQ 这类客户端只认 `In-Reply-To` + `References`；Outlook（Exchange）
    的会话视图按 `Thread-Index` 归组，缺了它即使主题和 References 都对，也会
    被拆成一封新邮件。所以父邮件带这两个头时原样抄过来。

    这三个头装的都是 Message-ID / base64 串，必须原样发送，走 `set_raw_header`
    而不是普通赋值（否则长 id 会被折成 RFC 2047 编码，见其注释）。
    """
    parent_id = reply_to or message_id(parent)
    if parent_id:
        set_raw_header(reply, "In-Reply-To", parent_id)
    refs = " ".join(value for value in (references(parent), parent_id) if value)
    if refs:
        set_raw_header(reply, "References", refs)
    topic = thread_topic(parent)
    if topic:
        reply["Thread-Topic"] = topic
    index = thread_index(parent)
    if index:
        set_raw_header(reply, "Thread-Index", index)


def received_at(msg: EmailMessage) -> str:
    raw = str(msg.get("Date") or "").strip()
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        value = datetime.now().astimezone()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().isoformat(timespec="seconds")


def received_day(msg: EmailMessage) -> str:
    return received_at(msg)[:10]


def reply_subject(value: str, fallback: str = "回复") -> str:
    base = re.sub(r"[\r\n]+", " ", value or "")
    base = re.sub(r"^\s*((Re|RE|回复)\s*[:：]\s*)+", "", base).strip()
    return "Re: " + (base[:200] or fallback)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    text = "".join(parser.parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def body_text(msg: EmailMessage) -> str:
    """优先取 text/plain；只有 HTML 时转成文本。"""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = part.get_content()
        except (KeyError, LookupError, UnicodeDecodeError):
            content = part.get_payload(decode=True) or b""
            if isinstance(content, bytes):
                content = content.decode("utf-8", "replace")
        if part.get_content_type() == "text/html":
            return _html_to_text(str(content))
        return str(content)
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace")
    return ""
