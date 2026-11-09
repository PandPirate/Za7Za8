#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把国务院办公厅公布的法定节假日安排生成 .ics 日历文件。

生成的文件可以直接导入 Outlook / Google 日历 / Apple 日历：每个放假日一条
全天事件，每个调休上班日也有一条（带前一天 9 点的提醒），这样月视图里一眼
就能看出哪天不上班、哪个周末要补班。

数据要按年手工维护：每年 11 月前后国务院办公厅发通知，把新一年的 blocks /
workdays 照着通知原文抄进 YEAR_DATA 即可。纯标准库，Python 3.8+。

用法：
    python scripts/cn_holiday_ics.py                    # 生成 2027 年，输出到 downloads/
    python scripts/cn_holiday_ics.py -o 2027.ics        # 指定输出文件
    python scripts/cn_holiday_ics.py --holidays-only    # 只要放假日，不要调休上班
    python scripts/cn_holiday_ics.py --no-alarms        # 不要调休日的提醒
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

PRODID = "-//Za7Za8//cn-holiday-ics 1.0//CN"
CAL_HOST = "cn-holiday.za7za8"
WEEKDAY_CN = "一二三四五六日"

# 2027 年安排：国办发明电〔2026〕10 号（2026-11-05）
YEAR_DATA = {
    2027: {
        "notice": "国办发明电〔2026〕10号",
        "url": "https://www.gov.cn/zhengce/content/202611/content_7085101.htm",
        "blocks": [
            {
                "name": "元旦",
                "start": "01-01",
                "end": "01-03",
                "note": "1月1日（周五）至3日（周日）放假，共3天。",
                "lunar": ["公历新年"],
            },
            {
                "name": "春节",
                "start": "02-05",
                "end": "02-13",
                "note": "2月5日（农历除夕、周五）至13日（农历正月初八、周六）放假调休，共9天。"
                "1月31日（周日）、2月14日（周日）上班。",
                # 除夕 + 正月初一 ~ 初八
                "lunar": ["农历除夕"] + ["农历正月初" + c for c in "一二三四五六七八"],
                "day_names": ["除夕"],
            },
            {
                "name": "清明节",
                "start": "04-03",
                "end": "04-05",
                "note": "4月3日（周六）至5日（周一）放假，共3天。",
                "lunar": ["二十四节气·清明（4月5日）"],
            },
            {
                "name": "劳动节",
                "start": "05-01",
                "end": "05-05",
                "note": "5月1日（周六）至5日（周三）放假调休，共5天。5月8日（周六）上班。",
                "lunar": ["国际劳动节"],
            },
            {
                "name": "端午节",
                "start": "06-09",
                "end": "06-09",
                "note": "6月9日（周三）放假，共1天。",
                "lunar": ["农历五月初五"],
            },
            {
                "name": "中秋节",
                "start": "09-15",
                "end": "09-15",
                "note": "9月15日（周三）放假，共1天。",
                "lunar": ["农历八月十五"],
            },
            {
                "name": "国庆节",
                "start": "10-01",
                "end": "10-07",
                "note": "10月1日（周五）至7日（周四）放假调休，共7天。"
                "9月26日（周日）、10月9日（周六）上班。",
                "lunar": ["中华人民共和国国庆日"],
            },
        ],
        # 周末调休上班日：(日期, 所属假期)
        "workdays": [
            ("01-31", "春节"),
            ("02-14", "春节"),
            ("05-08", "劳动节"),
            ("09-26", "国庆节"),
            ("10-09", "国庆节"),
        ],
        # 通知里明写的日期 → 星期（0=周一）。生成前先自检，防止抄错日期。
        "weekday_checks": [
            ("01-01", 4), ("01-03", 6),
            ("02-05", 4), ("02-13", 5), ("01-31", 6), ("02-14", 6),
            ("04-03", 5), ("04-05", 0),
            ("05-01", 5), ("05-05", 2), ("05-08", 5),
            ("06-09", 2),
            ("09-15", 2),
            ("09-26", 6), ("10-01", 4), ("10-07", 3), ("10-09", 5),
        ],
    }
}


# ---------------------------------------------------------------- 文本工具


def esc(text):
    """按 RFC 5545 转义 TEXT 值：反斜杠、分号、逗号、换行。"""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold(line, limit=75):
    """把一行折成不超过 limit 字节的多行，不会把一个 UTF-8 字符劈开。"""
    out, cur, size = [], "", 0
    avail = limit
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > avail:
            out.append(cur)
            cur, size = ch, n
            avail = limit - 1  # 续行以空格开头，占掉一个字节
        else:
            cur += ch
            size += n
    out.append(cur)
    return [out[0]] + [" " + s for s in out[1:]]


def weekday_cn(day):
    return "周" + WEEKDAY_CN[day.weekday()]


def md_cn(day):
    return "{}月{}日".format(day.month, day.day)


def parse_md(year, text):
    month, day = (int(x) for x in text.split("-"))
    return dt.date(year, month, day)


def iter_days(start, end):
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


# ---------------------------------------------------------------- 自检


def check_weekdays(year, data):
    """通知里写明的「日期（星期）」要和真实日历对得上，抄错日期这里就会炸。"""
    bad = []
    for text, expected in data["weekday_checks"]:
        day = parse_md(year, text)
        if day.weekday() != expected:
            bad.append(
                "{} 应为{}，实际是{}".format(day, "周" + WEEKDAY_CN[expected], weekday_cn(day))
            )
    if bad:
        raise SystemExit("日期自检失败：\n  " + "\n  ".join(bad))


# ---------------------------------------------------------------- 生成事件


def build_events(year, data, alarms):
    notice, url = data["notice"], data["url"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    events = []

    for block in data["blocks"]:
        start = parse_md(year, block["start"])
        end = parse_md(year, block["end"])
        days = list(iter_days(start, end))
        total = len(days)
        day_names = block.get("day_names") or []

        for i, day in enumerate(days):
            lunar = block["lunar"][i] if i < len(block["lunar"]) else ""
            title = day_names[i] if i < len(day_names) else block["name"]

            desc = ["{}年{}".format(year, block["name"])]
            if total > 1:
                desc[0] += "假期（共{}天，第{}天）".format(total, i + 1)
            else:
                desc[0] += "（共1天）"
            if lunar:
                desc.append(lunar)
            desc.append(block["note"])
            desc.append("{} · {}".format(notice, url))

            events.append(
                {
                    "date": day,
                    "busy": False,
                    "lines": [
                        "BEGIN:VEVENT",
                        "UID:{}-{:%Y%m%d}-holiday@{}".format(year, day, CAL_HOST),
                        "DTSTAMP:" + stamp,
                        "DTSTART;VALUE=DATE:{:%Y%m%d}".format(day),
                        "DTEND;VALUE=DATE:{:%Y%m%d}".format(day + dt.timedelta(days=1)),
                        "SUMMARY:" + esc(title),
                        "DESCRIPTION:" + esc("\n".join(desc)),
                        "CATEGORIES:法定节假日",
                        "TRANSP:TRANSPARENT",
                        "X-MICROSOFT-CDO-ALLDAYEVENT:TRUE",
                        "X-MICROSOFT-CDO-BUSYSTATUS:FREE",
                        "CLASS:PUBLIC",
                        "STATUS:CONFIRMED",
                        "SEQUENCE:0",
                        "END:VEVENT",
                    ],
                }
            )

    for text, holiday in data["workdays"]:
        day = parse_md(year, text)
        desc = [
            "{}年{}调休上班日（{}）".format(year, holiday, weekday_cn(day)),
            "{}（{}）上班。".format(md_cn(day), weekday_cn(day)),
            "{} · {}".format(notice, url),
        ]
        lines = [
            "BEGIN:VEVENT",
            "UID:{}-{:%Y%m%d}-workday@{}".format(year, day, CAL_HOST),
            "DTSTAMP:" + stamp,
            "DTSTART;VALUE=DATE:{:%Y%m%d}".format(day),
            "DTEND;VALUE=DATE:{:%Y%m%d}".format(day + dt.timedelta(days=1)),
            "SUMMARY:" + esc("调休上班"),
            "DESCRIPTION:" + esc("\n".join(desc)),
            "CATEGORIES:调休上班",
            "TRANSP:OPAQUE",
            "X-MICROSOFT-CDO-ALLDAYEVENT:TRUE",
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY",
            "CLASS:PUBLIC",
            "STATUS:CONFIRMED",
            "SEQUENCE:0",
        ]
        if alarms:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                "DESCRIPTION:" + esc("明天是调休上班日"),
                # 全天事件从 00:00 起算，提前 15 小时 = 前一天 09:00
                "TRIGGER:-PT15H",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
        events.append({"date": day, "busy": True, "lines": lines})

    events.sort(key=lambda e: (e["date"], e["busy"]))
    return events


def make_ics(year, data, alarms, holidays_only):
    events = build_events(year, data, alarms)
    if holidays_only:
        events = [e for e in events if not e["busy"]]

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:" + PRODID,
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + esc("中国法定节假日 {}".format(year)),
        "X-WR-CALDESC:" + esc("{} 年放假与调休安排，{}。".format(year, data["notice"])),
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for event in events:
        lines.extend(event["lines"])
    lines.append("END:VCALENDAR")

    folded = []
    for line in lines:
        folded.extend(fold(line))
    return "\r\n".join(folded) + "\r\n", events


# ---------------------------------------------------------------- 校验


def validate(text):
    """按 RFC 5545 的骨架检查一遍，返回 (问题列表, 统计)。"""
    problems = []
    if text.startswith("\ufeff"):
        problems.append("文件带 BOM")
    if not text.endswith("\r\n"):
        problems.append("文件没有以 CRLF 结尾")

    raw = text.split("\r\n")[:-1]
    for i, line in enumerate(raw, 1):
        if "\n" in line or "\r" in line:
            problems.append("第 {} 行混入了裸换行".format(i))
        if len(line.encode("utf-8")) > 75:
            problems.append("第 {} 行超过 75 字节".format(i))

    # 展开折行
    lines = []
    for line in raw:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    if not lines or lines[0] != "BEGIN:VCALENDAR":
        problems.append("首行不是 BEGIN:VCALENDAR")
    if not lines or lines[-1] != "END:VCALENDAR":
        problems.append("末行不是 END:VCALENDAR")

    depth = 0
    for line in lines:
        if line == "BEGIN:VCALENDAR":
            depth += 1
        elif line == "END:VCALENDAR":
            depth -= 1

    # 逐个收集事件的属性（VALARM 里的字段不算事件的）
    events = []
    current = None
    skip = False
    for line in lines:
        if line == "BEGIN:VALARM":
            skip = True
        elif line == "END:VALARM":
            skip = False
        elif line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            events.append(current)
            current = None
        elif current is not None and not skip and ":" in line:
            name, value = line.split(":", 1)
            current[name.split(";")[0]] = (name, value)

    if depth != 0:
        problems.append("BEGIN/END:VCALENDAR 不配对")

    uids = set()
    days = 0
    for idx, ev in enumerate(events, 1):
        for key in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY"):
            if key not in ev:
                problems.append("第 {} 个事件缺少 {}".format(idx, key))
        if "UID" in ev:
            uid = ev["UID"][1]
            if uid in uids:
                problems.append("UID 重复：" + uid)
            uids.add(uid)
        if "DTSTART" in ev and "DTEND" in ev:
            try:
                start = dt.datetime.strptime(ev["DTSTART"][1], "%Y%m%d").date()
                end = dt.datetime.strptime(ev["DTEND"][1], "%Y%m%d").date()
                if end <= start:
                    problems.append("第 {} 个事件 DTEND 不晚于 DTSTART".format(idx))
                days += (end - start).days
            except ValueError:
                problems.append("第 {} 个事件日期格式不对".format(idx))

    stats = {"events": len(events), "days": days}
    return problems, stats


# ---------------------------------------------------------------- 入口


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成中国法定节假日 .ics 日历")
    parser.add_argument("--year", type=int, default=2027, help="年份，默认 2027")
    parser.add_argument("-o", "--out", type=Path, help="输出文件路径")
    parser.add_argument("--holidays-only", action="store_true", help="不生成调休上班日")
    parser.add_argument("--no-alarms", action="store_true", help="调休上班日不加提醒")
    args = parser.parse_args(argv)

    data = YEAR_DATA.get(args.year)
    if data is None:
        raise SystemExit(
            "没有 {} 年的数据，请先按国务院办公厅通知把 blocks / workdays 补进 YEAR_DATA".format(
                args.year
            )
        )

    check_weekdays(args.year, data)
    text, events = make_ics(args.year, data, not args.no_alarms, args.holidays_only)
    problems, stats = validate(text)

    out = args.out or (
        Path(__file__).resolve().parent.parent / "downloads" / "{}中国节假日.ics".format(args.year)
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(str(out), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)

    holidays = sum(1 for e in events if not e["busy"])
    print(
        "{} 年：{} 个放假日 + {} 个调休上班日（共 {} 条事件）".format(
            args.year, holidays, len(events) - holidays, len(events)
        )
    )
    for event in events:
        day = event["date"]
        summary = next(l.split(":", 1)[1] for l in event["lines"] if l.startswith("SUMMARY:"))
        print("  {:%Y-%m-%d}（{}） {}".format(day, weekday_cn(day), summary))
    print("已写入 {}".format(out))

    if problems:
        print("\n校验未通过：")
        for item in problems:
            print("  - " + item)
        return 1
    print("校验通过：%d 条事件，UID 无重复，日期合法，行宽合规。" % stats["events"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
