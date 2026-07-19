"""技能触发次数统计 —— 从各 coding agent 的本地会话记录里数技能调用。

支持三个 agent,信号可靠度依次下降:

- **claude**:~/.claude/projects/**/*.jsonl,每次调用 Skill 工具都有一条结构化
  tool_use 记录(name="Skill", input.skill=技能名),信号干净。resume/fork 会把
  历史消息复制进新会话文件,同一条 tool_use 会在多个文件里出现——按 tool_use id
  去重,同一次调用只算一次。
- **opencode**:opencode.db(sqlite,session/message/part 三张表),技能通过官方
  内置 `skill` 工具调用,记在 part.data 里(实测形状:
  {"type":"tool","tool":"skill","state":{"input":{"name":...}}}),信号干净。
- **codex**:~/.codex/sessions/**/*.jsonl + ~/.codex/archived_sessions/*.jsonl。
  Codex 自身的技能调用检测就是启发式(源码里的隐式调用检测:命令读了 SKILL.md
  或跑了技能 scripts/ 下的脚本),Codex App 显示的 "runs" 也来自这套检测。
  这里对齐它的口径:只认 response_item 里 exec 类工具调用的**命令文本**
  (会话文件里到处是 SKILL.md 字样——turn_context 每轮注入技能列表、输出回显、
  compacted 历史副本,全都不是触发);路径必须落在标准技能放置目录
  (.claude/.codex/.agents,可带 skills 子层)——在 skills-hub 库目录里开发技能
  不算使用;命中"读 SKILL.md"或"跑 <技能>/scripts/ 脚本"都算;**同一轮
  (turn_context 分界)同一技能只算一次**。语义即「触发轮数」,与 Codex App 的
  runs 同口径(实测三个技能对齐度 93%-100%;App 只从 2026-05 功能上线后开始记,
  我们扫全部历史,所以老技能这边的数字会更大)。

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


def _default_claude_projects_dir():
    """所有项目的会话都在同一个根下:<配置目录>/projects/<按路径编码的项目名>/*.jsonl。
    配置目录默认 ~/.claude(Windows 是 %USERPROFILE%\\.claude),可被 CLAUDE_CONFIG_DIR 整体搬走。"""
    override = os.environ.get("CLAUDE_PROJECTS_DIR")
    if override:
        return Path(override)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def _default_codex_dirs():
    """会话按日期(不按项目)存在 <CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*.jsonl,默认 ~/.codex。"""
    override = os.environ.get("CODEX_SESSIONS_DIR")
    if override:
        return [Path(override)]
    base = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return [base / "sessions", base / "archived_sessions"]


def _default_opencode_db():
    """opencode 即使在 Windows 上也用 XDG 风格路径;桌面版在 %LOCALAPPDATA%\\opencode\\data。
    按候选顺序取第一个真实存在的,一个都不存在就用第一个候选(is_file 会让扫描静默跳过)。"""
    for env in ("OPENCODE_DB_PATH", "OPENCODE_DB"):
        v = os.environ.get(env)
        if v:
            return Path(v)
    candidates = []
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        candidates.append(Path(xdg) / "opencode" / "opencode.db")
    candidates.append(Path.home() / ".local" / "share" / "opencode" / "opencode.db")
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "opencode" / "data" / "opencode.db")
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


DEFAULT_CLAUDE_PROJECTS_DIR = _default_claude_projects_dir()
DEFAULT_CODEX_DIRS = _default_codex_dirs()
DEFAULT_OPENCODE_DB = _default_opencode_db()

SUPPORTED_AGENTS = ("claude", "codex", "opencode")

_LOCK = threading.Lock()
_SENTINEL_DAY = "0000-00-00"   # 时间戳缺失/解析失败时的占位日:排序恒最早,不会污染"今日/近N天"
# 只认标准技能放置目录下的路径(.claude/.codex/.agents,可带 skills 子层,兼容 Windows 反斜杠)。
# 库目录(library/<name>/SKILL.md)故意不匹配:在 skills-hub 里开发技能不算使用。
_SKILL_PATH_RE = re.compile(r'\.(?:claude|codex|agents)[/\\]+(?:skills[/\\]+)?([A-Za-z0-9][\w.-]*)[/\\]+SKILL\.md')
# 隐式调用的另一半:跑了技能 scripts/ 目录下的脚本(与 Codex 自身检测同口径)
_SKILL_SCRIPT_RE = re.compile(r'\.(?:claude|codex|agents)[/\\]+(?:skills[/\\]+)?([A-Za-z0-9][\w.-]*)[/\\]+scripts[/\\]')
# Codex 里真正"发起执行"的 exec 类工具;apply_patch(编辑)、update_plan、read_mcp_resource 等都不算触发
_CODEX_EXEC_TOOLS = {"exec_command", "exec", "shell", "local_shell", "container.exec"}

_SCHEMA_VERSION = 3   # v3:codex 按轮去重(对齐 Codex App runs 口径)+ scripts/ 隐式调用;升版时全量重建

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
CREATE TABLE IF NOT EXISTS claude_calls (
  id TEXT PRIMARY KEY
);
"""


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(_SCHEMA)
    version = c.execute("PRAGMA user_version").fetchone()[0]
    if version < _SCHEMA_VERSION:   # 全是可从原始日志重新导出的派生数据,直接重建
        c.executescript("DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS events; "
                        "DROP TABLE IF EXISTS claude_calls;")
        c.executescript(_SCHEMA)
        c.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        c.commit()
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
                out.append((block.get("id"), (name, day, project)))
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
                for call_id, key in _claude_skill_events(raw):
                    if call_id:
                        # resume/fork 会把历史消息原样复制进新会话文件:同一 tool_use id 只算一次
                        cur = conn.execute("INSERT OR IGNORE INTO claude_calls(id) VALUES(?)", (call_id,))
                        if cur.rowcount == 0:
                            continue
                    tally[key] = tally.get(key, 0) + 1
            _add_events(conn, "claude", tally)
            _save_file_row(conn, "claude", path, st.st_mtime, st.st_size, new_offset)
        conn.commit()
    finally:
        conn.close()


# ---------- Codex:没有专门的 Skill 工具,对齐 Codex App "runs" 的口径数"触发轮数" ----------
#
# 一个会话文件里 SKILL.md 字样远多于真实触发:turn_context 每轮把技能列表注入上下文、
# function_call_output/event_msg 回显命令、compacted 复制历史。所以只认 response_item
# 里 exec 类工具的命令文本(读 SKILL.md 或跑 <技能>/scripts/ 脚本),并以 turn_context
# 为轮分界,同一轮同一技能只记一次(记在该轮首次命中那天)。

def _codex_meta(raw_meta):
    """files.meta 的 JSON 形状:{"project": 会话 cwd, "turn_seen": [当前轮已计数的技能]}。
    turn_seen 只需要保留"当前这一轮"的:增量扫描可能停在一轮中间,下次接着扫时
    不能把同一轮里的再次读取又算一次;一旦扫到新的 turn_context 就清空。"""
    if raw_meta:
        try:
            obj = json.loads(raw_meta)
            if isinstance(obj, dict):
                return {"project": obj.get("project") or "", "turn_seen": list(obj.get("turn_seen") or ())}
        except ValueError:
            return {"project": raw_meta, "turn_seen": []}
    return {"project": "", "turn_seen": []}


def _codex_tally(lines, meta):
    project = meta["project"]
    turn_seen = set(meta["turn_seen"])
    tally = {}
    for raw in lines:
        if b'"turn_context"' in raw:
            try:
                if json.loads(raw).get("type") == "turn_context":
                    turn_seen = set()   # 新的一轮开始
                    continue
            except ValueError:
                pass
        if b'"session_meta"' in raw and b'"cwd"' in raw:
            try:
                obj = json.loads(raw)
                cwd = (obj.get("payload") or {}).get("cwd")
                if cwd:
                    project = cwd
            except ValueError:
                pass
            continue
        if b"SKILL.md" not in raw and b"scripts" not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload") or {}
        if payload.get("type") not in ("function_call", "custom_tool_call"):
            continue
        if payload.get("name") not in _CODEX_EXEC_TOOLS:
            continue
        args = payload.get("arguments")
        if not isinstance(args, str):
            args = payload.get("input")
        if not isinstance(args, str):
            try:
                args = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                continue
        names = (set(_SKILL_PATH_RE.findall(args)) | set(_SKILL_SCRIPT_RE.findall(args))) - turn_seen
        if not names:
            continue
        day = _local_day(obj.get("timestamp") or "")
        for name in names:
            turn_seen.add(name)
            key = (name, day, project)
            tally[key] = tally.get(key, 0) + 1
    return tally, {"project": project, "turn_seen": sorted(turn_seen)}


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
            meta = _codex_meta(row[3] if row else None)
            try:
                lines, new_offset = _read_new_lines(path, prev_offset)
            except OSError:
                continue
            tally, meta = _codex_tally(lines, meta)
            _add_events(conn, "codex", tally)
            _save_file_row(conn, "codex", path, st.st_mtime, st.st_size, new_offset,
                           json.dumps(meta, ensure_ascii=False))
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
