# -*- coding: utf-8 -*-
"""英超插件：赛果、赛程、看点，外加一份当轮赛程的 .ics 附件。

数据两条路：
    实时：ESPN 的公开比分接口（eng.1 scoreboard），按日期区间一次取回；
    离线：plugins/data/ 里的快照，--offline 或实时请求失败时用。

没有网、也没有快照时不会硬编造比分——插件直接返回 None，这封邮件就不带英超区块。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import escape
from typing import List, Optional

from ..calendar_sync import CalendarEvent, CalendarResult, sync
from ..core import CST, UTC, Section

SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?dates={day}"
)
SNAPSHOT = "sample_pl_2026-11-09.json"

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
        return self.state == "post" and self.home_score is not None

    @property
    def goals(self) -> int:
        if self.home_score is None or self.away_score is None:
            return 0
        return self.home_score + self.away_score

    def when(self) -> str:
        return self.local.strftime("%m-%d %H:%M")

    def label(self) -> str:
        return "{} vs {}".format(self.home, self.away)


# ---------------------------------------------------------------- 取数


def round_of(day: dt.date) -> Optional[int]:
    """离这一天最近、且不晚于「次日」的那一轮。季末连着一周双赛也够用。"""
    probe = day + dt.timedelta(days=1)
    best = None
    for index, text in enumerate(ROUND_DATES, 1):
        if dt.date.fromisoformat(text) <= probe:
            best = index
    return best


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

        def score(side):
            raw = side.get("score")
            if not finished or raw in (None, ""):
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        matches.append(
            Match(
                kickoff=dt.datetime.fromisoformat(event["date"].replace("Z", "+00:00")),
                home=TEAM_CN.get(home["team"]["displayName"], home["team"]["displayName"]),
                away=TEAM_CN.get(away["team"]["displayName"], away["team"]["displayName"]),
                home_score=score(home),
                away_score=score(away),
                state=state,
                venue=(competition.get("venue") or {}).get("fullName", ""),
            )
        )
    return matches


def parse_snapshot(payload) -> List[Match]:
    matches: List[Match] = []
    for item in payload.get("matches", []):
        score = item.get("score") or [None, None]
        matches.append(
            Match(
                kickoff=dt.datetime.fromisoformat(item["kickoff"].replace("Z", "+00:00")),
                home=item["home"],
                away=item["away"],
                home_score=score[0],
                away_score=score[1],
                state=item.get("state", "pre"),
                venue=item.get("venue", ""),
            )
        )
    return matches


def _season_days(ctx, payload):
    """响应里的 leagues[].calendar 是整季的比赛日，拿它决定要请求哪几天。"""
    days = []
    for league in payload.get("leagues", []):
        for item in league.get("calendar") or []:
            try:
                days.append(dt.datetime.fromisoformat(str(item).replace("Z", "+00:00")).date())
            except ValueError:
                continue
    return days


def _fetch_live(ctx) -> List[Match]:
    """先取今天的响应拿到赛季日历，再只请求窗口内的比赛日。

    ESPN 的 scoreboard 不接受日期区间（传 a-b 会 400），但一整季也就几十个比赛日，
    按日历逐日取，通常七八个请求就够。
    """
    today = ctx.now.astimezone(UTC).date()
    seed = ctx.http_json(SCOREBOARD.format(day=today.strftime("%Y%m%d")))
    calendar = _season_days(ctx, seed)

    low = today - dt.timedelta(days=9)
    high = today + dt.timedelta(days=30)
    wanted = sorted({day for day in calendar if low <= day <= high})
    if not wanted:  # 没有日历（接口变了）就隔三天探一次，别盲扫
        wanted = [today + dt.timedelta(days=offset) for offset in range(-9, 31, 3)]

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


def _safe_json(ctx, url):
    """单日取失败不算致命——少一天数据总比整封邮件没有强。"""
    try:
        return ctx.http_json(url)
    except Exception as exc:
        ctx.warn("取英超 {} 的数据失败：{}".format(url.rsplit("=", 1)[-1], exc))
        return None


def load_matches(ctx) -> "tuple[List[Match], str]":
    if not ctx.offline:
        try:
            return _fetch_live(ctx), "ESPN 实时比分"
        except Exception as exc:  # 网络不通就退回快照
            ctx.warn("英超实时数据拉取失败（{}），改用离线快照".format(exc))
    try:
        payload = ctx.snapshot(SNAPSHOT)
    except Exception as exc:
        ctx.warn("没有可用的英超快照（{}），本次跳过".format(exc))
        return [], "无数据"
    return parse_snapshot(payload), "离线快照 · {}".format(payload.get("captured_at", ""))


# ---------------------------------------------------------------- 排版


def _subtitle(text: str) -> str:
    return (
        '<div style="font-size:12px;color:#64748b;letter-spacing:.04em;'
        'margin:18px 0 4px;">' + text + "</div>"
    )


def _results_table(matches: List[Match]) -> str:
    rows = []
    for match in matches:
        home_style = "font-weight:700;" if match.home_score > match.away_score else ""
        away_style = "font-weight:700;" if match.away_score > match.home_score else ""
        rows.append(
            "<tr>"
            f'<td style="padding:9px 10px 9px 0;border-bottom:1px solid #eef2f7;color:#64748b;'
            f'font-size:12px;white-space:nowrap;">{match.when()}</td>'
            f'<td style="padding:9px 10px 9px 0;border-bottom:1px solid #eef2f7;text-align:right;{home_style}">{match.home}</td>'
            f'<td style="padding:9px 0;border-bottom:1px solid #eef2f7;text-align:center;font-weight:700;'
            f'white-space:nowrap;color:#0f172a;">{match.home_score} - {match.away_score}</td>'
            f'<td style="padding:9px 0 9px 10px;border-bottom:1px solid #eef2f7;{away_style}">{match.away}</td>'
            "</tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;font-size:14px;">' + "".join(rows) + "</table>"
    )


def _group_by_day(matches: List[Match]) -> list:
    """按北京时间的日期分组。中间没比赛的日子也补一格，看起来才像日历。"""
    buckets = {}
    for match in matches:
        buckets.setdefault(match.local.date(), []).append(match)
    if not buckets:
        return []
    days = sorted(buckets)
    span = (days[-1] - days[0]).days + 1
    if span <= 7:
        days = [days[0] + dt.timedelta(days=i) for i in range(span)]
    return [(day, sorted(buckets.get(day, []), key=lambda m: m.kickoff)) for day in days]


def _wd(day) -> str:
    return "周" + "一二三四五六日"[day.weekday()]


def _week_strip(days) -> str:
    """一排日期格子，有比赛的那天点亮。"""
    width = max(1, int(100 / max(1, len(days))))
    cells = []
    for day, items in days:
        active = bool(items)
        bg = "#eef2ff" if active else "#f8fafc"
        fg = "#1f2937" if active else "#94a3b8"
        accent = "#4338ca" if active else "#cbd5e1"
        label = "{} 场".format(len(items)) if items else "—"
        cells.append(
            f'<td width="{width}%" align="center" style="padding:0 3px;vertical-align:top;">'
            f'<div style="background:{bg};border-radius:10px;padding:9px 2px 10px 2px;">'
            f'<div style="font-size:11px;color:{accent};line-height:1.5;">{_wd(day)}</div>'
            f'<div style="font-size:18px;font-weight:700;color:{fg};line-height:1.25;">{day.day}</div>'
            f'<div style="font-size:11px;color:{accent};line-height:1.5;">{label}</div>'
            f"</div></td>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:separate;border-spacing:6px 0;margin:0 -3px;table-layout:fixed;">'
        "<tr>" + "".join(cells) + "</tr></table>"
    )


def _agenda(days) -> str:
    """按天排的赛程清单，左边日期、右边当天所有场次。"""
    rows = []
    for day, items in days:
        if not items:
            continue
        lines = "<br>".join(
            f'<span style="color:#64748b;">{m.local:%H:%M}</span> {m.home}'
            f' <span style="color:#94a3b8;">vs</span> {m.away}'
            for m in items
        )
        rows.append(
            "<tr>"
            f'<td width="96" valign="top" style="padding:9px 12px 9px 0;border-top:1px solid #eef2f7;'
            f'font-size:13px;font-weight:600;color:#0f172a;white-space:nowrap;">'
            f"{day.month}/{day.day} {_wd(day)}</td>"
            f'<td style="padding:9px 0;border-top:1px solid #eef2f7;font-size:14px;'
            f'line-height:1.9;color:#0f172a;">{lines}</td>'
            "</tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;">' + "".join(rows) + "</table>"
    )


def _calendar_block(matches: List[Match]) -> str:
    days = _group_by_day(matches)
    return _week_strip(days) + _agenda(days)


def _bullets(items: List[str]) -> str:
    if not items:
        return ""
    lis = "".join(
        '<li style="margin:5px 0;color:#334155;">' + item + "</li>" for item in items
    )
    return '<ul style="margin:2px 0 0;padding-left:18px;font-size:14px;">' + lis + "</ul>"


def _calendar_block(matches: List[Match]) -> str:
    days = _group_by_day(matches)
    return _week_strip(days) + _agenda(days)


def _bullets(items: List[str]) -> str:
    if not items:
        return ""
    lis = "".join(
        '<li style="margin:5px 0;color:#334155;">' + item + "</li>" for item in items
    )
    return '<ul style="margin:2px 0 0;padding-left:18px;font-size:14px;">' + lis + "</ul>"


def _calendar_events(matches: List[Match], round_no: Optional[int]) -> List[CalendarEvent]:
    events = []
    for match in matches:
        start = match.kickoff.astimezone(UTC)
        # key 用哈希，保持 ASCII 且对同一场比赛稳定（重复跑不会长出副本）
        digest = hashlib.md5(
            "{}|{}|{}".format(start.strftime("%Y%m%dT%H%M"), match.home, match.away).encode("utf-8")
        ).hexdigest()[:12]
        events.append(
            CalendarEvent(
                key="mailer:pl:" + digest,
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
    description = "英超最近赛果、下一轮赛程、看点，并把赛程写进本机日历"
    enabled_by_default = True

    def build(self, ctx) -> Optional[Section]:
        matches, source = load_matches(ctx)
        if not matches:
            return None

        now = ctx.now
        played = sorted([m for m in matches if m.finished], key=lambda m: m.kickoff, reverse=True)
        last_round = round_of(played[0].local.date()) if played else None
        finished = (
            [m for m in played if round_of(m.local.date()) == last_round][:12]
            if last_round
            else played[:10]
        )
        upcoming = sorted(
            [
                m
                for m in matches
                if not m.finished and m.kickoff >= now - dt.timedelta(hours=3)
            ],
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


        parts_html: List[str] = []
        parts_text: List[str] = []

        if finished:
            head = "赛果 · 第 {} 轮".format(last_round) if last_round else "赛果 · 最近完赛"
            parts_html.append(_subtitle(head))
            parts_html.append(_results_table(finished))
            parts_text.append(head)
            parts_text += [
                "· {}  {} {}-{} {}".format(
                    m.when(), m.home, m.home_score, m.away_score, m.away
                )
                for m in finished
            ]

        if fixtures:
            span = "{}–{}".format(fixtures[0].local.strftime("%m/%d"), fixtures[-1].local.strftime("%m/%d"))
            head = "赛程 · 第 {} 轮".format(next_round) if next_round else "赛程"
            parts_html.append(_subtitle("{}（{}，北京时间）".format(head, span)))
            parts_html.append(_calendar_block(fixtures))
            parts_text.append("")
            parts_text.append("{}（{}，北京时间）".format(head, span))
            for day, items in _group_by_day(fixtures):
                if not items:
                    continue
                parts_text.append("{}/{} {}".format(day.month, day.day, _wd(day)))
                parts_text += [
                    "  {}  {}".format(m.local.strftime("%H:%M"), m.label()) for m in items
                ]

        insights = self._insights(finished, fixtures)
        if insights:
            parts_html.append(_subtitle("看点"))
            parts_html.append(_bullets(insights))
            parts_text.append("")
            parts_text.append("看点")
            parts_text += ["· " + item for item in insights]

        events = _calendar_events(fixtures, next_round) if fixtures else []
        result = self._sync_calendar(ctx, events)
        if result is not None and result.line():
            parts_html.append(
                '<div style="margin-top:16px;padding:10px 12px;background:#f8fafc;border-radius:8px;'
                'font-size:13px;color:#334155;">🗓 {}</div>'.format(escape(result.line()))
            )
            parts_text.append("")
            parts_text.append(result.line())

        parts_html.append(
            '<div style="margin-top:16px;font-size:12px;color:#94a3b8;">数据来源：{}</div>'.format(source)
        )
        parts_text.append("")
        parts_text.append("数据来源：" + source)

        return Section(
            key=self.name,
            title=self.title,
            html="".join(parts_html),
            text="\n".join(parts_text),
            priority=10,
        )

    @staticmethod
    def _sync_calendar(ctx, events) -> Optional[CalendarResult]:
        """把赛程写进 Graph 上的 Outlook 日历；失败也不影响这封信，只在正文里注明。"""
        if not events or ctx.calendar_backend == "none":
            return None
        name = ctx.calendar_name or "英超赛程 2026-27"
        try:
            return sync(
                events,
                name,
                backend=ctx.calendar_backend,
                plan_only=ctx.calendar_plan_only,
            )
        except Exception as exc:
            ctx.warn("写日历失败：{}".format(exc))
            return CalendarResult("none", name, note="Graph 调用失败，检查登录状态")

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
            items.append("下轮最早开球：{}（{}）".format(fixtures[0].label(), fixtures[0].when()))
        return items


PLUGIN = PremierLeaguePlugin()
