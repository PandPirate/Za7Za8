# -*- coding: utf-8 -*-
"""命令行入口。

    python scripts/send_digest.py --list                 # 看有哪些插件
    python scripts/send_digest.py --dry-run              # 只生成预览，不发信
    python scripts/send_digest.py --to me@example.com    # 真发
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import MailConfig, split_addresses
from .core import Context, build_message, collect, load_plugins, save_preview, send_message
from constants import CST
from graph_calendar import graph_ready
from paths import PREVIEW_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="send_digest.py",
        description="可插拔的摘要邮件：选插件 → 生成 HTML/纯文本 → 预览或投递",
    )
    parser.add_argument("--list", action="store_true", help="列出可用插件后退出")
    parser.add_argument("--dry-run", action="store_true", help="只生成 .eml/.html 预览，不发信")
    parser.add_argument(
        "--plugin",
        action="append",
        metavar="NAME",
        help="只跑指定插件，可以重复；不写就跑所有默认启用的插件",
    )
    parser.add_argument("--to", metavar="ADDR", help="收件人，多个用逗号分隔，覆盖配置")
    parser.add_argument("--subject", metavar="TEXT", help="覆盖自动生成的标题")
    parser.add_argument("--brand", metavar="NAME", help="覆盖邮件页头（发件人显示名另由 from_name / 配置 brand 决定）")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="没有内容时返回 0（休赛期 / 定时任务用）",
    )
    parser.add_argument("--config", type=Path, metavar="JSON", help="SMTP 配置文件")
    parser.add_argument("--out", type=Path, metavar="DIR", help="预览输出目录")
    parser.add_argument(
        "--calendar",
        choices=["auto", "graph", "none"],
        default="auto",
        help="赛程写进哪里：auto（配了 Graph 就写，否则只发信）/ graph（Microsoft Graph）/ none",
    )
    parser.add_argument("--calendar-name", metavar="NAME", help="日历文件夹名，默认由插件决定")
    parser.add_argument(
        "--apply-calendar",
        action="store_true",
        help="配合 --dry-run：预览的同时也真的写日历（否则只演练）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    parser.add_argument("--graph-login", action="store_true", help="设备代码登录 Microsoft Graph（一次即可）")
    parser.add_argument("--graph-logout", action="store_true", help="删掉本地缓存的 Graph token")
    parser.add_argument("--graph-whoami", action="store_true", help="当前 token 属于哪个账号（日历就写到它名下）")
    return parser


def _graph_auth(args) -> int:
    """--graph-login / --graph-logout / --graph-whoami：设备代码登录，token 缓存在 state/.graph_token.json。"""
    from graph_calendar import (
        GraphError,
        cache_info,
        device_login,
        load_creds,
        token_claims,
        whoami,
    )

    creds = load_creds()
    if creds is None:
        print("先设置环境变量 MAILER_GRAPH_CLIENT_ID（Azure 应用的客户端 ID）", file=sys.stderr)
        print("注册步骤见 docs/outlook-graph-calendar.md", file=sys.stderr)
        return 2

    if args.graph_whoami:
        try:
            me = whoami(creds)
        except GraphError as exc:
            print("查询失败：{}".format(exc), file=sys.stderr)
            info = cache_info(creds)
            if info:
                print(
                    "token 缓存：{} | client_id={} | 租户={} | 剩余 {} 秒 | refresh={}".format(
                        info["path"],
                        (info["client_id"] or "?")[:8] + "…",
                        info["tenant"] or "?",
                        info["left"],
                        "有" if info["has_refresh"] else "无",
                    ),
                    file=sys.stderr,
                )
            claims = token_claims(creds)
            if claims:
                print(
                    "token 声明：aud={} | scp={} | iss={}".format(
                        claims.get("aud") or "-", claims.get("scp") or "-", claims.get("iss") or "-"
                    ),
                    file=sys.stderr,
                )
            return 1
        print("当前 token 属于：{}".format(me.get("displayName") or "?"))
        print("账号：{}".format(me.get("userPrincipalName") or me.get("mail") or "?"))
        print("日历会写到这个账号的邮箱里")
        return 0

    if args.graph_logout:
        if creds.token_path.exists():
            creds.token_path.unlink()
            print("已删除 {}".format(creds.token_path))
        else:
            print("本来就没有缓存的 token")
        return 0

    try:
        device_login(creds, echo=print)
    except GraphError as exc:
        print("登录失败：{}".format(exc), file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    # Windows 重定向到管道/文件时默认 GBK：中文能过但 ─ ✓ 之类会炸，统一成 UTF-8
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    # 登录 / 登出 / 查账号不依赖插件，别让一个坏插件挡住 token 维护
    if args.graph_login or args.graph_logout or args.graph_whoami:
        return _graph_auth(args)

    plugins = load_plugins()

    if args.list:
        print("可用插件 {} 个：".format(len(plugins)))
        for plugin in plugins.values():
            flag = "默认启用" if getattr(plugin, "enabled_by_default", False) else "需 --plugin 指定"
            print("  {:<16} {:<8} {}".format(plugin.name, flag, plugin.description))
        return 0

    if args.plugin:
        unknown = [name for name in args.plugin if name not in plugins]
        if unknown:
            print("没有这些插件：{}".format("、".join(unknown)), file=sys.stderr)
            print("可用：{}".format("、".join(plugins)), file=sys.stderr)
            return 2
        chosen = [plugins[name] for name in args.plugin]
    else:
        chosen = [p for p in plugins.values() if getattr(p, "enabled_by_default", False)]

    if not chosen:
        print("没有启用的插件", file=sys.stderr)
        return 2

    cfg = MailConfig.load(args.config)
    if args.to:
        cfg.to = split_addresses(args.to)
    if args.brand:
        cfg.brand = args.brand

    ctx = Context(
        now=datetime.now(CST),
        calendar_backend=args.calendar,
        calendar_name=args.calendar_name or "",
        # 预览默认只演练，加 --apply-calendar 才真的写日历
        calendar_plan_only=bool(args.dry_run and not args.apply_calendar),
    )

    if args.calendar == "auto" and not graph_ready():
        print(
            "提示：还没配置 Graph 凭据（MAILER_GRAPH_CLIENT_ID + --graph-login），"
            "这封邮件只发内容，不会写日历",
            file=sys.stderr,
        )
    sections = collect(ctx, chosen)
    if not sections:
        if args.allow_empty:
            print("没有内容，已跳过")
            return 0
        print("插件都没有产出内容，不生成邮件", file=sys.stderr)
        return 1

    message = build_message(cfg, sections, ctx.now, args.subject or None)

    if args.dry_run:
        outdir = args.out or PREVIEW_DIR
        stem = "digest-{:%Y%m%d}".format(ctx.now)
        # 只跑一个插件时把名字写进文件名，避免先后两次预览互相覆盖
        if len(chosen) == 1:
            stem += "-" + chosen[0].name
        eml, page = save_preview(message, outdir, stem)
        print("标题：{}".format(message["Subject"]))
        print("区块：{}".format("、".join("{}({})".format(s.title, s.key) for s in sections)))
        print("已生成：\n  {}\n  {}".format(eml, page))
        print("-" * 46)
        plain = next(
            (part.get_content() for part in message.walk() if part.get_content_type() == "text/plain"),
            "",
        )
        print(plain.strip())
        return 0

    problems = cfg.problems()
    if problems:
        print("还差这些才能发信：{}".format("、".join(problems)), file=sys.stderr)
        print("先用 --dry-run 看预览，或参考 README 里的环境变量", file=sys.stderr)
        return 2

    send_message(message, cfg)
    print("已发送：{} → {}".format(message["Subject"], ", ".join(cfg.to)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
