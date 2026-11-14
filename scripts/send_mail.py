#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发一封普通邮件：标题 + 纯文本 / HTML + 附件。

    python scripts/send_mail.py --to a@b.com                      # 默认标题和正文
    python scripts/send_mail.py --to a@b.com --subject 标题 --body "正文"
    python scripts/send_mail.py --to a@b.com --subject 周报 --body-file note.txt
    python scripts/send_mail.py --to a@b.com --subject 图文 --html page.html --attach a.pdf
    python scripts/send_mail.py --to a@b.com --dry-run            # 只生成 .eml/.html

SMTP 配置和 `send_digest.py` 共用：环境变量（MAILER_SMTP_*）或 `--config` 指向的 JSON。
想看配置对不对、又不想真发信，就加 `--dry-run` —— 它把邮件写成 .eml/.html 放在
`downloads/mail_preview/`，可以拖进 Outlook 检查。

只发正文，不做摘要、不碰日历——那是 `send_digest.py` 的事。
"""

import argparse
import mimetypes
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from pathlib import Path

import _bootstrap  # noqa: F401  （把 lib/ 放进 sys.path）

from constants import CST
from mail.config import MailConfig, split_addresses
from mail.core import message_id, save_preview, send_message
from paths import PREVIEW_DIR

# 不写 --subject/--body 时用的默认值：这个脚本最常见的用途就是“发一封测试信”
DEFAULT_SUBJECT = "发信测试"
DEFAULT_BODY = "来自 send_mail.py"


def _read_text(path: Path) -> str:
    """读文本文件；`-` 表示标准输入。"""
    if str(path) == "-":
        return sys.stdin.read()
    return path.read_text(encoding="utf-8")


def build_message(cfg, args, text: str, html: str, now) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = args.subject or DEFAULT_SUBJECT
    # 没配发件人时给个占位地址，别留下空尖括号（预览用）
    message["From"] = formataddr((cfg.brand, cfg.sender or "noreply@localhost"))
    if cfg.sender:
        message["Reply-To"] = cfg.sender
    if cfg.to:
        message["To"] = ", ".join(cfg.to)
    # 必须是带时区偏移的写法（+0800）。以前用 formatdate(localtime=False) 写出 -0000，
    # 那是「时区未知」，部分垃圾邮件过滤器会当成可疑信号
    message["Date"] = format_datetime(now)
    message["Message-ID"] = message_id(cfg.sender)

    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")

    for raw in args.attach:
        path = Path(raw)
        if not path.is_file():
            raise SystemExit("附件不存在：{}".format(path))
        ctype, encoding = mimetypes.guess_type(str(path))
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                               filename=path.name)
    return message


def main(argv=None) -> int:
    # Windows 重定向到管道/文件时默认 GBK，统一成 UTF-8
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="发一封普通邮件（标题 + 正文 + 可选 HTML / 附件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  %(prog)s --to a@b.com                          # 默认标题和正文\n"
        "  %(prog)s --to a@b.com --subject 标题 --body \"正文\"\n"
        "  %(prog)s --to a@b.com --subject 周报 --body-file note.txt --attach 图.png\n"
        "  %(prog)s --to a@b.com --dry-run                # 只写 .eml/.html\n",
    )
    parser.add_argument("--to", metavar="ADDR",
                        help="收件人，多个用逗号分隔；不写则用 MAILER_TO")
    parser.add_argument("--subject", metavar="TEXT",
                        help="标题，默认「{}」".format(DEFAULT_SUBJECT))
    body = parser.add_mutually_exclusive_group()
    body.add_argument("--body", metavar="TEXT",
                      help="纯文本正文，默认「{}」".format(DEFAULT_BODY))
    body.add_argument("--body-file", type=Path, metavar="PATH", help="从文件读正文（- = stdin）")
    parser.add_argument("--html", type=Path, metavar="PATH",
                        help="附一个 HTML 版正文（- = stdin）")
    parser.add_argument("--attach", action="append", default=[], metavar="PATH",
                        help="附件，可重复")
    parser.add_argument("--config", type=Path, metavar="JSON", help="SMTP 配置文件")
    parser.add_argument("--from", dest="sender", metavar="ADDR", help="覆盖发件人地址")
    parser.add_argument("--brand", metavar="NAME", help="覆盖发件人显示名")
    parser.add_argument("--dry-run", action="store_true",
                        help="只生成 .eml/.html 预览，不发信")
    parser.add_argument("--debug", action="store_true",
                        help="打印完整 SMTP 对话（看服务端对每个收件人的返回码）")
    parser.add_argument("--out", type=Path, metavar="DIR", help="预览输出目录")
    args = parser.parse_args(argv)

    if args.body_file and str(args.body_file) == "-" and args.html and str(args.html) == "-":
        parser.error("--body-file 和 --html 不能同时从标准输入读")

    cfg = MailConfig.load(args.config)
    if args.sender:
        cfg.sender = args.sender
    if args.brand:
        cfg.brand = args.brand
    if args.to:
        cfg.to = split_addresses(args.to)

    text = args.body if args.body is not None else DEFAULT_BODY
    if args.body_file:
        text = _read_text(args.body_file)
    html = _read_text(args.html) if args.html else ""

    now = datetime.now(CST)
    message = build_message(cfg, args, text, html, now)

    if args.dry_run:
        outdir = args.out or PREVIEW_DIR
        eml, page = save_preview(message, outdir, "mail-{:%Y%m%d-%H%M%S}".format(now))
        print("标题：{}".format(message["Subject"]))
        print("收件人：{}".format(", ".join(cfg.to) or "-"))
        print("附件：{}".format("、".join(Path(p).name for p in args.attach) or "无"))
        print("已生成：\n  {}\n  {}".format(eml, page))
        return 0

    problems = cfg.problems()
    if problems:
        print("还差这些才能发信：{}".format("、".join(problems)), file=sys.stderr)
        print("先用 --dry-run 看预览，或参考 README 里的环境变量", file=sys.stderr)
        return 2

    try:
        send_message(message, cfg, debug=args.debug)
    except smtplib.SMTPRecipientsRefused as exc:
        print("收件人被服务端拒收：", file=sys.stderr)
        for addr, (code, detail) in exc.recipients.items():
            print("  {} → {} {}".format(
                addr, code, detail.decode("utf-8", "replace")), file=sys.stderr)
        return 1
    except (smtplib.SMTPException, OSError) as exc:
        print("投递失败：{}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return 1

    print("已发送：{} → {}（{} KiB 正文，{} 个附件）".format(
        message["Subject"], ", ".join(cfg.to),
        len(bytes(message)) // 1024, len(args.attach)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
