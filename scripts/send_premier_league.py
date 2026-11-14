#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""英超专用入口：发一封英超快报，并把下一轮赛程写进 Outlook 日历。

    python scripts/send_premier_league.py --dry-run     # 预览 + 演练日历
    python scripts/send_premier_league.py               # 真发信 + 真写日历
    python scripts/send_premier_league.py --to me@example.com

等价于：

    python scripts/send_digest.py --plugin premier_league --calendar graph

其余参数（--to / --subject / --config / --graph-login …）原样转交；
自己写了 --plugin 或 --calendar，就按自己的来。

写日历按「标题 + 开始时间」去重：同批重复只留一条，日历里已有的副本只保留
一条，重复跑不会越写越多。
"""

import sys

import _bootstrap  # noqa: F401  （把 lib/ 放进 sys.path）

from mail.cli import main as digest_main  # noqa: E402

PLUGIN = "premier_league"
CALENDAR = "graph"


def _given(flag: str, argv) -> bool:
    """`--x value` 和 `--x=value` 两种写法都算显式给出。"""
    return any(arg == flag or arg.startswith(flag + "=") for arg in argv)


def _with_defaults(argv):
    defaults = []
    if not _given("--plugin", argv):
        defaults += ["--plugin", PLUGIN]
    if not _given("--calendar", argv):
        defaults += ["--calendar", CALENDAR]
    return defaults + argv


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    return digest_main(_with_defaults(args))


if __name__ == "__main__":
    sys.exit(main())
