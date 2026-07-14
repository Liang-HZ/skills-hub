# usage_log 回归测试:Skill 触发次数的增量扫描与聚合(Claude Code / Codex / OpenCode)
# 运行:  python3 -m unittest discover -s tests -v
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="hub-usage-test-"))
# 与 test_pure_manager.py 共用同一进程时可能已经设置过;用 setdefault 不覆盖,
# 但无论谁先导入,webui/usage_log 的 HUB 都必然落在某个临时目录,绝不是真实 ~/skills-hub。
os.environ.setdefault("SKILLS_HUB_ROOT", str(TMP / "hub"))

sys.path.insert(0, str(REPO))
import usage_log  # noqa: E402


def skill_line(skill, ts=None, cwd="/tmp/proj", tool_name="Skill"):
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({
        "type": "assistant", "cwd": cwd, "timestamp": ts,
        "message": {"content": [{"type": "tool_use", "name": tool_name, "input": {"skill": skill}}]},
    })


def codex_session_meta(cwd="/tmp/codex-proj", ts=None):
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({"timestamp": ts, "type": "session_meta", "payload": {"cwd": cwd}})


def codex_exec_line(cmd_text, ts=None):
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({
        "timestamp": ts, "type": "response_item",
        "payload": {"type": "custom_tool_call", "name": "exec", "input": cmd_text},
    })


class ClaudeScanTests(unittest.TestCase):
    """Claude Code:结构化 Skill 工具调用,全部指向临时 fixture 目录。"""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=TMP))
        self.projects_dir = self.work / "claude-projects"
        self.projects_dir.mkdir()
        usage_log.DEFAULT_CLAUDE_PROJECTS_DIR = self.projects_dir
        usage_log.DEFAULT_CODEX_DIRS = [self.work / "no-codex"]
        usage_log.DEFAULT_OPENCODE_DB = self.work / "no-opencode.db"
        usage_log.DB_PATH = self.work / "usage.sqlite3"

    def _write(self, rel_path, text):
        p = self.projects_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_empty_dir_returns_no_skills(self):
        self.assertEqual(usage_log.stats(), {})

    def test_missing_dir_returns_no_skills(self):
        usage_log.DEFAULT_CLAUDE_PROJECTS_DIR = self.work / "does-not-exist"
        self.assertEqual(usage_log.stats(), {})

    def test_single_invocation_counted_in_all_windows(self):
        self._write("s1/a.jsonl", skill_line("demo-skill") + "\n")
        st = usage_log.stats()
        self.assertEqual(st["demo-skill"]["total"], 1)
        self.assertEqual(st["demo-skill"]["today"], 1)
        self.assertEqual(st["demo-skill"]["d7"], 1)
        self.assertEqual(st["demo-skill"]["d30"], 1)
        self.assertIsNotNone(st["demo-skill"]["last_day"])
        self.assertEqual(st["demo-skill"]["by_agent"]["claude"]["total"], 1)

    def test_non_skill_tool_use_ignored(self):
        self._write("s1/a.jsonl", skill_line("whatever", tool_name="Bash") + "\n")
        self.assertEqual(usage_log.stats(), {})

    def test_incremental_scan_does_not_double_count(self):
        f = self._write("s1/a.jsonl", skill_line("demo-skill") + "\n")
        self.assertEqual(usage_log.stats()["demo-skill"]["total"], 1)
        with f.open("a") as fh:
            fh.write(skill_line("demo-skill") + "\n")
        self.assertEqual(usage_log.stats()["demo-skill"]["total"], 2)

    def test_incomplete_trailing_line_waits_for_next_scan(self):
        full = skill_line("demo-skill")
        f = self._write("s1/a.jsonl", skill_line("demo-skill") + "\n" + full[:20])  # 尾行没写完,没有换行
        st = usage_log.stats()
        self.assertEqual(st["demo-skill"]["total"], 1, "半条 JSON 不该被计入")
        with f.open("a") as fh:
            fh.write(full[20:] + "\n")  # 补完这一行
        st = usage_log.stats()
        self.assertEqual(st["demo-skill"]["total"], 2, "补完后下次扫描应该计入")

    def test_old_event_in_total_but_not_recent_windows(self):
        self._write("s1/a.jsonl", skill_line("old-skill", ts="2020-01-01T00:00:00.000Z") + "\n")
        st = usage_log.stats()
        self.assertEqual(st["old-skill"]["total"], 1)
        self.assertEqual(st["old-skill"]["d7"], 0)
        self.assertEqual(st["old-skill"]["d30"], 0)
        self.assertEqual(st["old-skill"]["today"], 0)

    def test_subagent_subdir_is_scanned(self):
        self._write("sess/subagents/agent-x.jsonl", skill_line("sub-skill") + "\n")
        self.assertEqual(usage_log.stats()["sub-skill"]["total"], 1)

    def test_distinct_projects_counted(self):
        self._write("s1/a.jsonl", skill_line("multi-skill", cwd="/tmp/p1") + "\n")
        self._write("s2/a.jsonl", skill_line("multi-skill", cwd="/tmp/p2") + "\n")
        self.assertEqual(usage_log.stats()["multi-skill"]["projects"], 2)

    def test_missing_timestamp_does_not_pollute_recent_windows(self):
        line = json.dumps({"type": "assistant", "cwd": "/tmp/p1",
                            "message": {"content": [{"type": "tool_use", "name": "Skill",
                                                      "input": {"skill": "no-ts-skill"}}]}})
        self._write("s1/a.jsonl", line + "\n")
        st = usage_log.stats()
        self.assertEqual(st["no-ts-skill"]["total"], 1)
        self.assertEqual(st["no-ts-skill"]["today"], 0)
        self.assertIsNone(st["no-ts-skill"]["last_day"])


class CodexScanTests(unittest.TestCase):
    """Codex:没有专门的 Skill 工具,启发式扫 exec 类工具调用里出现的 SKILL.md 路径。"""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=TMP))
        usage_log.DEFAULT_CLAUDE_PROJECTS_DIR = self.work / "no-claude"
        self.codex_dir = self.work / "codex-sessions"
        self.codex_dir.mkdir()
        usage_log.DEFAULT_CODEX_DIRS = [self.codex_dir]
        usage_log.DEFAULT_OPENCODE_DB = self.work / "no-opencode.db"
        usage_log.DB_PATH = self.work / "usage.sqlite3"

    def _write(self, rel_path, text):
        p = self.codex_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_skill_md_path_in_exec_command_counted(self):
        cmd = "sed -n '1,80p' /Users/x/hub/library/deploy-runbook/SKILL.md"
        self._write("2026/07/10/rollout-1.jsonl",
                     codex_session_meta() + "\n" + codex_exec_line(cmd) + "\n")
        st = usage_log.stats()
        self.assertEqual(st["deploy-runbook"]["total"], 1)
        self.assertEqual(st["deploy-runbook"]["by_agent"]["codex"]["total"], 1)

    def test_two_distinct_skills_in_one_line_both_counted(self):
        cmd = ("cat /a/skills/foo-skill/SKILL.md && cat /b/library/bar-skill/SKILL.md")
        self._write("2026/07/10/rollout-1.jsonl",
                     codex_session_meta() + "\n" + codex_exec_line(cmd) + "\n")
        st = usage_log.stats()
        self.assertEqual(st["foo-skill"]["total"], 1)
        self.assertEqual(st["bar-skill"]["total"], 1)

    def test_repeated_path_in_same_line_counts_once(self):
        cmd = "cat /a/skills/dup-skill/SKILL.md; cat /a/skills/dup-skill/SKILL.md"
        self._write("2026/07/10/rollout-1.jsonl",
                     codex_session_meta() + "\n" + codex_exec_line(cmd) + "\n")
        self.assertEqual(usage_log.stats()["dup-skill"]["total"], 1)

    def test_no_skill_md_mention_not_counted(self):
        cmd = "git status && ls -la"
        self._write("2026/07/10/rollout-1.jsonl",
                     codex_session_meta() + "\n" + codex_exec_line(cmd) + "\n")
        self.assertEqual(usage_log.stats(), {})

    def test_project_cached_from_session_meta_across_incremental_scans(self):
        f = self._write("2026/07/10/rollout-1.jsonl", codex_session_meta(cwd="/tmp/codexproj") + "\n")
        usage_log.stats()  # 先扫一遍,消费掉 session_meta 那一行
        with f.open("a") as fh:
            cmd = "cat /a/skills/later-skill/SKILL.md"
            fh.write(codex_exec_line(cmd) + "\n")
        st = usage_log.stats()
        self.assertEqual(st["later-skill"]["total"], 1)
        self.assertEqual(st["later-skill"]["projects"], 1)

    def test_incremental_does_not_double_count(self):
        cmd = "cat /a/skills/inc-skill/SKILL.md"
        f = self._write("2026/07/10/rollout-1.jsonl",
                         codex_session_meta() + "\n" + codex_exec_line(cmd) + "\n")
        self.assertEqual(usage_log.stats()["inc-skill"]["total"], 1)
        with f.open("a") as fh:
            fh.write(codex_exec_line(cmd) + "\n")
        self.assertEqual(usage_log.stats()["inc-skill"]["total"], 2)


class OpenCodeScanTests(unittest.TestCase):
    """OpenCode:官方内置 skill 工具调用,记在 opencode.db 的 part 表里。"""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=TMP))
        usage_log.DEFAULT_CLAUDE_PROJECTS_DIR = self.work / "no-claude"
        usage_log.DEFAULT_CODEX_DIRS = [self.work / "no-codex"]
        self.db_path = self.work / "opencode.db"
        usage_log.DEFAULT_OPENCODE_DB = self.db_path
        usage_log.DB_PATH = self.work / "usage.sqlite3"
        self._init_opencode_db()

    def _init_opencode_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE project (id TEXT PRIMARY KEY);
            CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, directory TEXT);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                                time_created INTEGER, data TEXT);
        """)
        conn.execute("INSERT INTO project(id) VALUES ('p1')")
        conn.execute("INSERT INTO session(id,project_id,directory) VALUES ('s1','p1','/tmp/oc-proj')")
        conn.commit()
        conn.close()
        self._conn = sqlite3.connect(self.db_path)

    def _add_part(self, data_obj, time_created=None):
        time_created = time_created or int(datetime.now(timezone.utc).timestamp() * 1000)
        pid = f"part-{time_created}-{os.urandom(4).hex()}"
        self._conn.execute(
            "INSERT INTO part(id,message_id,session_id,time_created,data) VALUES (?,?,?,?,?)",
            (pid, "m1", "s1", time_created, json.dumps(data_obj)))
        self._conn.commit()

    def test_tool_skill_part_counted(self):
        self._add_part({"type": "tool-skill", "tool": "skill", "input": {"name": "git-release"}})
        st = usage_log.stats()
        self.assertEqual(st["git-release"]["total"], 1)
        self.assertEqual(st["git-release"]["by_agent"]["opencode"]["total"], 1)

    def test_alternate_shape_state_input_counted(self):
        self._add_part({"type": "tool", "tool": "skill", "state": {"input": {"name": "alt-shape"}}})
        self.assertEqual(usage_log.stats()["alt-shape"]["total"], 1)

    def test_non_skill_tool_ignored(self):
        self._add_part({"type": "tool-bash", "tool": "bash", "input": {"command": "ls"}})
        self.assertEqual(usage_log.stats(), {})

    def test_project_directory_counted(self):
        self._add_part({"type": "tool-skill", "tool": "skill", "input": {"name": "proj-skill"}})
        self.assertEqual(usage_log.stats()["proj-skill"]["projects"], 1)

    def test_incremental_scan_does_not_double_count(self):
        self._add_part({"type": "tool-skill", "tool": "skill", "input": {"name": "inc-oc-skill"}})
        self.assertEqual(usage_log.stats()["inc-oc-skill"]["total"], 1)
        self._add_part({"type": "tool-skill", "tool": "skill", "input": {"name": "inc-oc-skill"}})
        self.assertEqual(usage_log.stats()["inc-oc-skill"]["total"], 2)

    def test_missing_db_returns_no_skills(self):
        usage_log.DEFAULT_OPENCODE_DB = self.work / "does-not-exist.db"
        self.assertEqual(usage_log.stats(), {})


class CrossAgentAggregationTests(unittest.TestCase):
    """同一个技能被不同 agent 触发,total 应该加总,by_agent 应该分开。"""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=TMP))
        self.claude_dir = self.work / "claude-projects"
        self.claude_dir.mkdir()
        self.codex_dir = self.work / "codex-sessions"
        self.codex_dir.mkdir()
        self.oc_db = self.work / "opencode.db"
        usage_log.DEFAULT_CLAUDE_PROJECTS_DIR = self.claude_dir
        usage_log.DEFAULT_CODEX_DIRS = [self.codex_dir]
        usage_log.DEFAULT_OPENCODE_DB = self.oc_db
        usage_log.DB_PATH = self.work / "usage.sqlite3"
        conn = sqlite3.connect(self.oc_db)
        conn.executescript("""
            CREATE TABLE project (id TEXT PRIMARY KEY);
            CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, directory TEXT);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                                time_created INTEGER, data TEXT);
        """)
        conn.execute("INSERT INTO project(id) VALUES ('p1')")
        conn.execute("INSERT INTO session(id,project_id,directory) VALUES ('s1','p1','/tmp/oc-proj')")
        conn.execute("INSERT INTO part(id,message_id,session_id,time_created,data) VALUES (?,?,?,?,?)",
                      ("pt1", "m1", "s1", int(datetime.now(timezone.utc).timestamp() * 1000),
                       json.dumps({"type": "tool-skill", "tool": "skill", "input": {"name": "shared-skill"}})))
        conn.commit()
        conn.close()

    def test_total_sums_across_agents(self):
        (self.claude_dir / "s1").mkdir()
        (self.claude_dir / "s1" / "a.jsonl").write_text(skill_line("shared-skill") + "\n")
        (self.codex_dir / "2026" / "07" / "10").mkdir(parents=True)
        (self.codex_dir / "2026" / "07" / "10" / "rollout-1.jsonl").write_text(
            codex_session_meta() + "\n" +
            codex_exec_line("cat /x/library/shared-skill/SKILL.md") + "\n")
        st = usage_log.stats()
        self.assertEqual(st["shared-skill"]["total"], 3)
        self.assertEqual(st["shared-skill"]["by_agent"]["claude"]["total"], 1)
        self.assertEqual(st["shared-skill"]["by_agent"]["codex"]["total"], 1)
        self.assertEqual(st["shared-skill"]["by_agent"]["opencode"]["total"], 1)


class UsageApiRouteTests(unittest.TestCase):
    """/api/usage 走真实 HTTP 路由,同样只认临时 fixture 目录。"""

    @classmethod
    def setUpClass(cls):
        import webui  # noqa: E402  (放在类方法里,SKILLS_HUB_ROOT 在文件顶部已设好)
        cls.webui = webui
        webui.ensure_hub()
        cls.work = Path(tempfile.mkdtemp(dir=TMP))
        cls.projects_dir = cls.work / "claude-projects"
        cls.projects_dir.mkdir()
        usage_log.DEFAULT_CLAUDE_PROJECTS_DIR = cls.projects_dir
        usage_log.DEFAULT_CODEX_DIRS = [cls.work / "no-codex"]
        usage_log.DEFAULT_OPENCODE_DB = cls.work / "no-opencode.db"
        usage_log.DB_PATH = cls.work / "usage.sqlite3"
        f = cls.projects_dir / "s1" / "a.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(skill_line("route-skill") + "\n")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.srv.server_address[1]
        webui.SERVER_PORT = cls.port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_usage_route_returns_scanned_stats(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/usage")
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        self.assertEqual(r.status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["skills"]["route-skill"]["total"], 1)
        self.assertEqual(body["skills"]["route-skill"]["by_agent"]["claude"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
