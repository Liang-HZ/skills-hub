"""技能触发次数统计 —— 从各 coding agent 的本地会话记录里数技能调用。

支持三个 agent,信号可靠度依次下降:

- **claude**:~/.claude/projects/**/*.jsonl,每次调用 Skill 工具都有一条结构化
  tool_use 记录(name="Skill", input.skill=技能名),信号干净。
- **opencode**:~/.local/share/opencode/opencode.db(sqlite,session/message/part
  三张表),技能通过官方内置 `skill` 工具调用(`skill({name:"..."})`),记在某个
  part 的 data 里,信号也算干净,只是具体 JSON 形状没有本机真实样本验证过,
  代码里做了防御性的多形状尝试。
- **codex**:~/.codex/sessions/**/*.jsonl + ~/.codex/archived_sessions/*.jsonl。
  实测过 Codex 没有专门的 Skill 工具——技能就是靠 exec/apply_patch 之类的通用
  工具去 `cat`/`sed` 读 SKILL.md 文件。这里退化成启发式:只要某次 exec 类工具
  调用的文本里出现了形如 `.../<技能名>/SKILL.md` 的路径,就记一次。会比真实触发
  次数更粗略(读来看看 vs 真的照着做,这里区分不了),但比完全不统计有价值。

Cursor 的本地存储(state.vscdb 里的 aiService.prompts / cursorDiskKV 等 key)是
社区逆向出来的、官方不公开的格式,版本升级随时可能改动导致静默失效,暂不接入;
等社区/官方有更稳定的参考再说。

三者共用同一张 events 表,按 (agent, skill, day, project) 聚合、增量扫描(文件类
的按字节偏移,opencode 的按 sqlite rowid 高水位),本模块对所有数据源都只读不改。
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

HUB = Path(os.environ.get("SKILLS_HUB_ROOT") or Path(__file__).resolve().parent)
DB_PATH = HUB / ".state" / "usage.sqlite3"

DEFAULT_CLAUDE_PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR") or (Path.home() / ".claude" / "projects"))
_codex_override = os.environ.get("CODEX_SESSIONS_DIR")
DEFAULT_CODEX_DIRS = [Path(_codex_override)] if _codex_override else [
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "archived_sessions",
]
DEFAULT_OPENCODE_DB = Path(os.environ.get("OPENCODE_DB_PATH") or
                            (Path.home() / ".local" / "share" / "opencode" / "opencode.db"))

SUPPORTED_AGENTS = ("claude", "codex", "opencode")

_LOCK = threading.Lock()
_SENTINEL_DAY = "0000-00-00"   # 时间戳缺失/解析失败时的占位日:排序恒最早,不会污染"今日/近N天"
_SKILL_PATH_RE = re.compile(r'/([A-Za-z0-9][\w.-]*)/SKILL\.md')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  agent TEXT NOT NULL, path TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL,
  offset INTEGER NOT NULL, meta TEXT,
  PRIMARY KEY (agent, path)
);
CREATE TABLE IF NOT EXISTS events (
  agent TEXT NOT NULL, skill TEXT NOT NULL, day TEXT NOT NULL, project TEXT NOT NULL, count INTEGER NOT NULL,
  PRIMARY KEY (agent, skill, day, project)
);
"""


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(_SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()}
    if "agent" not in cols:   # 旧单 agent 版本的库:全是可从原始日志重新导出的派生数据,直接重建
        c.executescript("DROP TABLE files; DROP TABLE events;")
        c.executescript(_SCHEMA)
    return c


def _local_day(ts: str) -> str:
    if not ts:
        return _SENTINEL_DAY
    try:
        dt = datetime.fromisoformat(ts.rstrip("Z")).replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return _SENTINEL_DAY


def _epoch_day(value) -> str:
    if not value:
        return _SENTINEL_DAY
    try:
        v = float(value)
        if v > 10 ** 12:   # 毫秒时间戳
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return _SENTINEL_DAY


def _read_new_lines(path: Path, prev_offset: int):
    """从 prev_offset 读到文件末尾,只返回读到的完整行(bytes list)和新的偏移。"""
    with path.open("rb") as f:
        f.seek(prev_offset)
        data = f.read()
    nl = data.rfind(b"\n")
    if nl == -1:
        return [], prev_offset   # 没有新的完整行(可能会话正在写这一行),等下次
    lines = data[:nl].split(b"\n")
    return lines, prev_offset + nl + 1


def _file_row(conn, agent, path):
    return conn.execute("SELECT mtime, size, offset, meta FROM files WHERE agent=? AND path=?",
                         (agent, str(path))).fetchone()


def _save_file_row(conn, agent, path, st_mtime, st_size, offset, meta=None):
    conn.execute(
        "INSERT INTO files(agent,path,mtime,size,offset,meta) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(agent,path) DO UPDATE SET mtime=excluded.mtime,size=excluded.size,"
        "offset=excluded.offset,meta=excluded.meta",
        (agent, str(path), st_mtime, st_size, offset, meta))


def _add_events(conn, agent, tally):
    for (name, day, project), n in tally.items():
        conn.execute(
            "INSERT INTO events(agent,skill,day,project,count) VALUES(?,?,?,?,?) "
            "ON CONFLICT(agent,skill,day,project) DO UPDATE SET count=count+excluded.count",
            (agent, name, day, project, n))


# ---------- Claude Code:结构化 Skill 工具调用 ----------

def _claude_skill_events(raw_line: bytes):
    if b'"Skill"' not in raw_line:
        return []
    try:
        obj = json.loads(raw_line)
    except ValueError:
        return []
    content = (obj.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    day = _local_day(obj.get("timestamp") or "")
    project = obj.get("cwd") or ""
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
            name = (block.get("input") or {}).get("skill")
            if name:
                out.append((name, day, project))
    return out


def _scan_claude(root):
    if not root.is_dir():
        return
    conn = _conn()
    try:
        for path in sorted(root.rglob("*.jsonl")):
            try:
                st = path.stat()
            except OSError:
                continue
            row = _file_row(conn, "claude", path)
            if row and row[0] == st.st_mtime and row[1] == st.st_size:
                continue
            prev_offset = row[2] if row and st.st_size >= row[1] else 0
            try:
                lines, new_offset = _read_new_lines(path, prev_offset)
            except OSError:
                continue
            tally = {}
            for raw in lines:
                for key in _claude_skill_events(raw):
                    tally[key] = tally.get(key, 0) + 1
            _add_events(conn, "claude", tally)
            _save_file_row(conn, "claude", path, st.st_mtime, st.st_size, new_offset)
        conn.commit()
    finally:
        conn.close()


# ---------- Codex:没有专门的 Skill 工具,启发式扫 SKILL.md 路径 ----------

def _codex_tally(lines, cached_project):
    project = cached_project
    tally = {}
    for raw in lines:
        if b'"session_meta"' in raw and b'"cwd"' in raw:
            try:
                obj = json.loads(raw)
                cwd = (obj.get("payload") or {}).get("cwd")
                if cwd:
                    project = cwd
            except ValueError:
                pass
        if b"SKILL.md" not in raw:
            continue
        text = raw.decode("utf-8", "ignore")
        names = set(_SKILL_PATH_RE.findall(text))
        if not names:
            continue
        m_ts = re.search(r'"timestamp"\s*:\s*"([^"]+)"', text)
        day = _local_day(m_ts.group(1) if m_ts else "")
        for name in names:
            key = (name, day, project)
            tally[key] = tally.get(key, 0) + 1
    return tally, project


def _scan_codex(root):
    if not root.is_dir():
        return
    conn = _conn()
    try:
        for path in sorted(root.rglob("*.jsonl")):
            try:
                st = path.stat()
            except OSError:
                continue
            row = _file_row(conn, "codex", path)
            if row and row[0] == st.st_mtime and row[1] == st.st_size:
                continue
            prev_offset = row[2] if row and st.st_size >= row[1] else 0
            cached_project = (row[3] if row else "") or ""
            try:
                lines, new_offset = _read_new_lines(path, prev_offset)
            except OSError:
                continue
            tally, project = _codex_tally(lines, cached_project)
            _add_events(conn, "codex", tally)
            _save_file_row(conn, "codex", path, st.st_mtime, st.st_size, new_offset, project)
        conn.commit()
    finally:
        conn.close()


# ---------- OpenCode:官方内置 skill 工具,记在 opencode.db 的 part 表里 ----------

def _opencode_skill_name(data_text):
    if not data_text or ("skill" not in data_text):
        return None
    try:
        p = json.loads(data_text)
    except ValueError:
        return None
    if not isinstance(p, dict):
        return None
    if p.get("type") != "tool-skill" and p.get("tool") != "skill":
        return None
    for src in (p.get("input"), (p.get("state") or {}).get("input"), p.get("args"),
                (p.get("toolInvocation") or {}).get("args")):
        if isinstance(src, dict) and src.get("name"):
            return src["name"]
    return None


def _scan_opencode(db_path):
    if not db_path.is_file():
        return
    conn = _conn()
    try:
        row = _file_row(conn, "opencode", db_path)
        last_rowid = int(row[2]) if row else 0
        try:
            src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            return
        try:
            cur = src.execute(
                "SELECT part.rowid, part.data, part.time_created, session.directory "
                "FROM part JOIN session ON part.session_id = session.id "
                "WHERE part.rowid > ? ORDER BY part.rowid", (last_rowid,))
            tally = {}
            max_rowid = last_rowid
            for rowid, data, time_created, directory in cur:
                max_rowid = max(max_rowid, rowid)
                name = _opencode_skill_name(data)
                if not name:
                    continue
                day = _epoch_day(time_created)
                project = directory or ""
                key = (name, day, project)
                tally[key] = tally.get(key, 0) + 1
        finally:
            src.close()
        _add_events(conn, "opencode", tally)
        _save_file_row(conn, "opencode", db_path, 0, 0, max_rowid)
        conn.commit()
    finally:
        conn.close()


def scan(claude_dir=None, codex_dirs=None, opencode_db=None):
    """增量扫描三个 agent 的新增/变化记录,把新事件并入 usage.sqlite3。"""
    with _LOCK:
        _scan_claude(Path(claude_dir) if claude_dir else DEFAULT_CLAUDE_PROJECTS_DIR)
        for root in (codex_dirs if codex_dirs is not None else DEFAULT_CODEX_DIRS):
            _scan_codex(Path(root))
        _scan_opencode(Path(opencode_db) if opencode_db else DEFAULT_OPENCODE_DB)


def stats(claude_dir=None, codex_dirs=None, opencode_db=None):
    """扫描后返回 {技能名: {total, d7, d30, today, last_day, projects, by_agent}}。

    by_agent 是 {agent名: {total, d7, d30, today}} 的全量明细,给前端做「悬浮看各
    agent 占比」用;外层的 total/d7/d30/today 是三个 agent 加总。
    """
    scan(claude_dir, codex_dirs, opencode_db)
    with _LOCK:
        conn = _conn()
        try:
            today = datetime.now().astimezone().strftime("%Y-%m-%d")
            cut7 = (datetime.now().astimezone() - timedelta(days=6)).strftime("%Y-%m-%d")
            cut30 = (datetime.now().astimezone() - timedelta(days=29)).strftime("%Y-%m-%d")
            rows = conn.execute("""
                SELECT skill, agent,
                       SUM(count),
                       SUM(CASE WHEN day >= ? THEN count ELSE 0 END),
                       SUM(CASE WHEN day >= ? THEN count ELSE 0 END),
                       SUM(CASE WHEN day = ?  THEN count ELSE 0 END),
                       MAX(day)
                FROM events GROUP BY skill, agent
            """, (cut30, cut7, today)).fetchall()
            proj_rows = conn.execute(
                "SELECT skill, COUNT(DISTINCT project) FROM events GROUP BY skill").fetchall()
        finally:
            conn.close()
    proj_count = dict(proj_rows)
    out = {}
    for skill, agent, total, d30, d7, today_n, last_day in rows:
        s = out.setdefault(skill, {"total": 0, "d7": 0, "d30": 0, "today": 0,
                                    "last_day": None, "projects": proj_count.get(skill, 0), "by_agent": {}})
        s["total"] += total or 0
        s["d7"] += d7 or 0
        s["d30"] += d30 or 0
        s["today"] += today_n or 0
        if last_day and last_day != _SENTINEL_DAY and (s["last_day"] is None or last_day > s["last_day"]):
            s["last_day"] = last_day
        s["by_agent"][agent] = {"total": total or 0, "d7": d7 or 0, "d30": d30 or 0, "today": today_n or 0}
    return out
