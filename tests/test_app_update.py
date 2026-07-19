# 软件更新感知的回归测试:检查/应用都必须非破坏。
# 运行:  python3 -m unittest discover -s tests -v
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="hub-update-test-"))
os.environ.setdefault("SKILLS_HUB_ROOT", str(TMP / "hub"))

sys.path.insert(0, str(REPO))
import webui  # noqa: E402  (必须在设好 SKILLS_HUB_ROOT 之后导入)


class AppUpdateTests(unittest.TestCase):
    """给 HUB 仓库配一个本地目录当 origin,模拟上游有新提交。"""

    @classmethod
    def setUpClass(cls):
        webui.ensure_hub()
        # 保证 HUB 至少有一个提交(空仓库 clone 不出分支)
        marker = webui.HUB / "MARKER.md"
        marker.write_text("v1\n")
        webui.git(["add", "MARKER.md"], cwd=webui.HUB)
        webui.git(["commit", "-m", "marker v1"], cwd=webui.HUB)

    def setUp(self):
        webui._APP_VERSION_CACHE = None
        webui.git(["remote", "remove", "origin"], cwd=webui.HUB)
        self.upstream = Path(tempfile.mkdtemp(dir=TMP)) / "upstream"
        r = webui.git(["clone", str(webui.HUB), str(self.upstream)])
        self.assertEqual(r.returncode, 0, r.stderr)
        webui.git(["remote", "add", "origin", str(self.upstream)], cwd=webui.HUB)
        webui._APP_VERSION_CACHE = None

    def tearDown(self):
        webui.git(["remote", "remove", "origin"], cwd=webui.HUB)
        webui._APP_VERSION_CACHE = None

    def upstream_commit(self, text):
        (self.upstream / "MARKER.md").write_text(text)
        webui.git(["add", "MARKER.md"], cwd=self.upstream)
        webui.git(["commit", "-m", f"upstream: {text.strip()}"], cwd=self.upstream)

    def test_version_info_present(self):
        v = webui.app_version()
        self.assertTrue(v["head"])
        self.assertTrue(v["branch"])
        self.assertIn("upstream", v["origin"])

    def test_git_commit_refreshes_cached_version(self):
        # 通过页面建/改技能会推进 HEAD;「当前版本」不能停在启动时缓存的旧值
        before = webui.app_version()["head"]          # 建立缓存
        (webui.LIB / "ver-cache-probe").mkdir(exist_ok=True)
        (webui.LIB / "ver-cache-probe" / "SKILL.md").write_text(
            "---\nname: ver-cache-probe\ndescription: 探针\n---\n正文\n")
        try:
            webui.git_commit("新建 ver-cache-probe")
            after = webui.app_version()["head"]
            self.assertNotEqual(before, after, "git_commit 后缓存版本应已刷新")
            head_now = webui.git(["log", "-1", "--format=%h %ad", "--date=short"],
                                 cwd=webui.HUB).stdout.strip()
            self.assertEqual(after, head_now)
        finally:
            import shutil
            shutil.rmtree(webui.LIB / "ver-cache-probe", ignore_errors=True)
            webui.git(["add", "library"], cwd=webui.HUB)
            webui.git(["commit", "-m", "cleanup", "--allow-empty"], cwd=webui.HUB)
            webui._APP_VERSION_CACHE = None

    def test_check_without_origin_gives_guidance(self):
        webui.git(["remote", "remove", "origin"], cwd=webui.HUB)
        webui._APP_VERSION_CACHE = None
        r = webui.op_update_check({})
        self.assertFalse(r["ok"])
        self.assertIn("remote add origin", r["out"])

    def test_check_up_to_date_returns_no_commits(self):
        r = webui.op_update_check({})
        self.assertTrue(r["ok"], r.get("out"))
        self.assertEqual(r["commits"], [])

    def test_check_lists_new_upstream_commits(self):
        self.upstream_commit("v2\n")
        r = webui.op_update_check({})
        self.assertTrue(r["ok"], r.get("out"))
        self.assertEqual(len(r["commits"]), 1)
        self.assertIn("upstream: v2", r["commits"][0])

    def test_check_reports_new_release_tag(self):
        # 上游打了 tag 的更新,检查结果应给出新版本号
        self.upstream_commit("tagged\n")
        webui.git(["tag", "v9.9.9-test"], cwd=self.upstream)
        try:
            r = webui.op_update_check({})
            self.assertTrue(r["ok"], r.get("out"))
            self.assertEqual(r["latest"], "v9.9.9-test")
        finally:
            webui.git(["tag", "-d", "v9.9.9-test"], cwd=webui.HUB)
            webui._APP_VERSION_CACHE = None

    def test_app_version_reports_installed_tag(self):
        webui.git(["tag", "v0.0.1-test"], cwd=webui.HUB)
        webui._APP_VERSION_CACHE = None
        try:
            self.assertEqual(webui.app_version()["tag"], "v0.0.1-test")
        finally:
            webui.git(["tag", "-d", "v0.0.1-test"], cwd=webui.HUB)
            webui._APP_VERSION_CACHE = None

    def test_apply_merges_upstream_commit(self):
        self.upstream_commit("v3\n")
        r = webui.op_update_apply({})
        self.assertTrue(r["ok"], r["out"])
        self.assertEqual((webui.HUB / "MARKER.md").read_text(), "v3\n")

    def test_apply_when_up_to_date_is_noop(self):
        r = webui.op_update_apply({})
        self.assertTrue(r["ok"])

    def test_apply_refuses_dirty_worktree(self):
        self.upstream_commit("v4\n")
        marker = webui.HUB / "MARKER.md"
        orig = marker.read_text()
        marker.write_text("本地手改,不能被更新冲掉\n")
        try:
            r = webui.op_update_apply({})
            self.assertFalse(r["ok"])
            self.assertEqual(marker.read_text(), "本地手改,不能被更新冲掉\n",
                             "拒绝更新时绝不能动本地文件")
        finally:
            marker.write_text(orig)

    def test_apply_conflict_rolls_back_cleanly(self):
        # 本地与上游对同一文件做互相冲突的提交
        self.upstream_commit("upstream-line\n")
        (webui.HUB / "MARKER.md").write_text("local-line\n")
        webui.git(["add", "MARKER.md"], cwd=webui.HUB)
        webui.git(["commit", "-m", "local change"], cwd=webui.HUB)
        r = webui.op_update_apply({})
        self.assertFalse(r["ok"])
        self.assertIn("git merge", r["out"])
        self.assertEqual((webui.HUB / "MARKER.md").read_text(), "local-line\n",
                         "冲突回退后本地内容必须原样")
        st = webui.git(["status", "--porcelain"], cwd=webui.HUB)
        self.assertEqual(st.stdout.strip(), "", "回退后不能留下合并中间状态")


if __name__ == "__main__":
    unittest.main()
