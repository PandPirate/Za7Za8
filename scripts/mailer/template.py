# -*- coding: utf-8 -*-
"""版面：把若干区块拼成一封能看的邮件。

HTML 部分全用内联样式 + table 布局，Outlook 桌面版（不认 <style>、flex、grid）
也能正常显示；纯文本版本给不看 HTML 的客户端和终端预览用。
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

WEEKDAY_CN = "一二三四五六日"


def weekday(day: datetime) -> str:
    return "周" + WEEKDAY_CN[day.weekday()]


def render_text(sections: Sequence, now: datetime, brand: str) -> str:
    rule = "─" * 46
    parts = [
        brand,
        "{}年{}月{}日 {} {:%H:%M}（北京时间）".format(
            now.year, now.month, now.day, weekday(now), now
        ),
        rule,
    ]
    for section in sections:
        parts.append("[{}]".format(section.title))
        parts.append(section.text.strip())
        parts.append("")
    parts.append(rule)
    parts.append("本邮件由邮件框架生成 · {}".format("、".join(s.title for s in sections)))
    return "\n".join(parts).strip() + "\n"


def render_document(sections: Sequence, now: datetime, brand: str) -> "tuple[str, str]":
    """返回 (html, text)。"""
    subtitle = "{}年{}月{}日 · {}".format(now.year, now.month, now.day, weekday(now))

    blocks = []
    for section in sections:
        blocks.append(
            """
      <tr>
        <td style="padding:26px 28px 6px 28px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
            <tr>
              <td width="4" style="background:#2563eb;border-radius:2px;font-size:0;line-height:0;">&nbsp;</td>
              <td style="padding-left:10px;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                         font-size:16px;font-weight:700;color:#0f172a;letter-spacing:.02em;">{title}</td>
            </tr>
          </table>
        </td>
      </tr>
      <tr>
        <td style="padding:12px 28px 8px 28px;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                   font-size:14px;line-height:1.7;color:#1e293b;">
{body}
        </td>
      </tr>""".format(
                title=_escape(section.title), body=section.html.strip()
            )
        )

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{brand}</title>
</head>
<body style="margin:0;padding:0;background:#eef2f7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f7;border-collapse:collapse;">
<tr>
<td align="center" style="padding:28px 12px;">
  <table role="presentation" width="640" cellpadding="0" cellspacing="0"
         style="width:100%;max-width:640px;background:#ffffff;border-radius:14px;border-collapse:separate;
                border:1px solid #e2e8f0;overflow:hidden;">
    <tr>
      <td style="background:#0f172a;padding:24px 28px;">
        <div style="font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;font-size:20px;
                    font-weight:700;color:#f8fafc;letter-spacing:.04em;">{brand}</div>
        <div style="font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;font-size:13px;
                    color:#94a3b8;margin-top:6px;">{subtitle}</div>
      </td>
    </tr>{blocks}
    <tr>
      <td style="padding:20px 28px 24px 28px;border-top:1px solid #eef2f7;">
        <div style="font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;font-size:12px;
                    line-height:1.7;color:#94a3b8;">
          由邮件框架生成 · {titles}<br>
          时间均为北京时间；数据以官方/赛会公布为准，仅供个人参考。
        </div>
      </td>
    </tr>
  </table>
</td>
</tr>
</table>
</body>
</html>
""".format(
        brand=_escape(brand),
        subtitle=_escape(subtitle),
        blocks="".join(blocks),
        titles=_escape("、".join(s.title for s in sections)),
    )
    return html, render_text(sections, now, brand)


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
