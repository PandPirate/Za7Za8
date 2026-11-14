#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日习惯打卡邮件：一天只发一封新邮件。

    python scripts/send_habits.py --dry-run     # 预览，不发送、不占当天的名额
    python scripts/send_habits.py               # 真发；同一天重复运行会跳过
    python scripts/send_habits.py --to me@example.com

发出去之后，当天的回复都在这封邮件里；回信由 scripts/habit_replies.py 处理。
"""

import sys
from datetime import datetime

import _bootstrap  # noqa: F401

import habit_tracker
from constants import CST
from mail.cli import main as digest_main
from paths import STATE_DIR

PLUGIN = "habit"
TO = "glycyrrhizin@outlook.com"


def _given(flag: str, argv) -> bool:
    return any(arg == flag or arg.startswith(flag + "=") for arg in argv)


def _with_defaults(argv, day: str):
    defaults = []
    if not _given("--plugin", argv):
        defaults += ["--plugin", PLUGIN]
    if not _given("--calendar", argv):
        defaults += ["--calendar", "none"]
    if not _given("--to", argv):
        defaults += ["--to", TO]
    if not _given("--subject", argv):
        defaults += ["--subject", "{} · {}".format(habit_tracker.SUBJECT, day)]
    if not _given("--config", argv):
        config = STATE_DIR / "mail.json"
        if config.exists():
            defaults += ["--config", str(config)]
    if not _given("--brand", argv):
        defaults += ["--brand", habit_tracker.SUBJECT]
    return defaults + argv


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    day = datetime.now(CST).date().isoformat()
    if any(
        arg in ("--graph-login", "--graph-logout", "--graph-whoami", "--list")
        for arg in args
    ):
        return digest_main(_with_defaults(args, day))

    dry_run = "--dry-run" in args
    if not dry_run and habit_tracker.was_sent(day):
        print("{} 已经发过习惯打卡邮件，跳过".format(day))
        return 0

    status = digest_main(_with_defaults(args, day))
    if status == 0 and not dry_run:
        habit_tracker.mark_sent(day)
    return status


if __name__ == "__main__":
    sys.exit(main())
