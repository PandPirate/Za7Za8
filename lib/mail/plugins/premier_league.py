# -*- coding: utf-8 -*-
"""英超插件：上一轮赛果、积分榜、下一轮赛程和看点，并把赛程写进 Outlook 日历。

数据只来自 ESPN 的公开接口（eng.1 scoreboard / standings）。取不到就返回 None，
这封邮件干脆不带英超区块——不拿旧比分充数。
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
    "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?dates={day}"
)
STANDINGS = "https://site.api.espn.com/apis/v2/sports/soccer/eng.1/standings"
CALENDAR_NAME = "英超赛程 2026-27"

# ESPN 的英文队名 → 中文，没收录的原样保留
TEAM_CN = {
    "Arsenal": "阿森纳",
    "Aston Villa": "阿斯顿维拉",
    "AFC Bournemouth": "伯恩茅斯",
    "Bournemouth": "伯恩茅斯",
    "Brentford": "布伦特福德",
    "Brighton & Hove Albion": "布莱顿",
    "Chelsea": "切尔西",
    "Coventry City": "考文垂",
    "Crystal Palace": "水晶宫",
    "Everton": "埃弗顿",
    "Fulham": "富勒姆",
    "Hull City": "赫尔城",
    "Ipswich Town": "伊普斯维奇",
    "Leeds United": "利兹联",
    "Liverpool": "利物浦",
    "Manchester City": "曼城",
    "Manchester United": "曼联",
    "Newcastle United": "纽卡斯尔",
    "Nottingham Forest": "诺丁汉森林",
    "Sunderland": "桑德兰",
    "Tottenham Hotspur": "热刺",
}

# 2026/27 赛季每一轮的日期（周六那场，用来把比赛归到轮次上）
ROUND_DATES = (
    "2026-08-22", "2026-08-29", "2026-09-05", "2026-09-12", "2026-09-19",
    "2026-10-10", "2026-10-17", "2026-10-24", "2026-10-31", "2026-11-07",
    "2026-11-21", "2026-11-28", "2026-12-02", "2026-12-05", "2026-12-12",
    "2026-12-19", "2026-12-26", "2026-12-30", "2027-01-02", "2027-01-06",
    "2027-01-16", "2027-01-23", "2027-01-30", "2027-02-06", "2027-02-10",
    "2027-02-20", "2027-02-27", "2027-03-03", "2027-03-13", "2027-03-20",
    "2027-04-10", "2027-04-17", "2027-04-24", "2027-05-01", "2027-05-08",
    "2027-05-15", "2027-05-23", "2027-05-30",
)

# 「焦点战」的判定：两队都在这个名单里
FOCUS_TEAMS = {"利物浦", "曼联", "曼城", "阿森纳", "切尔西", "热刺", "纽卡斯尔"}

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
    """积分榜的一行。ESPN 的 pointsFor / pointsAgainst 指的是**进球数**，不是积分。"""

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


def round_of(day: dt.date) -> Optional[int]:
    """这一天属于第几轮：取「不晚于次日」的最近一轮，季末一周双赛也够用。"""
    probe = day + dt.timedelta(days=1)
    best = None
    for index, text in enumerate(ROUND_DATES, 1):
        if dt.date.fromisoformat(text) <= probe:
            best = index
    return best


def _score(side, finished: bool) -> Optional[int]:
    raw = side.get("score")
    if not finished or raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _team_name(side) -> str:
    """ESPN 的队名 → 中文；字段缺失时给空串，不让 None 漏进 Match。"""
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
            )
        )
    return matches


def _season_days(payload) -> List[dt.date]:
    """响应里的 leagues[].calendar 是整季的比赛日，拿它决定要请求哪几天。"""
    days = []
    for league in payload.get("leagues", []):
        for item in league.get("calendar") or []:
            try:
                days.append(dt.datetime.fromisoformat(str(item).replace("Z", "+00:00")).date())
            except ValueError:
                continue
    return days


def _safe_json(ctx, url):
    """单日取失败不算致命——少一天数据总比整封邮件没有强。"""
    try:
        return ctx.http_json(url)
    except Exception as exc:
        ctx.warn("取英超 {} 的数据失败：{}".format(url.rsplit("=", 1)[-1], exc))
        return None


def _fetch_live(ctx) -> List[Match]:
    """先取今天的响应拿到赛季日历，再只请求窗口内的比赛日。

    ESPN 的 scoreboard 不接受日期区间（传 a-b 会 400），但一整季也就几十个比赛日，
    按日历逐日取，通常七八个请求就够。
    """
    today = ctx.now.astimezone(UTC).date()
    seed = ctx.http_json(SCOREBOARD.format(day=today.strftime("%Y%m%d")))
    calendar = _season_days(seed)

    low = today - dt.timedelta(days=14)
    high = today + dt.timedelta(days=40)
    wanted = sorted({day for day in calendar if low <= day <= high})
    if not wanted:  # 没有日历（接口变了）就隔三天探一次，别盲扫
        wanted = [today + dt.timedelta(days=offset) for offset in range(-14, 41, 3)]

    matches: List[Match] = []
    seen = set()

    def absorb(payload):
        for match in parse_espn(payload):
            key = (match.kickoff, match.home, match.away)
            if key in seen:
                continue
            seen.add(key)
            matches.append(match)

    absorb(seed)
    # 每个请求各建一次连接、比较慢，并发取
    targets = [day for day in wanted if day != today]
    if targets:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for payload in pool.map(
                lambda day: _safe_json(ctx, SCOREBOARD.format(day=day.strftime("%Y%m%d"))), targets
            ):
                if payload is not None:
                    absorb(payload)
    return matches


def load_matches(ctx):
    """实时赛程；取不到就返回空列表，由插件决定不产出这一块。"""
    try:
        return _fetch_live(ctx), "ESPN 实时比分"
    except Exception as exc:
        ctx.warn("英超数据拉取失败：{}".format(exc))
        return [], "无数据"


def _stat(stats, name: str, default: int = 0) -> int:
    """stats 是 [{name, value, ...}]，value 一律是 float（如 27.0），转成 int。"""
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
    """积分榜；取不到就返回空列表，不拖累整封邮件。"""
    try:
        return parse_standings(ctx.http_json(STANDINGS))
    except Exception as exc:
        ctx.warn("积分榜拉取失败：{}".format(exc))
        return []


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
        # 用 or 0：未结束的比赛比分是 None，直接比较会抛 TypeError
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


def _calendar_events(matches: List[Match], round_no: Optional[int]) -> List[CalendarEvent]:
    events = []
    for match in matches:
        start = match.kickoff.astimezone(UTC)
        events.append(
            CalendarEvent(
                subject="英超 {} vs {}".format(match.home, match.away),
                start=start,
                end=start + dt.timedelta(hours=2),
                location=match.venue,
                body="英超第{}轮 · 北京时间 {}".format(round_no, match.when())
                if round_no
                else "北京时间 {}".format(match.when()),
            )
        )
    return events


# ---------------------------------------------------------------- 插件


class PremierLeaguePlugin:
    name = "premier_league"
    title = "英超快报"
    description = "英超上一轮赛果、积分榜、下一轮赛程、看点，并把赛程写进 Outlook 日历"
    enabled_by_default = True

    def build(self, ctx) -> Optional[Section]:
        matches, source = load_matches(ctx)
        if not matches:
            return None

        now = ctx.now
        played = sorted((m for m in matches if m.finished), key=lambda m: m.kickoff, reverse=True)
        last_round = round_of(played[0].local.date()) if played else None
        finished = (
            [m for m in played if round_of(m.local.date()) == last_round][:12]
            if last_round
            else played[:10]
        )
        upcoming = sorted(
            (m for m in matches if not m.finished and m.kickoff >= now - dt.timedelta(hours=3)),
            key=lambda m: m.kickoff,
        )
        next_round = round_of(upcoming[0].local.date()) if upcoming else None
        fixtures = (
            [m for m in upcoming if round_of(m.local.date()) == next_round]
            if next_round
            else upcoming[:10]
        )
        if not finished and not fixtures:
            return None

        standings = load_standings(ctx)

        html: List[str] = []
        text: List[str] = []

        if finished:
            head = "赛果 · 第 {} 轮".format(last_round) if last_round else "赛果"
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
                    _pad(row.team, 10),
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
            head = "赛程 · 第 {} 轮".format(next_round) if next_round else "赛程"
            html += [_label("{} · {}（北京时间）".format(head, span)), _agenda(fixtures)]
            text += ["", "{} · {}（北京时间）".format(head, span)]
            for day, items in _group_by_day(fixtures):
                text.append("{}/{} {}".format(day.month, day.day, _weekday(day)))
                text += ["  {}  {}".format(m.local.strftime("%H:%M"), m.label()) for m in items]

        insights = self._insights(finished, fixtures)
        if insights:
            html += [_label("看点"), _bullets(insights)]
            text += ["", "看点"] + ["· " + item for item in insights]

        events = _calendar_events(fixtures, next_round) if fixtures else []
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
            priority=10,
        )

    @staticmethod
    def _sync_calendar(ctx, events) -> Optional[CalendarResult]:
        """把赛程写进 Outlook 日历；失败也不影响这封信，只在正文里注明。"""
        if not events or ctx.calendar_backend == "none":
            return None
        name = ctx.calendar_name or CALENDAR_NAME
        try:
            return sync(
                events, name, backend=ctx.calendar_backend, plan_only=ctx.calendar_plan_only
            )
        except Exception as exc:
            ctx.warn("写日历失败：{}".format(exc))
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


PLUGIN = PremierLeaguePlugin()
