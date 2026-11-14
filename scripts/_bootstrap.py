# -*- coding: utf-8 -*-
"""把 `<仓库>/lib` 放进 `sys.path`。

入口脚本（都在 `scripts/`）第一行 `import _bootstrap` 即可：

    import _bootstrap  # noqa: F401
    from mail.cli import main

这样「lib 在哪」只写一次，不用每个入口各写一遍 `sys.path.insert`——那种写法在
搬目录时必然漏改。
"""

import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"

if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))
