# -*- coding: utf-8 -*-
"""示例插件：用来照着写自己的插件。

它默认不上场（enabled_by_default = False），想跑它：

    python scripts/send_digest.py --plugin example --dry-run

要点就三条：暴露 PLUGIN 对象、实现 build(ctx)、返回 Section 或 None。
"""

from __future__ import annotations

from ..core import Section


class ExamplePlugin:
    name = "example"
    title = "示例区块"
    description = "演示插件契约：返回标题、HTML 和纯文本"
    enabled_by_default = False

    def build(self, ctx):
        html = (
            '<p style="margin:0 0 8px;">这个区块来自 <code style="background:#f1f5f9;'
            'padding:2px 6px;border-radius:4px;">mail.plugins.example</code>。</p>'
            '<p style="margin:0;color:#64748b;font-size:13px;">'
            "生成时间：{:%Y-%m-%d %H:%M}（北京时间）</p>".format(ctx.now)
        )
        text = "这个区块来自 mail.plugins.example。\n生成时间：{:%Y-%m-%d %H:%M}".format(ctx.now)
        return Section(key=self.name, title=self.title, html=html, text=text, priority=900)


PLUGIN = ExamplePlugin()
