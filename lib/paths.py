# -*- coding: utf-8 -*-
"""仓库内的固定路径。

所有落盘位置都从这里取——**不要在别的文件里自己数 `parents`**。这套代码搬过几次
目录，`parents[1]` / `parent.parent` / `parents[2]` 三种写法各错过一处，症状分别是
「入口一跑就 ModuleNotFoundError」「token 缓存写到 scripts/downloads/」「.ics 生成到
错误目录」。

本文件在 `lib/` 下，所以仓库根是 `parents[1]`。
"""

from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent              # lib/
REPO_ROOT = LIB_DIR.parent                             # 仓库根

# 大文件产物：pikpak 下载、邮件预览、节假日 .ics
DOWNLOADS_DIR = REPO_ROOT / "downloads"

# 机器状态 / 凭据：token、缓存。不入库（见 .gitignore）
STATE_DIR = REPO_ROOT / "state"

# `--dry-run` 生成的 .eml / .html
PREVIEW_DIR = DOWNLOADS_DIR / "mail_preview"
