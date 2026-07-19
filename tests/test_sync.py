# 备份·多机同步(直接同步 hub 仓库本身)的回归测试。
# 运行:  python3 -m unittest discover -s tests -v
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="hub-sync-test-"))
os.environ.setdefault("SKILLS_HUB_ROOT", str(TMP / "hub"))

sys.path.insert(0, str(REPO))
import webui  # noqa: E402  (必须在设好 SKILLS_HUB_ROOT 之后导入)


class SyncTests(unittest.TestCase):
    """私有仓库用本地裸仓库模拟;每个用例自带独立裸仓库与(可选)另一台电脑克隆。"""

    @classmethod
    def setUpClass(cls):
        webui.ensure_hub()
        if not (webui.HUB / "SYNC_MARKER.md").exists():
            (webui.HUB / "SYNC_MARKER.md").write_text("base\n")
            webui.git(["add", "SYNC_MARKER.md"], cwd=webui.HUB)
            webui.git(["commit", "-m", "sync marker"], cwd=webui.HUB)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=TMP))
        self.bare = self.work / "private.git"
        webui.git(["init", "--bare", str(self.bare)])
        self._old_roots = dict(webui.ROOTS)
        for k in webui.KINDS:
            webui.ROOTS[k] = self.work / f"home/.{k}/skills"
        self._made = []
        webui._APP_VERSION_CACHE = None

    def tearDown(self):
        webui.ROOTS.update(self._old_roots)
        webui.git(["remote", "remove", webui.SYNC_REMOTE], cwd=webui.HUB)
        for name in self._made:
            shutil.rmtree(webui.LIB / name, ignore_errors=True)
        if webui.PROFILE_FILE.exists():
            webui.PROFILE_FILE.unlink()
        webui.git(["add", "-A"], cwd=webui.HUB)
        webui.git(["commit", "-m", "test cleanup", "--allow-empty"], cwd=webui.HUB)
        webui._APP_VERSION_CACHE = None

    def make_skill(self, name):
        d = webui.LIB / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 测试\n---\n正文\n")
        self._made.append(name)
        webui.git_commit(f"test add {name}")

    def bind(self):
        r = webui.op_sync_bind({"url": str(self.bare)})
        self.assertTrue(r["ok"], r["out"])
        return r

    # ---- 绑定 ----

    def test_bind_pushes_current_branch(self):
        self.bind()
        branch = webui.app_version()["branch"]
        log = webui.git(["log", "--oneline", branch], cwd=self.bare)
        self.assertEqual(log.returncode, 0)
        self.assertTrue(log.stdout.strip(), "裸仓库应收到首次推送")

    def test_bind_bad_url_fails_cleanly(self):
        r = webui.op_sync_bind({"url": str(self.work / "does-not-exist.git")})
        self.assertFalse(r["ok"])

    def test_sync_without_bind_gives_guidance(self):
        r = webui.op_sync_now({})
        self.assertFalse(r["ok"])

    # ---- 推与拉 ----

    def test_sync_pushes_local_commits(self):
        self.bind()
        self.make_skill("sync-push-skill")
        r = webui.op_sync_now({})
        self.assertTrue(r["ok"], r["out"])
        branch = webui.app_version()["branch"]
        log = webui.git(["log", "--oneline", branch], cwd=self.bare).stdout
        self.assertIn("test add sync-push-skill", log)

    def test_sync_pulls_other_machine_commits(self):
        self.bind()
        other = self.work / "machine-b"
        webui.git(["clone", str(self.bare), str(other)])
        (other / "library" / "from-b-skill").mkdir(parents=True)
        (other / "library" / "from-b-skill" / "SKILL.md").write_text(
            "---\nname: from-b-skill\ndescription: B 机建的\n---\n正文\n")
        webui.git(["add", "library"], cwd=other)
        webui.git(["-c", "user.email=b@x", "-c", "user.name=b",
                   "commit", "-m", "webui: 新建 from-b-skill"], cwd=other)
        webui.git(["push", "origin", "HEAD"], cwd=other)
        self._made.append("from-b-skill")
        r = webui.op_sync_now({})
        self.assertTrue(r["ok"], r["out"])
        self.assertTrue((webui.LIB / "from-b-skill" / "SKILL.md").exists(), "B 机的技能应同步过来")

    def test_sync_snapshots_profile_into_repo(self):
        self.bind()
        r = webui.op_sync_now({})
        self.assertTrue(r["ok"], r["out"])
        self.assertTrue(webui.PROFILE_FILE.exists())
        st = webui.git(["status", "--porcelain", "--", "skills-profile.json"], cwd=webui.HUB)
        self.assertEqual(st.stdout.strip(), "", "开关状态快照应已提交进仓库")

    def test_sync_conflict_rolls_back_cleanly(self):
        self.bind()
        # B 机与本机对同一文件做互相冲突的提交
        other = self.work / "machine-b"
        webui.git(["clone", str(self.bare), str(other)])
        (other / "SYNC_MARKER.md").write_text("b-line\n")
        webui.git(["-c", "user.email=b@x", "-c", "user.name=b",
                   "commit", "-am", "b change"], cwd=other)
        webui.git(["push", "origin", "HEAD"], cwd=other)
        (webui.HUB / "SYNC_MARKER.md").write_text("a-line\n")
        webui.git(["add", "SYNC_MARKER.md"], cwd=webui.HUB)
        webui.git(["commit", "-m", "a change"], cwd=webui.HUB)
        r = webui.op_sync_now({})
        self.assertFalse(r["ok"])
        self.assertEqual((webui.HUB / "SYNC_MARKER.md").read_text(), "a-line\n",
                         "冲突回退后本机内容必须原样")
        st = webui.git(["status", "--porcelain", "--untracked-files=no"], cwd=webui.HUB)
        self.assertEqual(st.stdout.strip(), "", "回退后不能留下合并中间态")

    # ---- 两条水路互不串:origin(开源更新源) vs backup(私有同步) ----

    def test_sync_never_pushes_to_origin(self):
        # 用户的担心:同步会不会把私有内容推到开源仓库?——绝不。
        public = self.work / "public-upstream.git"
        webui.git(["init", "--bare", str(public)])
        webui.git(["remote", "add", "origin", str(public)], cwd=webui.HUB)
        webui._APP_VERSION_CACHE = None
        try:
            self.bind()
            self.make_skill("private-secret-skill")
            r = webui.op_sync_now({})
            self.assertTrue(r["ok"], r["out"])
            heads = webui.git(["branch", "-a"], cwd=public).stdout.strip()
            self.assertEqual(heads, "", "开源仓库(origin)必须一个字节都收不到")
            branch = webui.app_version()["branch"]
            log = webui.git(["log", "--oneline", branch], cwd=self.bare).stdout
            self.assertIn("private-secret-skill", log, "私有内容只应出现在 backup 私仓")
        finally:
            webui.git(["remote", "remove", "origin"], cwd=webui.HUB)
            webui._APP_VERSION_CACHE = None

    def test_app_update_and_private_sync_coexist(self):
        # 完整共存链路:上游发代码更新(origin) + 用户私有同步(backup),互不干扰
        public_src = self.work / "public-src"
        webui.git(["clone", str(webui.HUB), str(public_src)])
        marker = public_src / "APP_CODE.md"
        marker.write_text("上游新功能\n")
        webui.git(["add", "APP_CODE.md"], cwd=public_src)
        webui.git(["-c", "user.email=up@x", "-c", "user.name=up",
                   "commit", "-m", "webui: 上游功能更新"], cwd=public_src)
        webui.git(["remote", "add", "origin", str(public_src)], cwd=webui.HUB)
        webui._APP_VERSION_CACHE = None
        try:
            self.bind()
            self.make_skill("user-daily-skill")          # 用户自己的私有改动
            r = webui.op_update_apply({})                 # 拉开源功能更新
            self.assertTrue(r["ok"], r["out"])
            self.assertTrue((webui.HUB / "APP_CODE.md").exists(), "功能更新应到位")
            self.assertTrue((webui.LIB / "user-daily-skill").exists(), "用户技能不受影响")
            r = webui.op_sync_now({})                     # 再同步到私仓
            self.assertTrue(r["ok"], r["out"])
            branch = webui.app_version()["branch"]
            log = webui.git(["log", "--oneline", branch], cwd=self.bare).stdout
            self.assertIn("上游功能更新", log, "私仓应带上已合并的功能更新")
            self.assertIn("user-daily-skill", log, "私仓应带上用户自己的提交")
        finally:
            webui.git(["remote", "remove", "origin"], cwd=webui.HUB)
            (webui.HUB / "APP_CODE.md").unlink(missing_ok=True)
            webui.git(["add", "-A"], cwd=webui.HUB)
            webui.git(["commit", "-m", "test cleanup appcode", "--allow-empty"], cwd=webui.HUB)
            webui._APP_VERSION_CACHE = None

    # ---- 开关状态恢复 ----

    def test_profile_restore_without_file_gives_guidance(self):
        r = webui.op_profile_restore({})
        self.assertFalse(r["ok"])

    def test_profile_restore_applies_recorded_toggles(self):
        self.make_skill("restore-skill")
        webui.save_json(webui.PROFILE_FILE, {"version": 1, "global": {"restore-skill": ["claude"]}})
        r = webui.op_profile_restore({})
        self.assertTrue(r["ok"], r["out"])
        st = webui.entry_state(webui.ROOTS["claude"] / "restore-skill", "restore-skill")
        self.assertEqual(st, "hub-link")


if __name__ == "__main__":
    unittest.main()
