# -*- coding: utf-8 -*-
"""可插拔的摘要邮件框架。

    mailer/
      core.py       插件协议、内容区块、MIME 组装、投递
      template.py   HTML / 纯文本版面
      config.py     SMTP 配置（环境变量或 JSON）
      cli.py        命令行入口
      plugins/      插件目录：每个模块放一个 PLUGIN 对象即可被自动发现
"""

__version__ = "1.0.0"
