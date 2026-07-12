# 纯管理器回归测试(设计文档: docs/superpowers/specs/2026-07-10-skill-manager-only-design.md)
# 运行:  python3 -m unittest discover -s tests -v
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="hub-test-"))
os.environ["SKILLS_HUB_ROOT"] = str(TMP / "hub")

sys.path.insert(0, str(REPO))
import webui  # noqa: E402  (必须在设好 SKILLS_HUB_ROOT 之后导入)

# 把三类全局放置点也指进临时目录,测试绝不触碰真实家目录
for k in webui.KINDS:
    webui.ROOTS[k] = TMP / f"home/.{k}/skills"

SRC = (REPO / "webui.py").read_text()


class Recorder:
    """替身 sh():记录所有子进程调用,git 走本地无妨,其余一律不真跑。"""
    def __init__(self, passthrough_git=True):
        self.calls = []
        self.passthrough = passthrough_git
        self.real = None

    def __call__(self, args, cwd=None, timeout=300):
        self.calls.append([str(a) for a in args])
        if self.passthrough and args and str(args[0]) == "git":
            return self.real(args, cwd=cwd, timeout=timeout)
        import subprocess
        return subprocess.CompletedProcess(args, 0, "", "")

    def joined(self):
        return [" ".join(c) for c in self.calls]


def with_recorder(passthrough_git=True):
    rec = Recorder(passthrough_git)
    rec.real = webui.sh.__wrapped__ if hasattr(webui.sh, "__wrapped__") else _REAL_SH
    webui.sh = rec
    return rec


_REAL_SH = webui.sh


def restore_sh():
    webui.sh = _REAL_SH


NETWORK_GIT = ("clone", "fetch", "pull", "ls-remote", "push")
FORBIDDEN_BINARIES = ("hub_review", "hub_probe", "hub_watch", "claude", "codex",
                      "sandbox-exec", "curl", "wget")


class TestSourceRemoval(unittest.TestCase):
    """审核器、探针、监听不在活动代码里,也没有可达调用。"""

    def test_reviewer_files_deleted(self):
        for f in ("hub_review.py", "hub_probe.py", "hub_watch.py"):
            self.assertFalse((REPO / f).exists(), f"{f} 应已从活动工作树删除")

    def test_no_reviewer_imports(self):
        self.assertIsNone(re.search(r"^\s*(import|from)\s+hub_(review|probe|watch)", SRC, re.M))
        for word in ("hub_review", "hub_probe", "hub_watch", "sandbox-exec",
                     "review_dir", "run_review", "save_verdict", "toggle_gate",
                     "trigger_review", "judge"):
            self.assertNotIn(word, SRC, f"生产代码不应再出现 {word}")

    def test_no_review_api_routes(self):
        gone = {"/api/review/run", "/api/judge/test", "/api/watch/scan",
                "/api/update/merge", "/api/update/dismiss", "/api/update/recheck"}
        self.assertFalse(gone & set(webui.POST_OPS), "审核/监听 API 不应再注册")

    def test_page_has_no_verdict_ui(self):
        for token in ("安全审核", "审核", "危险", "verdict", "rv-pass", "rv-fail",
                      "预检", "监听", "dynamic_probe", "api_key"):
            self.assertNotIn(token, webui.PAGE, f"页面不应再包含 {token}")

    def test_state_has_no_review_fields(self):
        st = webui.api_state()
        for key in ("reviews", "pending", "judge", "key_set", "presets",
                    "dynamic_probe", "scanning", "watch_on"):
            self.assertNotIn(key, st)


class TestNoImplicitExecutionOrNetwork(unittest.TestCase):
    """管理动作不执行技能内容、不联网;git 一律禁 hook。"""

    def setUp(self):
        webui.ensure_hub()

    def tearDown(self):
        restore_sh()

    def test_state_never_touches_network(self):
        rec = with_recorder()
        webui.api_state()
        for line in rec.joined():
            for verb in NETWORK_GIT:
                self.assertNotIn(f" {verb} ", f" {line} ")

    def test_manage_ops_never_spawn_reviewer_or_network(self):
        rec = with_recorder()
        webui.op_new({"name": "t-skill"})
        webui.op_save_skill({"name": "t-skill", "content": "---\nname: t-skill\ndescription: x\n---\n"})
        webui.op_toggle({"target": "claude", "skill": "t-skill", "on": True})
        webui.op_toggle({"target": "claude", "skill": "t-skill", "on": False})
        webui.op_save_set({"name": "t-set", "content": "t-skill\n"})
        webui.op_set_delete({"name": "t-set"})
        webui.op_delete({"name": "t-skill"})
        for line in rec.joined():
            head = Path(line.split(" ")[0]).name
            self.assertNotIn(head, FORBIDDEN_BINARIES, f"管理动作不应启动 {head}")
            for verb in NETWORK_GIT:
                self.assertNotIn(f" {verb} ", f" {line} ")

    def test_git_wrapper_disables_hooks(self):
        rec = with_recorder(passthrough_git=False)
        webui.git(["status"])
        self.assertTrue(any("core.hooksPath=" in " ".join(c) for c in rec.calls),
                        "管理器发起的 git 必须指定独立空 hooksPath")

    def test_toggle_uses_links_not_execution(self):
        webui.op_new({"name": "link-skill"})
        r = webui.op_toggle({"target": "claude", "skill": "link-skill", "on": True})
        self.assertTrue(r["ok"], r)
        entry = webui.ROOTS["claude"] / "link-skill"
        self.assertIsNotNone(webui.read_link(entry), "开启应创建链接而不是执行任何东西")
        webui.op_toggle({"target": "claude", "skill": "link-skill", "on": False})
        self.assertEqual(webui.entry_state(entry, "link-skill"), "absent")
        webui.op_delete({"name": "link-skill"})


class TestHistoryPreserved(unittest.TestCase):
    """历史审核记录默认保留,删除技能不改写 reviews/。"""

    def test_delete_keeps_review_history(self):
        webui.ensure_hub()
        rv = webui.HUB / "reviews" / "keep-me.json"
        rv.parent.mkdir(exist_ok=True)
        rv.write_text("{}")
        webui.op_new({"name": "keep-me"})
        webui.op_delete({"name": "keep-me"})
        self.assertTrue(rv.exists(), "删除技能不应动 reviews/ 历史文件")


class TestUpdateTokens(unittest.TestCase):
    """检查与更新是两次独立授权;令牌一次性、绑定来源。"""

    def test_update_without_check_refused(self):
        r = webui.source_update("whatever", None)
        self.assertFalse(r["ok"])
        self.assertIn("令牌", r["out"])

    def test_update_refused_does_not_touch_network(self):
        rec = with_recorder(passthrough_git=False)
        try:
            webui.source_update("whatever", "bogus")
            self.assertEqual(rec.calls, [], "令牌无效时不应执行任何命令")
        finally:
            restore_sh()

    def test_token_single_use_and_source_bound(self):
        tok = webui.issue_update_token("src-a", "deadbeef")
        r = webui.source_update("src-b", tok)          # 换来源 → 拒
        self.assertFalse(r["ok"])
        r = webui.source_update("src-a", tok)          # 已被消费 → 拒
        self.assertFalse(r["ok"])
        tok2 = webui.issue_update_token("src-a", "deadbeef")
        webui.UPDATE_TOKENS[tok2]["exp"] = 0           # 过期 → 拒
        self.assertFalse(webui.source_update("src-a", tok2)["ok"])


class TestHttpAuthorization(unittest.TestCase):
    """写 API 由后端校验:同源 + JSON + 会话令牌,缺一不可。"""

    @classmethod
    def setUpClass(cls):
        webui.ensure_hub()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.srv.server_address[1]
        webui.SERVER_PORT = cls.port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _post(self, path, body=b"{}", headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json", "X-Hub-Token": webui.CSRF_TOKEN,
             "Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request("POST", path, body=body, headers=h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"))
        c.close()
        return out

    def test_page_serves_and_state_readable(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/")
        page = c.getresponse().read().decode()
        self.assertIn(webui.CSRF_TOKEN, page, "页面应携带会话令牌")
        c.request("GET", "/api/state")
        st = json.loads(c.getresponse().read())
        self.assertIn("skills", st)
        c.close()

    def test_missing_token_rejected(self):
        code, _ = self._post("/api/settings", headers={"X-Hub-Token": ""})
        self.assertEqual(code, 403)

    def test_wrong_token_rejected(self):
        code, _ = self._post("/api/settings", headers={"X-Hub-Token": "0" * 32})
        self.assertEqual(code, 403)

    def test_non_json_content_type_rejected(self):
        code, _ = self._post("/api/settings", headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 403)

    def test_cross_origin_rejected(self):
        code, _ = self._post("/api/settings", headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)

    def test_dns_rebinding_host_rejected(self):
        code, _ = self._post("/api/settings", host="evil.example:80")
        self.assertEqual(code, 403)

    def test_authorized_write_accepted(self):
        code, j = self._post("/api/settings", body=b'{"clean_empty_dirs": true}')
        self.assertEqual(code, 200)
        self.assertTrue(j["ok"])

    def test_update_api_with_bogus_token_refused(self):
        code, j = self._post("/api/source/update",
                             body=b'{"source": "x", "token": "bogus"}')
        self.assertEqual(code, 200)
        self.assertFalse(j["ok"])


if __name__ == "__main__":
    unittest.main()
