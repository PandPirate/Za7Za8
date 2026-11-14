#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""处理习惯打卡邮件的回复：读 IMAP → 解析 → 更新数据库 → 回信摘要。

    python scripts/habit_replies.py --dry-run          # 只解析，不写库、不发信
    python scripts/habit_replies.py                    # 处理最近 3 天的回复
    python scripts/habit_replies.py --since-days 7     # 扫描更久

默认读 `state/mail.json`（没有就读环境变量）；IMAP 主机不写时由 smtp.* 推导成 imap.*。
适合挂 cron，每 10 分钟跑一次。
"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import _bootstrap  # noqa: F401

import habit_tracker
from constants import CST
from mail.config import MailConfig
from mail.core import Section, build_message, send_message
from mail import inbox
from paths import STATE_DIR

DEFAULT_TO = "glycyrrhizin@outlook.com"


def _default_config() -> Optional[Path]:
    path = STATE_DIR / "mail.json"
    return path if path.exists() else None


def _day_for(subject: str, message) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", subject or "")
    if match:
        return match.group(1)
    return inbox.received_day(message)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="处理习惯打卡邮件的回复（IMAP 收信 + SMTP 回信）",
    )
    parser.add_argument("--config", type=Path, metavar="JSON", help="SMTP/IMAP 配置文件")
    parser.add_argument("--db", type=Path, metavar="PATH", help="SQLite 数据库路径")
    parser.add_argument("--since-days", type=int, default=3, metavar="N",
                        help="扫描最近几天的邮件，默认 3")
    parser.add_argument("--limit", type=int, default=50, metavar="N",
                        help="最多检查多少封，默认 50")
    parser.add_argument("--dry-run", action="store_true",
                        help="只解析并打印，不写数据库、不发信、不标记已读")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    cfg = MailConfig.load(args.config or _default_config())
    # state/mail.json 里可能只配了 SMTP，没有 to；用每日邮件同样的收件人兜底
    if not cfg.to:
        cfg.to = [DEFAULT_TO]
    problems = cfg.problems()
    if problems:
        print("邮件配置不完整：{}".format("、".join(problems)), file=sys.stderr)
        print("检查 state/mail.json 或 MAILER_* 环境变量", file=sys.stderr)
        return 2
    if not cfg.password:
        print("缺少 IMAP 密码（MAILER_SMTP_PASS）", file=sys.stderr)
        return 2

    allowed = {address.lower() for address in cfg.to}
    if not allowed:
        print("没有配置收件人，无法判断哪些回复来自用户", file=sys.stderr)
        return 2

    socket.setdefaulttimeout(30)
    try:
        conn = inbox.connect(cfg)
    except Exception as exc:
        print("IMAP 连接失败：{}".format(exc), file=sys.stderr)
        print(
            "检查 MAILER_IMAP_HOST；QQ 邮箱要在设置里开启 IMAP，并使用授权码登录",
            file=sys.stderr,
        )
        return 2

    processed = 0
    try:
        for uid in inbox.search_recent(
            conn, args.since_days, args.limit, habit_tracker.SUBJECT
        ):
            header = inbox.fetch_headers(conn, uid)
            if header is None:
                continue
            subject = inbox.subject(header)
            if habit_tracker.SUBJECT not in subject:
                continue
            sender = inbox.from_addr(header)
            if sender not in allowed:
                if args.verbose:
                    print("跳过非用户回复：{}".format(sender), file=sys.stderr)
                continue
            message_id = inbox.message_id(header)
            if habit_tracker.is_processed(message_id, args.db):
                continue
            day = _day_for(subject, header)
            try:
                message = inbox.fetch_message(conn, uid)
            except Exception as exc:
                print("取信失败（{}）：{}".format(subject, exc), file=sys.stderr)
                continue
            if message is None:
                continue
            body = inbox.body_text(message)
            commands, errors = habit_tracker.parse_commands(body)

            if args.dry_run:
                print("--- {}".format(subject))
                print("from: {}  day: {}".format(sender, day))
                for command in commands:
                    print("命令: {}".format(command))
                for error in errors:
                    print("错误: {}".format(error))
                continue

            result = habit_tracker.apply_reply(day, body, args.db)
            html, text = habit_tracker.render_summary(day, result, habit_tracker.day_rows(day, args.db))
            section = Section(
                key="habit", title=habit_tracker.SUBJECT, html=html, text=text, priority=30
            )
            reply = build_message(
                cfg, [section], datetime.now(CST), inbox.reply_subject(subject)
            )
            reply["In-Reply-To"] = message_id
            refs = " ".join(
                value for value in (inbox.references(header), message_id) if value
            )
            if refs:
                reply["References"] = refs
            try:
                send_message(reply, cfg)
            except Exception as exc:
                print("回信失败（{}）：{}".format(subject, exc), file=sys.stderr)
                continue
            habit_tracker.record_reply(
                message_id, day, inbox.received_at(header), args.db
            )
            inbox.mark_seen(conn, uid)
            processed += 1
            print("已回复：{}（{} 条命令，{} 条错误）".format(
                subject, len(commands), len(errors)))
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    if args.verbose:
        print("处理完成，共回复 {} 封".format(processed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
