# -*- coding: utf-8 -*-
"""摘要邮件框架：**插件出内容，框架管版式和投递**。

    mail/
      core.py        插件协议、区块收集、MIME 组装、投递
      template.py    HTML / 纯文本版面
      config.py      SMTP 配置（环境变量或 JSON）
      cli.py         命令行入口（参数解析与编排）
      plugins/       插件目录：每个模块放一个 PLUGIN 对象即可被自动发现

这个包只管“发邮件”。写日历在 `lib/graph_calendar.py`：`cli` 只借用它的
`graph_ready()` 打一句提示，真正写日历的是插件。入口脚本在 `scripts/` 下
（`send_digest.py`），包本身没有 `__main__`。
"""

__version__ = "1.0.0"
