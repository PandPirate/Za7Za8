#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Graph 日历写入自检：建测试日历 → 写事件 → 验证幂等与去重 → 清理。

    python scripts/graph_calendar_test.py --dry-run   # 只读演练：不往日历写事件
    python scripts/graph_calendar_test.py             # 真写一遍，跑完自动清理
    python scripts/graph_calendar_test.py --keep      # 写完保留，方便去 Outlook 里看

前提：`MAILER_GRAPH_CLIENT_ID` 已设置，且跑过
`python scripts/send_digest.py --graph-login`（token 在 `state/.graph_token.json`）。

安全边界：只动独立的「Za7Za8 测试」日历，事件标题一律以 `[测试]` 开头，
清理时也只删这些——不会碰你已有的日程。
"""

import argparse
import sys
from datetime import datetime, timedelta

import _bootstrap  # noqa: F401  （把 lib/ 放进 sys.path）

import graph_calendar as gc
from constants import CST, UTC

PREFIX = "[测试] "
DEFAULT_CALENDAR = "Za7Za8 测试"


def build_events(now: datetime, offset_days: int):
    """3 条测试事件：北京时间 20:00 开始、各 2 小时，立在之后连续三天。"""
    day0 = (now + timedelta(days=offset_days)).replace(
        hour=20, minute=0, second=0, microsecond=0)
    events = []
    for index, name in enumerate(("甲队", "乙队", "丙队")):
        start = day0 + timedelta(days=index)
        events.append(gc.CalendarEvent(
            subject="{}英超 测试 {} vs 测试 对手".format(PREFIX, name),
            start=start,
            end=start + timedelta(hours=2),
            location="测试场地 {}".format(index + 1),
            body="由 graph_calendar_test.py 写入",
            reminder_minutes=15,
        ))
    return events


def _stamp(value) -> str:
    return str(value or "")[:16]


def main(argv=None) -> int:
    # Windows 重定向到管道/文件时默认 GBK，统一成 UTF-8
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="写一遍 Outlook 日历并验证结果（默认自动清理测试事件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  %(prog)s --dry-run        # 只读演练，不写事件\n"
        "  %(prog)s                  # 真写 + 验证 + 清理\n"
        "  %(prog)s --keep           # 写完保留\n",
    )
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR, metavar="NAME",
                        help="写入哪个日历，默认「{}」".format(DEFAULT_CALENDAR))
    parser.add_argument("--dry-run", action="store_true",
                        help="只读演练：不往日历写事件（仍会读日历、可能续期 token）")
    parser.add_argument("--keep", action="store_true", help="写完不清理，方便去 Outlook 里看")
    parser.add_argument("--offset-days", type=int, default=3, metavar="N",
                        help="测试事件放在几天后（默认 3）")
    parser.add_argument("--proxy", metavar="URL", help="如 http://127.0.0.1:7890")
    args = parser.parse_args(argv)

    if args.proxy:
        gc.set_proxy(args.proxy)

    creds = gc.load_creds()
    if creds is None:
        print("先设置环境变量 MAILER_GRAPH_CLIENT_ID（Azure 应用的客户端 ID）", file=sys.stderr)
        print("注册步骤见 docs/outlook-graph-calendar.md", file=sys.stderr)
        return 2
    if not gc.has_token(creds):
        print("还没有登录过：先跑 python scripts/send_digest.py --graph-login", file=sys.stderr)
        return 2

    events = build_events(datetime.now(CST), args.offset_days)
    window_start = events[0].start - timedelta(hours=1)
    window_end = events[-1].end + timedelta(hours=1)
    failures = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        print("  [{}] {}{}".format("OK" if ok else "失败", name,
                                  "  " + detail if detail else ""))
        if not ok:
            failures.append(name)
        return ok

    print("日历：{}".format(args.calendar))
    print("token：{}".format(creds.token_path))

    try:
        token = gc.access_token(creds)
        me = gc.me(token)
        check("认证", bool(token),
              "账号 {}".format(me.get("userPrincipalName") or me.get("displayName") or "?"))
    except gc.GraphError as exc:
        check("认证", False, str(exc))
        return 1

    calendar_id = gc.find_calendar(token, args.calendar)

    if args.dry_run:
        # 日历不存在不算失败：真跑时会自动建
        if calendar_id:
            print("  [OK] 日历已存在  {}".format(calendar_id))
        else:
            print("  [新建] 日历还不存在，真跑时会自动创建「{}」".format(args.calendar))
        planned = gc.sync(events, args.calendar, backend="graph", plan_only=True)
        check("演练", planned.created == len(events), planned.line())
        print("\n演练结束：没有往日历写任何事件（去掉 --dry-run 就是真写）。")
        return 1 if failures else 0

    try:
        # 先清掉上次中断留下的事件，保证「首次写入」从干净状态开始
        if calendar_id:
            for item in gc.list_events(calendar_id, window_start, window_end, creds):
                if str(item.get("subject", "")).startswith(PREFIX):
                    gc.delete_event(item["id"], creds)

        first = gc.sync(events, args.calendar, backend="graph")
        check("首次写入", first.created == len(events) and first.updated == 0, first.line())

        # 故意塞一条同标题同时间的副本，模拟历史上重复写入留下的多余日程
        calendar_id = gc.ensure_calendar(token, args.calendar)
        gc.create_event(calendar_id, events[0], creds)

        second = gc.sync(events, args.calendar, backend="graph")
        check("重复写入不产副本", second.created == 0 and second.updated == len(events),
              second.line())
        check("清理已有重复", second.deleted == 1, second.line())

        found = gc.list_events(calendar_id, window_start, window_end, creds)
        mine = [e for e in found if str(e.get("subject", "")).startswith(PREFIX)]
        check("事件都在", len(mine) == len(events), "{} 条".format(len(mine)))

        expected = {(e.subject, e.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"))
                    for e in events}
        actual = {(e.get("subject"), _stamp((e.get("start") or {}).get("dateTime")))
                  for e in mine}
        check("标题与开始时间对得上", expected == actual,
              "" if expected == actual else "差异 {}".format(expected ^ actual))
    except gc.GraphError as exc:
        check("写入", False, str(exc))
    finally:
        if args.keep:
            print("  [跳过] 清理（--keep）：去 Outlook 的「{}」日历看".format(args.calendar))
        elif calendar_id:
            try:
                left = [e for e in gc.list_events(calendar_id, window_start, window_end, creds)
                        if str(e.get("subject", "")).startswith(PREFIX)]
                for item in left:
                    gc.delete_event(item["id"], creds)
                after = [e for e in gc.list_events(calendar_id, window_start, window_end, creds)
                         if str(e.get("subject", "")).startswith(PREFIX)]
                check("清理", not after, "剩 {} 条".format(len(after)))
            except gc.GraphError as exc:
                check("清理", False, str(exc))
                print("  手动删「{}」里以「{}」开头的事件即可".format(args.calendar, PREFIX))

    print()
    if failures:
        print("失败项：{}".format("、".join(failures)))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
