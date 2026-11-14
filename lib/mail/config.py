# -*- coding: utf-8 -*-
"""SMTP 配置：优先读环境变量，也可以给一个 JSON 文件。

环境变量（都可以用 --config 的 JSON 覆盖）：

    MAILER_SMTP_HOST   邮件服务器，如 smtp.qq.com
    MAILER_SMTP_PORT   端口，默认 587（starttls）
    MAILER_SMTP_TLS    starttls | ssl | none ，默认 starttls
    MAILER_SMTP_USER   登录账号
    MAILER_SMTP_PASS   密码 / 授权码
    MAILER_FROM        发件人地址，默认同 USER
    MAILER_TO          收件人，多个用逗号或分号分隔
    MAILER_BRAND       发件人显示名，默认「每日摘要」

密码建议只放环境变量，不要写进仓库里的 JSON。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

ENV_KEYS = {
    "host": "MAILER_SMTP_HOST",
    "port": "MAILER_SMTP_PORT",
    "tls": "MAILER_SMTP_TLS",
    "user": "MAILER_SMTP_USER",
    "password": "MAILER_SMTP_PASS",
    "sender": "MAILER_FROM",
    "to": "MAILER_TO",
    "brand": "MAILER_BRAND",
}


def split_addresses(value: str) -> List[str]:
    """逗号或分号分隔的地址串 → 列表。"""
    if not value:
        return []
    for sep in (";", "，"):
        value = value.replace(sep, ",")
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class MailConfig:
    host: str = ""
    port: int = 587
    tls: str = "starttls"
    user: str = ""
    password: str = ""
    sender: str = ""
    to: List[str] = field(default_factory=list)
    brand: str = "每日摘要"

    @classmethod
    def load(cls, path: Optional[Path] = None, environ: Optional[Dict[str, str]] = None) -> "MailConfig":
        env = environ if environ is not None else os.environ
        cfg = cls()
        for field_name, env_name in ENV_KEYS.items():
            raw = env.get(env_name)
            if not raw:
                continue
            if field_name == "to":
                cfg.to = split_addresses(raw)
            elif field_name == "port":
                try:
                    cfg.port = int(raw)
                except ValueError:
                    raise SystemExit("{} 不是合法端口：{}".format(env_name, raw))
            else:
                setattr(cfg, field_name, raw.strip())

        if path:
            with open(str(path), encoding="utf-8") as fh:
                data = json.load(fh)
            for field_name, value in data.items():
                if not hasattr(cfg, field_name):
                    continue
                if field_name == "to":
                    cfg.to = split_addresses(value) if isinstance(value, str) else list(value)
                elif field_name == "port":
                    cfg.port = int(value)
                else:
                    setattr(cfg, field_name, value)

        if not cfg.sender and cfg.user:
            cfg.sender = cfg.user
        if cfg.tls not in ("starttls", "ssl", "none"):
            raise SystemExit("MAILER_SMTP_TLS 只能是 starttls / ssl / none，收到：{}".format(cfg.tls))
        return cfg

    def problems(self, to: Optional[List[str]] = None) -> List[str]:
        """真正发送前检查必填项。"""
        missing = []
        if not self.host:
            missing.append("SMTP 服务器（MAILER_SMTP_HOST）")
        if not self.sender:
            missing.append("发件人（MAILER_FROM 或 MAILER_SMTP_USER）")
        if not (to or self.to):
            missing.append("收件人（MAILER_TO 或 --to）")
        if self.user and not self.password:
            missing.append("密码（MAILER_SMTP_PASS）")
        return missing

    def masked(self) -> str:
        """打日志用，别把密码漏出去。"""
        return "{}:{} tls={} user={} to={}".format(
            self.host or "-", self.port, self.tls, self.user or "-", ",".join(self.to) or "-"
        )
