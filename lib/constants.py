# -*- coding: utf-8 -*-
"""全包共用的小常量。

`CST` / `UTC` 放这里而不是 `core.py`：日历层（`graph_calendar`）也要用，让它为了
一个时区去 import 邮件核心，依赖方向是反的。
"""

from datetime import timedelta, timezone

CST = timezone(timedelta(hours=8))     # 北京时间
UTC = timezone.utc
