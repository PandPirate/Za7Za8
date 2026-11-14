#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摘要邮件入口。

    python scripts/send_digest.py --list
    python scripts/send_digest.py --dry-run
    python scripts/send_digest.py --to you@example.com

实现都在 `lib/mail/` 里，`lib/graph_calendar.py` 负责写日历；这个文件只做两件事：
把 `lib/` 加进 `sys.path`，然后转交 `mail.cli.main`。
"""

import sys

import _bootstrap  # noqa: F401  （必须在 import mail 之前，它负责把 lib/ 放进 sys.path）

from mail.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
