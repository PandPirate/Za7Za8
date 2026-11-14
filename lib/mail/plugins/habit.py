# -*- coding: utf-8 -*-
"""习惯打卡插件：每天一封今日待办，回复邮件即可完成/说明原因。

数据层在 `lib/habit_tracker.py`，回信解析与摘要回复在 `scripts/habit_replies.py`；
这个插件只负责每日内容。
"""

from __future__ import annotations

from typing import Optional

import habit_tracker

from ..core import Section


class HabitPlugin:
    name = "habit"
    title = habit_tracker.SUBJECT
    description = "21天习惯养成：今日待办 + 邮件回复完成/说明原因"
    enabled_by_default = False

    def build(self, ctx) -> Optional[Section]:
        day = ctx.now.date().isoformat()
        try:
            rows = habit_tracker.ensure_day(day)
        except Exception as exc:
            ctx.warn("习惯打卡数据库打开失败：{}".format(exc))
            return None
        html, text = habit_tracker.render_daily(day, rows)
        return Section(
            key=self.name,
            title=self.title,
            html=html,
            text=text,
            priority=30,
        )


PLUGIN = HabitPlugin()
