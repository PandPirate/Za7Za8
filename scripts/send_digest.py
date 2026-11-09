#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摘要邮件入口。

    python scripts/send_digest.py --list
    python scripts/send_digest.py --dry-run
    python scripts/send_digest.py --to you@example.com

真正的实现在 scripts/mailer/ 里；这个文件只是把 scripts/ 放进 sys.path 再转交。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mailer.cli import main  # noqa: E402  （要在 sys.path 处理之后导入）

if __name__ == "__main__":
    sys.exit(main())
