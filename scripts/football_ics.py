#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把英超 / 欧冠赛程做成 .ics 日历，可直接导入 Outlook、Google 日历、手机日历。

时间一律按北京时间（Asia/Shanghai）录入，生成时换算成 UTC 写进 DTSTART/DTEND，
这样导入任何客户端都不会因为时区设置而偏一小时。纯标准库，Python 3.8+。

用法：
    python scripts/football_ics.py                  # 全部，输出到 downloads/
    python scripts/football_ics.py --only 欧冠       # 只导出欧冠
    python scripts/football_ics.py --no-alarms      # 不要赛前提醒
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

PRODID = "-//Za7Za8//football-ics 1.0//CN"
TZ = dt.timezone(dt.timedelta(hours=8))  # 录入用的北京时间
UTC = dt.timezone.utc
MATCH_MINUTES = 120

# (赛事, 轮次, 北京时间, 主队, 客队, 场地)
MATCHES = [
    # ---- 英超 2026/27 第 11 轮 ----
    ("英超", "第11轮", "2026-11-21 20:30", "曼城", "富勒姆", "伊蒂哈德球场"),
    ("英超", "第11轮", "2026-11-21 23:00", "伯恩茅斯", "诺丁汉森林", "维塔利蒂球场"),
    ("英超", "第11轮", "2026-11-21 23:00", "阿斯顿维拉", "桑德兰", "维拉公园"),
    ("英超", "第11轮", "2026-11-21 23:00", "切尔西", "利兹联", "斯坦福桥"),
    ("英超", "第11轮", "2026-11-21 23:00", "考文垂", "水晶宫", "考文垂建筑协会球场"),
    ("英超", "第11轮", "2026-11-21 23:00", "热刺", "伊普斯维奇", "托特纳姆热刺球场"),
    ("英超", "第11轮", "2026-11-22 01:30", "纽卡斯尔", "阿森纳", "圣詹姆斯公园"),
    ("英超", "第11轮", "2026-11-22 22:00", "赫尔城", "布莱顿", "MKM 球场"),
    ("英超", "第11轮", "2026-11-23 00:30", "利物浦", "曼联", "安菲尔德"),
    ("英超", "第11轮", "2026-11-24 04:00", "布伦特福德", "埃弗顿", "Gtech 社区球场"),

    # ---- 欧冠 2026/27 联赛阶段 第 5 轮 ----
    ("欧冠", "联赛阶段第5轮", "2026-11-25 01:45", "博德闪耀", "林茨", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-25 01:45", "加拉塔萨雷", "阿斯顿维拉", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-25 04:00", "阿森纳", "多特蒙德", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-25 04:00", "科莫", "雅典AEK", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-25 04:00", "费耶诺德", "波尔图", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-25 04:00", "曼城", "那不勒斯", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-25 04:00", "莱比锡红牛", "朗斯", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-25 04:00", "皇家马德里", "埃因霍温", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-25 04:00", "布拉迪斯拉发", "皇家贝蒂斯", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-26 01:45", "萨巴赫", "巴塞罗那", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-26 01:45", "布拉格斯拉维亚", "比利亚雷亚尔", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-26 04:00", "马德里竞技", "维京", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-26 04:00", "布鲁日", "利物浦", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-26 04:00", "国际米兰", "斯图加特", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-26 04:00", "里尔", "拜仁慕尼黑", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-26 04:00", "巴黎圣日耳曼", "罗马", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-26 04:00", "顿涅茨克矿工", "费内巴切", ""),
    ("欧冠", "联赛阶段第5轮", "2026-11-26 04:00", "葡萄牙体育", "曼联", ""),

    # ---- 欧冠 联赛阶段 第 6 轮 ----
    ("欧冠", "联赛阶段第6轮", "2026-12-09 01:45", "维京", "费耶诺德", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-09 01:45", "比利亚雷亚尔", "萨巴赫", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-09 04:00", "雅典AEK", "加拉塔萨雷", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-09 04:00", "罗马", "葡萄牙体育", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-09 04:00", "阿斯顿维拉", "巴黎圣日耳曼", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-09 04:00", "巴塞罗那", "曼城", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-09 04:00", "拜仁慕尼黑", "布拉格斯拉维亚", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-09 04:00", "曼联", "莱比锡红牛", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-09 04:00", "那不勒斯", "布鲁日", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-10 01:45", "皇家贝蒂斯", "科莫", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-10 01:45", "布拉迪斯拉发", "顿涅茨克矿工", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-10 04:00", "阿森纳", "皇家马德里", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-10 04:00", "多特蒙德", "国际米兰", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-10 04:00", "林茨", "费内巴切", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-10 04:00", "朗斯", "博德闪耀", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-10 04:00", "利物浦", "波尔图", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-10 04:00", "埃因霍温", "马德里竞技", ""),
    ("欧冠", "联赛阶段第6轮", "2026-12-10 04:00", "斯图加特", "里尔", ""),

    # ---- 欧冠 联赛阶段 第 7 轮 ----
    ("欧冠", "联赛阶段第7轮", "2027-01-20 01:45", "博德闪耀", "马德里竞技", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-20 01:45", "加拉塔萨雷", "费耶诺德", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-20 04:00", "雅典AEK", "罗马", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-20 04:00", "阿斯顿维拉", "多特蒙德", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-20 04:00", "波尔图", "布拉格斯拉维亚", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-20 04:00", "国际米兰", "利物浦", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-20 04:00", "里尔", "布拉迪斯拉发", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-20 04:00", "皇家马德里", "林茨", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-20 04:00", "斯图加特", "布鲁日", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-21 01:45", "费内巴切", "比利亚雷亚尔", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-21 01:45", "萨巴赫", "那不勒斯", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-21 04:00", "皇家贝蒂斯", "阿森纳", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-21 04:00", "科莫", "巴黎圣日耳曼", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-21 04:00", "朗斯", "曼城", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-21 04:00", "曼联", "拜仁慕尼黑", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-21 04:00", "莱比锡红牛", "顿涅茨克矿工", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-21 04:00", "葡萄牙体育", "巴塞罗那", ""),
    ("欧冠", "联赛阶段第7轮", "2027-01-21 04:00", "维京", "埃因霍温", ""),

    # ---- 欧冠 联赛阶段 第 8 轮（18 场同时开球）----
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "阿森纳", "萨巴赫", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "罗马", "里尔", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "马德里竞技", "费内巴切", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "巴塞罗那", "科莫", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "拜仁慕尼黑", "皇家贝蒂斯", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "布鲁日", "博德闪耀", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "多特蒙德", "雅典AEK", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "费耶诺德", "莱比锡红牛", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "林茨", "波尔图", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "利物浦", "朗斯", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "曼城", "葡萄牙体育", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "那不勒斯", "维京", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "巴黎圣日耳曼", "加拉塔萨雷", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "埃因霍温", "斯图加特", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "顿涅茨克矿工", "皇家马德里", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "布拉格斯拉维亚", "阿斯顿维拉", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "布拉迪斯拉发", "国际米兰", ""),
    ("欧冠", "联赛阶段第8轮", "2027-01-28 04:00", "比利亚雷亚尔", "曼联", ""),
]


# ---------------------------------------------------------------- 文本工具


def esc(text):
    """RFC 5545 TEXT 转义。"""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold(line, limit=75):
    """按字节折行，不劈开 UTF-8 字符。"""
    out, cur, size, avail = [], "", 0, limit
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > avail:
            out.append(cur)
            cur, size, avail = ch, n, limit - 1
        else:
            cur += ch
            size += n
    out.append(cur)
    return [out[0]] + [" " + s for s in out[1:]]


def parse_hkt(text):
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def weekday_cn(day):
    return "周" + "一二三四五六日"[day.weekday()]


# ---------------------------------------------------------------- 自检


def check(rows):
    """按轮次查完整性：每轮场数对、同轮没有球队重复出场、每队总出场数一致。

    抄漏一场或者主客队抄反，都会在这里露馅。
    """
    problems = []
    by_comp = {}
    for comp, round_, when, home, away, venue in rows:
        by_comp.setdefault(comp, {}).setdefault(round_, []).append((home, away, when))

    for comp, rounds in by_comp.items():
        per_round = 18 if comp == "欧冠" else 10
        counts = {}
        for round_, items in rounds.items():
            if len(items) != per_round:
                problems.append("{} {} 有 {} 场，应为 {} 场".format(comp, round_, len(items), per_round))
            seen = set()
            for home, away, _ in items:
                for team in (home, away):
                    if team in seen:
                        problems.append("{} {} 里 {} 出现了两次".format(comp, round_, team))
                    seen.add(team)
                    counts[team] = counts.get(team, 0) + 1
        for team, n in sorted(counts.items()):
            if n != len(rounds):
                problems.append("{}：{} 出场 {} 次，应为 {} 次".format(comp, team, n, len(rounds)))
    return problems


# ---------------------------------------------------------------- 生成


def build(rows, alarms, only=None):
    stamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    events = []
    for index, (comp, round_, when, home, away, venue) in enumerate(rows, 1):
        if only and comp != only:
            continue
        start = parse_hkt(when).astimezone(UTC)
        end = start + dt.timedelta(minutes=MATCH_MINUTES)
        local = parse_hkt(when)
        desc = [
            comp + round_,
            "北京时间 {} {:%m月%d日} {}".format(weekday_cn(local), local, local.strftime("%H:%M")),
        ]
        if venue:
            desc.append("场地：{}".format(venue))
        lines = [
            "BEGIN:VEVENT",
            "UID:{:%Y%m%dT%H%M}-{:03d}-{}@sports.za7za8".format(
                start, index, "pl" if comp == "英超" else "ucl"
            ),
            "DTSTAMP:" + stamp,
            "DTSTART:" + start.strftime("%Y%m%dT%H%M%SZ"),
            "DTEND:" + end.strftime("%Y%m%dT%H%M%SZ"),
            "SUMMARY:" + esc("{} {} vs {}".format(comp, home, away)),
            "DESCRIPTION:" + esc("\n".join(desc)),
            "CATEGORIES:" + esc(comp),
            "TRANSP:OPAQUE",
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY",
            "CLASS:PUBLIC",
            "STATUS:CONFIRMED",
            "SEQUENCE:0",
        ]
        if venue:
            lines.insert(8, "LOCATION:" + esc(venue))
        if alarms:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                "DESCRIPTION:" + esc("{} 分钟后开球：{} vs {}".format(30, home, away)),
                "TRIGGER:-PT30M",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
        events.append({"start": start, "lines": lines, "comp": comp, "text": "{} vs {}".format(home, away)})

    events.sort(key=lambda e: e["start"])
    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:" + PRODID,
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + esc("英超·欧冠 2026/27"),
        "X-WR-CALDESC:" + esc("英超与欧冠赛程，时间均为北京时间。"),
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    lines = header[:]
    for event in events:
        lines.extend(event["lines"])
    lines.append("END:VCALENDAR")

    folded = []
    for line in lines:
        folded.extend(fold(line))
    return "\r\n".join(folded) + "\r\n", events


def validate(text):
    problems = []
    if text.startswith("\ufeff"):
        problems.append("带 BOM")
    if not text.endswith("\r\n"):
        problems.append("结尾不是 CRLF")
    raw = text.split("\r\n")[:-1]
    for i, line in enumerate(raw, 1):
        if len(line.encode("utf-8")) > 75:
            problems.append("第 {} 行超过 75 字节".format(i))
        if "\n" in line or "\r" in line:
            problems.append("第 {} 行有裸换行".format(i))
    unfolded, lines = None, []
    for line in raw:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    if lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        problems.append("VCALENDAR 包裹不完整")

    events, cur, skip, uids = [], None, False, set()
    for line in lines:
        if line == "BEGIN:VALARM":
            skip = True
        elif line == "END:VALARM":
            skip = False
        elif line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            events.append(cur)
            cur = None
        elif cur is not None and not skip and ":" in line:
            k, v = line.split(":", 1)
            cur[k.split(";")[0]] = v
    for i, ev in enumerate(events, 1):
        for key in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY"):
            if key not in ev:
                problems.append("第 {} 个事件缺 {}".format(i, key))
        if ev.get("UID") in uids:
            problems.append("UID 重复：" + str(ev.get("UID")))
        uids.add(ev.get("UID"))
        try:
            s = dt.datetime.strptime(ev["DTSTART"], "%Y%m%dT%H%M%SZ")
            e = dt.datetime.strptime(ev["DTEND"], "%Y%m%dT%H%M%SZ")
            if e <= s:
                problems.append("第 {} 个事件结束时间不晚于开始".format(i))
        except (KeyError, ValueError):
            problems.append("第 {} 个事件时间格式不对".format(i))
    return problems, len(events)


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成英超/欧冠赛程 .ics")
    parser.add_argument("-o", "--out", type=Path, help="输出路径")
    parser.add_argument("--only", choices=["英超", "欧冠"], help="只导出某一项赛事")
    parser.add_argument("--no-alarms", action="store_true", help="不加赛前 30 分钟提醒")
    args = parser.parse_args(argv)

    problems = check(MATCHES)
    if problems:
        print("数据自检失败：")
        for p in problems:
            print("  - " + p)
        return 1

    text, events = build(MATCHES, not args.no_alarms, args.only)
    problems, count = validate(text)

    name = "2026-27 英超欧冠赛程" if not args.only else "2026-27 {}".format(args.only)
    out = args.out or (Path(__file__).resolve().parent.parent / "downloads" / (name + ".ics"))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(str(out), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)

    by_comp = {}
    for ev in events:
        by_comp.setdefault(ev["comp"], []).append(ev)
    print("共 {} 场：".format(len(events)))
    for comp, items in by_comp.items():
        print("  {:2d} 场  {}  {} → {}".format(
            len(items),
            comp,
            min(e["start"] for e in items).astimezone(TZ).strftime("%Y-%m-%d"),
            max(e["start"] for e in items).astimezone(TZ).strftime("%Y-%m-%d"),
        ))
    print("已写入 {}".format(out))
    if problems:
        print("校验未通过：")
        for p in problems:
            print("  - " + p)
        return 1
    print("校验通过：{} 条事件，时间合法、UID 唯一、行宽合规。".format(count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
