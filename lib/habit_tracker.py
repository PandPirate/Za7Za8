# -*- coding: utf-8 -*-
"""习惯打卡：SQLite 存储、回复解析、邮件排版。

编号就是 `habits.id`：全局唯一、永不复用。删掉中间一个，邮件里就会出现 1、3、4，
不会重新编号，这样回复和统计都不会错位。

每个待办可以带一个「目标」（`阅读;20分钟`），存在 `habits.goal` 里；每天建打卡时
把它快照进 `checkins.goal`，以后改目标不会改写历史。邮件只展示最新目标，统计（共完成
次数、连续天数）按编号跨目标累计。

数据库默认在 `state/habits.db`（已 gitignore），可用 MAILER_HABIT_DB 覆盖。
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from paths import STATE_DIR

DB_PATH = Path(os.environ.get("MAILER_HABIT_DB") or (STATE_DIR / "habits.db"))
WEEKDAY_CN = "一二三四五六日"
SUBJECT = "21天习惯养成"
# 过了当天还没回复的打卡，次日自动标成未完成，原因写这个
EXPIRED_REASON = "当日未回复"

SCHEMA = """
CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
    day TEXT NOT NULL,
    habit_id INTEGER NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (day, habit_id)
);
CREATE TABLE IF NOT EXISTS replies (
    message_id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent (
    day TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS habits_title ON habits(title);
"""

_NUM_RE = re.compile(r"\d+")
_QUOTE_STOP = (
    re.compile(r"^-{2,}\s*(Original Message|原始邮件)", re.I),
    re.compile(r"^On\s.+wrote:\s*$", re.I),
    re.compile(r"^在\s.+写道[：:]?\s*$"),
    re.compile(r"^(From|发件人|Sent|发送时间|To|收件人|Subject|主题)[:：]", re.I),
)
_IGNORE_PREFIX = (
    "今日待办",
    "回复格式",
    "回复示例",
    "打卡结果",
    "当前没有待办",
    "没完成的必须写原因",
    "所有待办",
    "P=完成",
)
_STATUS_MARKS = ("✅", "❌", "➕", "🗑", "ℹ", "⚠", "⏳")


@dataclass
class HabitRow:
    number: int          # = habits.id，稳定编号，全局唯一且不复用
    habit_id: int
    title: str
    goal: str            # 最新目标，空串表示没设
    created_at: str
    total_done: int
    status: str          # pending / done / missed
    reason: str
    streak: int


@dataclass
class Command:
    action: str          # done / missed / add / delete / all_done
    numbers: List[int] = field(default_factory=list)
    reason: str = ""
    title: str = ""
    goal: str = ""
    line: str = ""


@dataclass
class ReplyResult:
    day: str
    done: List[str] = field(default_factory=list)
    missed: List[Tuple[str, str]] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    """老库升级：`CREATE TABLE IF NOT EXISTS` 不会补新列，这里手动加 goal。"""
    for table in ("habits", "checkins"):
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info({})".format(table))
        }
        if "goal" not in columns:
            conn.execute(
                "ALTER TABLE {} ADD COLUMN goal TEXT NOT NULL DEFAULT ''".format(table)
            )


@contextmanager
def _db(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    db_path = Path(path) if path else DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _streak(conn: sqlite3.Connection, habit_id: int, day: str) -> int:
    """截至 day 的连续完成天数；今天还没打卡就从昨天往前数。"""
    rows = conn.execute(
        "SELECT day, status FROM checkins WHERE habit_id = ? AND day <= ?",
        (habit_id, day),
    ).fetchall()
    status = {row["day"]: row["status"] for row in rows}
    cursor = dt.date.fromisoformat(day)
    if status.get(day) == "done":
        pass
    elif status.get(day) == "pending":
        cursor -= dt.timedelta(days=1)
    else:
        return 0
    count = 0
    while status.get(cursor.isoformat()) == "done":
        count += 1
        cursor -= dt.timedelta(days=1)
    return count


def _rows(conn: sqlite3.Connection, day: str) -> List[HabitRow]:
    rows = conn.execute(
        "SELECT c.habit_id, h.title, h.goal, h.created_at, c.status, c.reason, "
        "(SELECT COUNT(*) FROM checkins t "
        " WHERE t.habit_id = c.habit_id AND t.status = 'done') AS total_done "
        "FROM checkins c JOIN habits h ON h.id = c.habit_id "
        "WHERE c.day = ? AND h.active = 1 ORDER BY h.id",
        (day,),
    ).fetchall()
    return [
        HabitRow(
            number=int(row["habit_id"]),
            habit_id=int(row["habit_id"]),
            title=row["title"],
            goal=str(row["goal"] or ""),
            created_at=row["created_at"],
            total_done=int(row["total_done"]),
            status=row["status"],
            reason=row["reason"],
            streak=_streak(conn, row["habit_id"], day),
        )
        for row in rows
    ]


def ensure_day(day: str, db_path: Optional[Path] = None) -> List[HabitRow]:
    """给当天所有启用的习惯补一条 pending，返回当天快照。

    补的时候顺手把**当天的目标**存进 checkins，以后改了目标也不会改掉历史。
    """
    with _db(db_path) as conn:
        now = _now()
        existing = {
            row["habit_id"]
            for row in conn.execute("SELECT habit_id FROM checkins WHERE day = ?", (day,))
        }
        for row in conn.execute("SELECT id, goal FROM habits WHERE active = 1 ORDER BY id"):
            if row["id"] in existing:
                continue
            conn.execute(
                "INSERT INTO checkins(day, habit_id, goal, status, reason, updated_at) "
                "VALUES(?, ?, ?, 'pending', '', ?)",
                (day, row["id"], str(row["goal"] or ""), now),
            )
        return _rows(conn, day)


def day_rows(day: str, db_path: Optional[Path] = None) -> List[HabitRow]:
    with _db(db_path) as conn:
        return _rows(conn, day)


def close_overdue(
    today: str, reason: str = EXPIRED_REASON, db_path: Optional[Path] = None
) -> int:
    """把 today 之前还停在 pending 的打卡标成 missed，返回条数。

    日报一天只发一封，用户不回复那条 pending 就永远停在那里；过了当天它就不再
    是「还没到截止时间」，而是「当天没完成」。不区分开，连续天数和完成率都会算错。

    只动 `day < today` 的行：今天还没过完，pending 是正常状态。
    用户事后补回复时会再次更新这些行，迟到的「完成」仍然算数。
    """
    with _db(db_path) as conn:
        cur = conn.execute(
            "UPDATE checkins SET status='missed', reason=?, updated_at=? "
            "WHERE day < ? AND status='pending'",
            (reason, _now(), today),
        )
        return int(cur.rowcount or 0)


def add_habit(
    title: str,
    day: str,
    db_path: Optional[Path] = None,
    goal: str = "",
) -> int:
    """测试 / 初始化用：新增一个习惯，并给当天补 pending。"""
    with _db(db_path) as conn:
        now = _now()
        goal = goal.strip()
        cur = conn.execute(
            "INSERT INTO habits(title, goal, active, created_at) VALUES(?, ?, 1, ?)",
            (title.strip(), goal, now),
        )
        habit_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT OR IGNORE INTO checkins(day, habit_id, goal, status, reason, updated_at) "
            "VALUES(?, ?, ?, 'pending', '', ?)",
            (day, habit_id, goal, now),
        )
        return habit_id


def _numbers(text: str) -> List[int]:
    return sorted({int(value) for value in _NUM_RE.findall(text)})


def _reason(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^(原因|因为|理由)\s*[:：]?\s*", "", text)
    return text.lstrip(":：").strip()


def _clean_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = unicodedata.normalize("NFKC", raw).strip()
        if not line or line.startswith(">"):
            continue
        if any(pattern.search(line) for pattern in _QUOTE_STOP):
            break
        # 模板占位符（<编号> / <原因>）和引用里的列表行不参与解析
        if "<" in line or ">" in line:
            continue
        if re.match(r"^\d+[.、)]\s*", line):
            continue
        lines.append(line)
    return lines


def parse_commands(text: str) -> Tuple[List[Command], List[str]]:
    """解析固定格式的回复；返回（命令, 错误）。"""
    commands: List[Command] = []
    errors: List[str] = []
    for line in _clean_lines(text):
        if line.startswith(_IGNORE_PREFIX) or line.startswith(_STATUS_MARKS):
            continue
        if line.rstrip(":：。.").strip() == "全部完成":
            commands.append(Command("all_done", line=line))
            continue

        match = re.match(
            r"^(?:PASS|P|DONE|完成|打卡)\s*[:：]?\s*(.+)$", line, re.I
        )
        if match:
            numbers = _numbers(match.group(1))
            if numbers:
                commands.append(Command("done", numbers=numbers, line=line))
            else:
                errors.append("「{}」没写编号".format(line))
            continue

        match = re.match(
            r"^(?:FAIL|F|未完成|没完成)\s*[:：]?\s*(.+)$", line, re.I
        )
        if match:
            rest = match.group(1)
            number_match = re.match(r"^([\d\s,，、]+)\s*(.*)$", rest)
            if not number_match:
                errors.append("「{}」没写编号".format(line))
                continue
            numbers = _numbers(number_match.group(1))
            reason = _reason(number_match.group(2))
            if not numbers:
                errors.append("「{}」没写编号".format(line))
            elif not reason:
                errors.append(
                    "F {} 需要写原因，例如：F {} 加班".format(
                        numbers[0], numbers[0]
                    )
                )
            else:
                commands.append(
                    Command("missed", numbers=numbers, reason=reason, line=line)
                )
            continue

        match = re.match(
            r"^(?:DELETE|DEL|D|删除)\s*[:：]?\s*(.+)$", line, re.I
        )
        if match:
            numbers = _numbers(match.group(1))
            if numbers:
                commands.append(Command("delete", numbers=numbers, line=line))
            else:
                errors.append("「{}」没写编号".format(line))
            continue

        match = re.match(r"^(?:ADD|A|新增|添加)\s*[:：]?\s*(.+)$", line, re.I)
        if match:
            # 一行一个待办；分号前是名称，分号后是目标（可省略）
            title, _, goal = match.group(1).partition(";")
            title = title.strip()
            goal = goal.strip()
            if title:
                commands.append(Command("add", title=title, goal=goal, line=line))
            else:
                errors.append("「{}」没写待办名称".format(line))
            continue

        errors.append("看不懂这一行：{}".format(line))
    return commands, errors


def apply_reply(day: str, text: str, db_path: Optional[Path] = None) -> ReplyResult:
    commands, errors = parse_commands(text)
    result = ReplyResult(day=day, errors=list(errors))
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT c.habit_id, h.title FROM checkins c "
            "JOIN habits h ON h.id = c.habit_id "
            "WHERE c.day = ? AND h.active = 1 ORDER BY h.id",
            (day,),
        ).fetchall()
        by_number = {int(row["habit_id"]): row for row in rows}
        now = _now()

        for command in commands:
            if command.action == "all_done":
                for row in rows:
                    conn.execute(
                        "UPDATE checkins SET status='done', reason='', updated_at=? "
                        "WHERE day=? AND habit_id=?",
                        (now, day, row["habit_id"]),
                    )
                    result.done.append(row["title"])
                continue

            if command.action == "add":
                existing = conn.execute(
                    "SELECT id, active, goal FROM habits WHERE title = ?",
                    (command.title,),
                ).fetchone()
                if existing is not None:
                    habit_id = int(existing["id"])
                    old_goal = str(existing["goal"] or "")
                    if not existing["active"]:
                        conn.execute("UPDATE habits SET active=1 WHERE id=?", (habit_id,))
                        result.notes.append("恢复：{}".format(command.title))
                    if command.goal and command.goal != old_goal:
                        conn.execute(
                            "UPDATE habits SET goal=? WHERE id=?", (command.goal, habit_id)
                        )
                        result.notes.append(
                            "目标更新：{}（{} → {}）".format(
                                command.title, old_goal or "无", command.goal
                            )
                        )
                    elif existing["active"]:
                        result.notes.append("已存在：{}".format(command.title))
                else:
                    cur = conn.execute(
                        "INSERT INTO habits(title, goal, active, created_at) "
                        "VALUES(?, ?, 1, ?)",
                        (command.title, command.goal, now),
                    )
                    habit_id = int(cur.lastrowid or 0)
                    result.added.append(command.title)

                current_goal = str(
                    conn.execute(
                        "SELECT goal FROM habits WHERE id = ?", (habit_id,)
                    ).fetchone()["goal"]
                    or ""
                )
                conn.execute(
                    "INSERT OR IGNORE INTO checkins"
                    "(day, habit_id, goal, status, reason, updated_at) "
                    "VALUES(?, ?, ?, 'pending', '', ?)",
                    (day, habit_id, current_goal, now),
                )
                # 目标变了就同步今天的快照；已经打过的卡保持原样
                conn.execute(
                    "UPDATE checkins SET goal=? WHERE day=? AND habit_id=?",
                    (current_goal, day, habit_id),
                )
                continue

            for number in command.numbers:
                row = by_number.get(number)
                if row is None:
                    result.errors.append("编号 {} 不存在".format(number))
                    continue
                if command.action == "done":
                    conn.execute(
                        "UPDATE checkins SET status='done', reason='', updated_at=? "
                        "WHERE day=? AND habit_id=?",
                        (now, day, row["habit_id"]),
                    )
                    result.done.append(row["title"])
                elif command.action == "missed":
                    conn.execute(
                        "UPDATE checkins SET status='missed', reason=?, updated_at=? "
                        "WHERE day=? AND habit_id=?",
                        (command.reason, now, day, row["habit_id"]),
                    )
                    result.missed.append((row["title"], command.reason))
                elif command.action == "delete":
                    conn.execute(
                        "UPDATE habits SET active=0 WHERE id=?", (row["habit_id"],)
                    )
                    result.deleted.append(row["title"])

        if not commands and not result.errors:
            result.errors.append("没有识别到命令")
    return result


def is_processed(message_id: str, db_path: Optional[Path] = None) -> bool:
    if not message_id:
        return False
    with _db(db_path) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM replies WHERE message_id = ?", (message_id,)
            ).fetchone()
            is not None
        )


def record_reply(
    message_id: str,
    day: str,
    received_at: str,
    db_path: Optional[Path] = None,
) -> None:
    if not message_id:
        return
    with _db(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO replies(message_id, day, received_at) "
            "VALUES(?, ?, ?)",
            (message_id, day, received_at),
        )


def was_sent(day: str, db_path: Optional[Path] = None) -> bool:
    """当天是否已经发过每日邮件（一天只发一封新邮件）。"""
    with _db(db_path) as conn:
        return (
            conn.execute("SELECT 1 FROM sent WHERE day = ?", (day,)).fetchone()
            is not None
        )


def mark_sent(day: str, db_path: Optional[Path] = None) -> None:
    with _db(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sent(day, sent_at) VALUES(?, ?)",
            (day, _now()),
        )


# ---------------------------------------------------------------- 排版


def date_label(day: str) -> str:
    value = dt.date.fromisoformat(day)
    return "{} 周{}".format(day, WEEKDAY_CN[value.weekday()])


def _header(day: str, title: str) -> str:
    return (
        '<div style="font-size:13px;color:#64748b;letter-spacing:.04em;'
        'margin:0 0 8px;">{} · {}</div>'.format(escape(title), escape(date_label(day)))
    )


def _template_html() -> str:
    return (
        '<div style="margin-top:18px;padding:12px 14px;background:#f8fafc;'
        'border:1px solid #e2e8f0;border-radius:8px;font-family:ui-monospace,'
        "Menlo,Consolas,monospace;font-size:14px;line-height:1.9;color:#334155;\">"
        '<div style="font-weight:700;color:#0f172a;margin-bottom:4px;">'
        "P=完成 · F=未完成（写原因） · A=新增 · D=删除</div>"
        "P &lt;编号...&gt;<br>"
        "F &lt;编号...&gt; &lt;原因&gt;<br>"
        "A &lt;待办&gt;;&lt;目标&gt;（目标可省略，一行一个）<br>"
        "D &lt;编号...&gt;"
        "</div>"
    )


def _template_text() -> str:
    return "\n".join(
        (
            "P=完成  F=未完成（写原因）  A=新增  D=删除",
            "P <编号...>",
            "F <编号...> <原因>",
            "A <待办>;<目标>（目标可省略，一行一个）",
            "D <编号...>",
        )
    )


def _title_text(row: HabitRow) -> str:
    """待办那一行：编号由调用方拼，这里是名称 + 目标。"""
    return row.title if not row.goal else "{};{}".format(row.title, row.goal)


def _status_text(row: HabitRow) -> str:
    if row.status == "done":
        return "✅ 完成"
    if row.status == "missed":
        return "❌ 未完成：{}".format(row.reason or "未写原因")
    return "⏳ 未回复；没完成请回复：未完成 {} 原因".format(row.number)


def _stats_text(row: HabitRow) -> str:
    start = (row.created_at or "")[:10] or "?"
    return "自 {} 开始 · 共完成 {} 次 · 连续 {} 天".format(
        start, row.total_done, row.streak
    )


def render_daily(day: str, rows: List[HabitRow]) -> Tuple[str, str]:
    """每日待办：HTML 与纯文本。"""
    if rows:
        html_rows = "".join(
            '<div style="padding:10px 0;border-bottom:1px solid #f1f5f9;'
            'font-size:16px;color:#0f172a;">'
            '<span style="display:inline-block;width:26px;color:#94a3b8;">{}'
            "</span>{}"
            '<div style="font-size:13px;color:#94a3b8;margin:2px 0 0 26px;">{}</div>'
            "</div>".format(
                row.number,
                escape(_title_text(row)),
                escape(_stats_text(row)),
            )
            for row in rows
        )
        text_rows = "\n".join(
            "{} {}\n   {}".format(row.number, _title_text(row), _stats_text(row))
            for row in rows
        )
    else:
        html_rows = (
            '<div style="padding:12px 0;font-size:16px;color:#334155;">'
            "今天还没有待办。回复 <b>新增 阅读;20分钟</b> 添加第一条。</div>"
        )
        text_rows = "今天还没有待办。回复：新增 阅读;20分钟"

    note = (
        "P/F/D 可接多个编号；A 一行一个，分号后是目标（可省略）。"
        "没完成的必须写原因；没提到的会继续显示为未回复。"
    )
    html = (
        _header(day, "今日待办")
        + html_rows
        + _template_html()
        + '<div style="margin-top:10px;font-size:13px;color:#94a3b8;">{}</div>'.format(
            escape(note)
        )
    )
    text = "\n".join(
        (
            "今日待办 · {}".format(date_label(day)),
            text_rows,
            "",
            _template_text(),
            "",
            note,
        )
    )
    return html, text


def render_summary(day: str, result: ReplyResult, rows: List[HabitRow]) -> Tuple[str, str]:
    """回信摘要：所有待办 + 本次变更 + 错误。"""
    changes_html: List[str] = []
    changes_text: List[str] = []
    if result.done:
        changes_html.append("✅ 完成：" + "、".join(escape(item) for item in result.done))
        changes_text.append("✅ 完成：" + "、".join(result.done))
    if result.missed:
        changes_html.append(
            "❌ 未完成："
            + "、".join(
                "{}（{}）".format(escape(title), escape(reason))
                for title, reason in result.missed
            )
        )
        changes_text.append(
            "❌ 未完成："
            + "、".join("{}（{}）".format(title, reason) for title, reason in result.missed)
        )
    if result.added:
        changes_html.append("➕ 新增：" + "、".join(escape(item) for item in result.added))
        changes_text.append("➕ 新增：" + "、".join(result.added))
    if result.deleted:
        changes_html.append("🗑 删除：" + "、".join(escape(item) for item in result.deleted))
        changes_text.append("🗑 删除：" + "、".join(result.deleted))
    if result.notes:
        changes_html.append("ℹ 提示：" + "、".join(escape(item) for item in result.notes))
        changes_text.append("ℹ 提示：" + "、".join(result.notes))
    if result.errors:
        changes_html.append(
            "⚠ 回复格式有问题：" + "；".join(escape(item) for item in result.errors)
        )
        changes_text.append("⚠ 回复格式有问题：" + "；".join(result.errors))

    if rows:
        rows_html = "".join(
            '<div style="padding:9px 0;border-bottom:1px solid #f1f5f9;'
            'font-size:16px;color:#0f172a;">'
            '<span style="display:inline-block;width:26px;color:#94a3b8;">{}'
            "</span>{}"
            '<div style="font-size:13px;color:#64748b;margin:2px 0 0 26px;">{}</div>'
            '<div style="font-size:12px;color:#94a3b8;margin:2px 0 0 26px;">{}</div>'
            "</div>".format(
                row.number,
                escape(_title_text(row)),
                escape(_status_text(row)),
                escape(_stats_text(row)),
            )
            for row in rows
        )
        rows_text = "\n".join(
            "{} {} — {}\n   {}".format(
                row.number, _title_text(row), _status_text(row), _stats_text(row)
            )
            for row in rows
        )
    else:
        rows_html = (
            '<div style="padding:12px 0;font-size:16px;color:#334155;">'
            "当前没有待办。回复 <b>新增 阅读;20分钟</b> 添加。</div>"
        )
        rows_text = "当前没有待办。回复：新增 阅读;20分钟"

    changes_block = (
        '<div style="font-size:14px;line-height:1.8;color:#334155;">'
        + "<br>".join(changes_html or ["（本次没有识别到有效变更）"])
        + "</div>"
    )
    help_html = ""
    help_text = ""
    if result.errors:
        help_html = (
            '<div style="margin-top:12px;font-size:13px;color:#9a3412;">'
            "格式有问题的行已忽略，请按下面格式重新回复。</div>"
        )
        help_text = "格式有问题的行已忽略，请按下面格式重新回复。"
    html = (
        _header(day, "打卡结果")
        + changes_block
        + '<div style="font-size:13px;color:#64748b;letter-spacing:.04em;'
        'margin:20px 0 6px;">所有待办</div>'
        + rows_html
        + help_html
        + _template_html()
    )
    text_parts = [
        "打卡结果 · {}".format(date_label(day)),
        "\n".join(changes_text or ["（本次没有识别到有效变更）"]),
        "",
        "所有待办",
        rows_text,
    ]
    if help_text:
        text_parts += ["", help_text]
    text_parts += ["", _template_text()]
    text = "\n".join(text_parts)
    return html, text
