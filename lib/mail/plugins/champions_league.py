# -*- coding: utf-8 -*-
"""欧冠插件：近期赛果、联赛阶段积分榜、下一批赛程和看点，并把赛程写进 Outlook 日历。

数据来自 ESPN 的公开接口（uefa.champions scoreboard / standings）。欧冠不是全年都有：
窗口内既没有刚结束的比赛、也没有即将开始的比赛时返回 None，这封邮件就不带欧冠区块。

ESPN 的 scoreboard 不接受日期区间，但接受**整月**（dates=YYYYMM），
所以按「上个月 / 这个月 / 下个月」各取一次，再按时间窗口筛。
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import escape
from typing import List, Optional

from constants import CST, UTC
from graph_calendar import CalendarEvent, CalendarResult, sync

from ..core import Section

SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard?dates={month}"
)
STANDINGS = "https://site.api.espn.com/apis/v2/sports/soccer/uefa.champions/standings"

# 最近多久之内结束的比赛算「赛果」、多久之内的比赛算「赛程」
RECENT_DAYS = 14
UPCOMING_DAYS = 30

STAGE_CN = {
    "league-phase": "联赛阶段",
    "knockout-round-playoffs": "淘汰赛附加赛",
    "round-of-16": "1/8 决赛",
    "quarterfinals": "1/4 决赛",
    "semifinals": "半决赛",
    "final": "决赛",
}

# ESPN 的英文队名 → 中文，没收录的原样保留
TEAM_CN = {
    "Paris Saint-Germain": "巴黎圣日耳曼",
    "Real Madrid": "皇家马德里",
    "Arsenal": "阿森纳",
    "Como": "科莫",
    "Barcelona": "巴塞罗那",
    "Aston Villa": "阿斯顿维拉",
    "Manchester City": "曼城",
    "Liverpool": "利物浦",
    "Club Brugge": "布鲁日",
    "Sporting CP": "葡萄牙体育",
    "Real Betis": "皇家贝蒂斯",
    "Borussia Dortmund": "多特蒙德",
    "Bayern Munich": "拜仁慕尼黑",
    "VfB Stuttgart": "斯图加特",
    "Internazionale": "国际米兰",
    "LASK Linz": "林茨",
    "Manchester United": "曼联",
    "Galatasaray": "加拉塔萨雷",
    "Fenerbahce": "费内巴切",
    "AS Roma": "罗马",
    "Shakhtar Donetsk": "顿涅茨克矿工",
    "Viking FK": "维京",
    "Atlético Madrid": "马德里竞技",
    "PSV Eindhoven": "埃因霍温",
    "Bodo/Glimt": "博德闪耀",
    "FC Porto": "波尔图",
    "Napoli": "那不勒斯",
    "Villarreal": "比利亚雷亚尔",
    "AEK Athens": "雅典 AEK",
    "RB Leipzig": "莱比锡",
    "Sabah FK": "萨巴赫",
    "Feyenoord Rotterdam": "费耶诺德",
    "Lens": "朗斯",
    "Slavia Prague": "布拉格斯拉维亚",
    "Lille": "里尔",
    "Slovan Bratislava": "布拉迪斯拉发斯拉夫人",
    # 以后可能遇到的常见队
    "Chelsea": "切尔西",
    "Tottenham Hotspur": "热刺",
    "Newcastle United": "纽卡斯尔",
    "Juventus": "尤文图斯",
    "AC Milan": "AC米兰",
    "Atalanta": "亚特兰大",
    "Benfica": "本菲卡",
    "Ajax": "阿贾克斯",
    "Celtic": "凯尔特人",
    "Olympique Marseille": "马赛",
    "Monaco": "摩纳哥",
    "Bayer Leverkusen": "勒沃库森",
    "Eintracht Frankfurt": "法兰克福",
    "Sevilla": "塞维利亚",
    "Valencia": "瓦伦西亚",
    "Lyon": "里昂",
    "FC Copenhagen": "哥本哈根",
    "Red Star Belgrade": "贝尔格莱德红星",
    "Dinamo Zagreb": "萨格勒布迪纳摩",
    "Salzburg": "萨尔茨堡",
    "Young Boys": "伯尔尼年轻人",
    "Sparta Prague": "布拉格斯巴达",
    "Sturm Graz": "格拉茨风暴",
    "Union Saint-Gilloise": "圣吉尔联",
    "Bologna": "博洛尼亚",
    "Girona": "赫罗纳",
    "Brest": "布雷斯特",
}

FOCUS_TEAMS = {
    "皇家马德里",
    "巴塞罗那",
    "拜仁慕尼黑",
    "曼城",
    "利物浦",
    "阿森纳",
    "巴黎圣日耳曼",
    "国际米兰",
    "AC米兰",
    "尤文图斯",
    "切尔西",
    "曼联",
}

_LABEL = "font-size:13px;color:#64748b;letter-spacing:.04em;margin:22px 0 8px;"
_LINE = "border-bottom:1px solid #f1f5f9;"


@dataclass
class Match:
    kickoff: dt.datetime
    home: str
    away: str
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    state: str = "pre"
    venue: str = ""
    stage: str = ""

    @property
    def local(self) -> dt.datetime:
        return self.kickoff.astimezone(CST)

    @property
    def finished(self) -> bool:
        return (
            self.state == "post"
            and self.home_score is not None
            and self.away_score is not None
        )

    @property
    def goals(self) -> int:
        if self.home_score is None or self.away_score is None:
            return 0
        return self.home_score + self.away_score

    def when(self) -> str:
        return self.local.strftime("%m-%d %H:%M")

    def label(self) -> str:
        return "{} vs {}".format(self.home, self.away)


@dataclass
class Standing:
    """积分榜的一行。ESPN 的 pointsFor / pointsAgainst 指的是进球数，不是积分。"""

    rank: int
    team: str
    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    points: int

    @property
    def goal_diff(self) -> int:
        return self.goals_for - self.goals_against

    @property
    def record(self) -> str:
        return "{}-{}-{}".format(self.won, self.drawn, self.lost)


# ---------------------------------------------------------------- 取数


def _score(side, finished: bool) -> Optional[int]:
    raw = side.get("score")
    if not finished or raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _team_name(side) -> str:
    raw = ((side or {}).get("team") or {}).get("displayName") or ""
    return TEAM_CN.get(raw, raw)


def parse_espn(payload) -> List[Match]:
    matches: List[Match] = []
    for event in payload.get("events", []):
        competition = (event.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in competition.get("competitors", [])}
        home, away = sides.get("home"), sides.get("away")
        if not home or not away:
            continue
        state = competition.get("status", {}).get("type", {}).get("state", "pre")
        finished = state == "post"
        matches.append(
            Match(
                kickoff=dt.datetime.fromisoformat(event["date"].replace("Z", "+00:00")),
                home=_team_name(home),
                away=_team_name(away),
                home_score=_score(home, finished),
                away_score=_score(away, finished),
                state=state,
                venue=(competition.get("venue") or {}).get("fullName", ""),
                stage=(event.get("season") or {}).get("slug") or "",
            )
        )
    return matches


def _month_keys(day: dt.date) -> List[str]:
    """上个月 / 这个月 / 下个月；跨年由 date 运算处理。"""
    first = day.replace(day=1)
    return [
        (first - dt.timedelta(days=1)).strftime("%Y%m"),
        first.strftime("%Y%m"),
        (first + dt.timedelta(days=32)).strftime("%Y%m"),
    ]


def _safe_json(ctx, url):
    """单月取失败不算致命——少一个月数据总比整封邮件没有强。"""
    try:
        return ctx.http_json(url)
    except Exception as exc:
        ctx.warn("取欧冠 {} 的数据失败：{}".format(url.rsplit("=", 1)[-1], exc))
        return None


def _fetch_live(ctx) -> List[Match]:
    today = ctx.now.astimezone(UTC).date()
    months = _month_keys(today)
    with ThreadPoolExecutor(max_workers=3) as pool:
        payloads = pool.map(
            lambda month: _safe_json(ctx, SCOREBOARD.format(month=month)), months
        )
        matches: List[Match] = []
        seen = set()
        for payload in payloads:
            if payload is None:
                continue
            for match in parse_espn(payload):
                key = (match.kickoff, match.home, match.away)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(match)
    return matches


def load_matches(ctx):
    """窗口内的欧冠比赛；取不到就返回空列表，由插件决定不产出这一块。"""
    try:
        return _fetch_live(ctx), "ESPN 实时比分"
    except Exception as exc:
        ctx.warn("欧冠数据拉取失败：{}".format(exc))
        return [], "无数据"


def _stat(stats, name: str, default: int = 0) -> int:
    for item in stats:
        if item.get("name") == name:
            try:
                return int(float(item.get("value")))
            except (TypeError, ValueError):
                return default
    return default


def parse_standings(payload) -> List[Standing]:
    rows: List[Standing] = []
    for child in payload.get("children") or []:
        entries = (child.get("standings") or {}).get("entries") or []
        for entry in entries:
            stats = entry.get("stats") or []
            if not stats:
                continue
            rows.append(
                Standing(
                    rank=_stat(stats, "rank"),
                    team=_team_name(entry),
                    played=_stat(stats, "gamesPlayed"),
                    won=_stat(stats, "wins"),
                    drawn=_stat(stats, "ties"),
                    lost=_stat(stats, "losses"),
                    # ESPN 的 pointsFor / pointsAgainst 是进球数，积分在 points
                    goals_for=_stat(stats, "pointsFor"),
                    goals_against=_stat(stats, "pointsAgainst"),
                    points=_stat(stats, "points"),
                )
            )
    rows.sort(key=lambda s: s.rank or 99)
    return rows


def load_standings(ctx) -> List[Standing]:
    """联赛阶段积分榜；取不到就返回空列表，不拖累整封邮件。"""
    try:
        return parse_standings(ctx.http_json(STANDINGS))
    except Exception as exc:
        ctx.warn("欧冠积分榜拉取失败：{}".format(exc))
        return []


def _stage_name(matches: List[Match]) -> str:
    for match in matches:
        if match.stage:
            return STAGE_CN.get(match.stage, match.stage)
    return ""


def _matchday(matches: List[Match]):
    """同一周的比赛算一个比赛日；欧冠一轮通常落在周二/周三。"""
    if not matches:
        return []
    week = matches[0].local.isocalendar()[:2]
    return [m for m in matches if m.local.isocalendar()[:2] == week]


def _calendar_name(now: dt.datetime) -> str:
    start = now.year if now.month >= 7 else now.year - 1
    return "欧冠赛程 {}-{:02d}".format(start, (start + 1) % 100)


# ---------------------------------------------------------------- 排版


def _pad(text: str, width: int) -> str:
    """按显示宽度补空格：中文占两格，纯文本在等宽字体下才能对齐。"""
    cells = sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)
    return text + " " * max(0, width - cells)


def _label(text: str) -> str:
    return '<div style="{}">{}</div>'.format(_LABEL, escape(text))


def _results_table(matches: List[Match]) -> str:
    rows = []
    for match in matches:
        home = escape(match.home)
        away = escape(match.away)
        if (match.home_score or 0) > (match.away_score or 0):
            home = '<span style="font-weight:700;">{}</span>'.format(home)
        if (match.away_score or 0) > (match.home_score or 0):
            away = '<span style="font-weight:700;">{}</span>'.format(away)
        rows.append(
            "<tr>"
            f'<td style="padding:9px 10px 9px 0;{_LINE}color:#94a3b8;font-size:13px;'
            f'white-space:nowrap;vertical-align:top;">{match.when()}</td>'
            f'<td style="padding:9px 0;{_LINE}font-size:16px;color:#0f172a;line-height:1.6;">'
            f'{home} <span style="font-weight:700;white-space:nowrap;">'
            f"{match.home_score} - {match.away_score}</span> {away}</td>"
            "</tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;">' + "".join(rows) + "</table>"
    )


def _group_by_day(matches: List[Match]):
    buckets = {}
    for match in matches:
        buckets.setdefault(match.local.date(), []).append(match)
    return [
        (day, sorted(items, key=lambda m: m.kickoff))
        for day, items in sorted(buckets.items())
    ]


def _weekday(day) -> str:
    return "周" + "一二三四五六日"[day.weekday()]


def _agenda(matches: List[Match]) -> str:
    """按天排的赛程：日期单独一行，下面列当天场次，窄屏也不会挤成两列。"""
    blocks = []
    for day, items in _group_by_day(matches):
        lines = "".join(
            '<div style="margin:2px 0;">'
            f'<span style="color:#94a3b8;font-size:14px;">{match.local:%H:%M}</span> '
            f'{escape(match.home)} <span style="color:#cbd5e1;">vs</span> {escape(match.away)}'
            "</div>"
            for match in items
        )
        blocks.append(
            '<div style="margin:0 0 14px;">'
            f'<div style="font-size:14px;font-weight:700;color:#0f172a;margin-bottom:4px;">'
            f"{day.month}/{day.day} {_weekday(day)}</div>"
            f'<div style="font-size:16px;line-height:1.9;color:#0f172a;">{lines}</div>'
            "</div>"
        )
    return "".join(blocks)


def _standings_table(rows: List[Standing]) -> str:
    """三列：排名 / 球队（战绩小字）/ 积分，窄屏也能一屏放下。"""
    head = (
        "<tr>"
        f'<td style="padding:0 8px 6px 0;{_LINE}font-size:12px;color:#94a3b8;"></td>'
        f'<td style="padding:0 8px 6px 0;{_LINE}font-size:12px;color:#94a3b8;">球队</td>'
        f'<td style="padding:0 0 6px;{_LINE}font-size:12px;color:#94a3b8;text-align:right;">积分</td>'
        "</tr>"
    )
    body = []
    for row in rows:
        meta = "{} 场 · {} · 净胜 {:+d}".format(row.played, row.record, row.goal_diff)
        body.append(
            "<tr>"
            f'<td style="padding:8px 8px 8px 0;{_LINE}font-size:13px;color:#94a3b8;'
            f'vertical-align:top;">{row.rank}</td>'
            f'<td style="padding:8px 8px 8px 0;{_LINE}font-size:16px;color:#0f172a;'
            f'line-height:1.4;">{escape(row.team)}'
            f'<div style="font-size:13px;color:#94a3b8;font-weight:400;margin-top:2px;">'
            f"{meta}</div></td>"
            f'<td style="padding:8px 0;{_LINE}font-size:18px;font-weight:700;color:#0f172a;'
            f'text-align:right;vertical-align:top;">{row.points}</td>'
            "</tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;">' + head + "".join(body) + "</table>"
    )


def _bullets(items: List[str]) -> str:
    lis = "".join(
        '<li style="margin:6px 0;color:#334155;">' + escape(item) + "</li>" for item in items
    )
    return (
        '<ul style="margin:2px 0 0;padding-left:20px;font-size:16px;line-height:1.7;">'
        + lis
        + "</ul>"
    )


def _calendar_events(matches: List[Match]) -> List[CalendarEvent]:
    events = []
    for match in matches:
        start = match.kickoff.astimezone(UTC)
        events.append(
            CalendarEvent(
                subject="欧冠 {} vs {}".format(match.home, match.away),
                start=start,
                end=start + dt.timedelta(hours=2),
                location=match.venue,
                body="欧冠 · 北京时间 {}".format(match.when()),
            )
        )
    return events


# ---------------------------------------------------------------- 插件


class ChampionsLeaguePlugin:
    name = "champions_league"
    title = "欧冠快报"
    description = "欧冠近期赛果、联赛阶段积分榜、下一批赛程、看点，并把赛程写进 Outlook 日历"
    enabled_by_default = True

    def build(self, ctx) -> Optional[Section]:
        matches, source = load_matches(ctx)
        if not matches:
            return None

        now = ctx.now
        recent_start = now - dt.timedelta(days=RECENT_DAYS)
        upcoming_end = now + dt.timedelta(days=UPCOMING_DAYS)
        finished = _matchday(
            sorted(
                (m for m in matches if m.finished and m.kickoff >= recent_start),
                key=lambda m: m.kickoff,
                reverse=True,
            )
        )
        fixtures = _matchday(
            sorted(
                (
                    m
                    for m in matches
                    if not m.finished
                    and m.kickoff >= now - dt.timedelta(hours=3)
                    and m.kickoff <= upcoming_end
                ),
                key=lambda m: m.kickoff,
            )
        )

        # 欧冠不是全年都有：窗口内一场都没有，就当这期没有欧冠
        if not finished and not fixtures:
            return None

        result_stage = _stage_name(finished)
        fixture_stage = _stage_name(fixtures)
        active_stage = fixture_stage or result_stage
        standings = load_standings(ctx) if active_stage in ("", "联赛阶段") else []

        html: List[str] = []
        text: List[str] = []

        if finished:
            head = "赛果 · {}".format(result_stage) if result_stage else "赛果"
            html += [_label(head), _results_table(finished)]
            text += [head] + [
                "· {}  {} {}-{} {}".format(
                    m.when(), m.home, m.home_score, m.away_score, m.away
                )
                for m in finished
            ]

        if standings:
            html += [_label("积分榜"), _standings_table(standings)]
            text += ["", "积分榜"] + [
                "{:>2}. {} {:>2}场 {:>5} {:>+3} {:>2}分".format(
                    row.rank,
                    _pad(row.team, 12),
                    row.played,
                    row.record,
                    row.goal_diff,
                    row.points,
                )
                for row in standings
            ]

        if fixtures:
            first, last = fixtures[0].local, fixtures[-1].local
            span = first.strftime("%m/%d")
            if first.date() != last.date():
                span += "–" + last.strftime("%m/%d")
            head = "赛程 · {}".format(fixture_stage) if fixture_stage else "赛程"
            html += [_label("{} · {}（北京时间）".format(head, span)), _agenda(fixtures)]
            text += ["", "{} · {}（北京时间）".format(head, span)]
            for day, items in _group_by_day(fixtures):
                text.append("{}/{} {}".format(day.month, day.day, _weekday(day)))
                text += ["  {}  {}".format(m.local.strftime("%H:%M"), m.label()) for m in items]

        insights = self._insights(finished, fixtures)
        if insights:
            html += [_label("看点"), _bullets(insights)]
            text += ["", "看点"] + ["· " + item for item in insights]

        events = _calendar_events(fixtures) if fixtures else []
        result = self._sync_calendar(ctx, events)

        notes_html: List[str] = []
        notes_text: List[str] = []
        if result is not None and result.line():
            notes_html.append(escape(result.line()))
            notes_text.append(result.line())
        notes_html.append("数据：" + escape(source))
        notes_text.append("数据：" + source)
        html.append(
            '<div style="margin-top:20px;font-size:13px;color:#94a3b8;line-height:1.8;">'
            + "<br>".join(notes_html)
            + "</div>"
        )
        text += ["", *notes_text]

        return Section(
            key=self.name,
            title=self.title,
            html="".join(html),
            text="\n".join(text).strip(),
            priority=20,
        )

    def _sync_calendar(self, ctx, events) -> Optional[CalendarResult]:
        """把赛程写进 Outlook 日历；失败也不影响这封信，只在正文里注明。"""
        if not events or ctx.calendar_backend == "none":
            return None
        name = ctx.calendar_name or _calendar_name(ctx.now)
        try:
            return sync(
                events, name, backend=ctx.calendar_backend, plan_only=ctx.calendar_plan_only
            )
        except Exception as exc:
            ctx.warn("写欧冠日历失败：{}".format(exc))
            return CalendarResult("none", name, note="写日历失败，详情看日志")

    @staticmethod
    def _insights(finished: List[Match], fixtures: List[Match]) -> List[str]:
        items: List[str] = []
        if finished:
            top = max(finished, key=lambda m: m.goals)
            if top.goals >= 5:
                items.append(
                    "进球大战：{} {} - {} {}，全场 {} 球".format(
                        top.home, top.home_score, top.away_score, top.away, top.goals
                    )
                )
        focus = [m for m in fixtures if m.home in FOCUS_TEAMS and m.away in FOCUS_TEAMS]
        for match in focus[:2]:
            items.append("焦点战：{}（{}）".format(match.label(), match.when()))
        if fixtures and not focus:
            items.append("最早开球：{}（{}）".format(fixtures[0].label(), fixtures[0].when()))
        return items


PLUGIN = ChampionsLeaguePlugin()
