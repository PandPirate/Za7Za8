# -*- coding: utf-8 -*-
"""插件目录。

新插件 = 在这个目录里新建一个模块，暴露一个 PLUGIN 对象：

    from ..core import Section

    class Hello:
        name = "hello"
        title = "示例"
        description = "一句话说明这个插件发什么"
        enabled_by_default = False

        def build(self, ctx):
            return Section(key="hello", title="示例", html="<p>...</p>", text="...")

    PLUGIN = Hello()

框架启动时自动扫描（pkgutil），不需要注册表，也不需要改任何别处。
"""
