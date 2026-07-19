# 启用状态 profile(多机同步的开关快照/恢复所依赖)的回归测试。
# 运行:  python3 -m unittest discover -s tests -v
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="hub-upstream-test-"))
os.environ.setdefault("SKILLS_HUB_ROOT", str(TMP / "hub"))

sys.path.insert(0, str(REPO))
import webui  # noqa: E402  (必须在设好 SKILLS_HUB_ROOT 之后导入)


class UpstreamTests(unittest.TestCase):
    """ROOTS 在 setUp 里指向本用例的临时目录、tearDown 恢复,不影响其他测试模块。"""

    def setUp(self):
        webui.ensure_hub()
        self.work = Path(tempfile.mkdtemp(dir=TMP))
        self._old_roots = dict(webui.ROOTS)
        for k in webui.KINDS:
            webui.ROOTS[k] = self.work / f"home/.{k}/skills"
        self._made = []

    def tearDown(self):
        webui.ROOTS.update(self._old_roots)
        for name in self._made:
            shutil.rmtree(webui.LIB / name, ignore_errors=True)
        webui.git(["add", "library"], cwd=webui.HUB)
        webui.git(["commit", "-m", "test cleanup", "--allow-empty"], cwd=webui.HUB)

    def make(self, name):
        d = webui.LIB / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 测试技能\n---\n正文\n")
        self._made.append(name)
        webui.git_commit(f"test add {name}")
        return d

    # ---- profile 导出 ----

    def test_profile_empty_when_nothing_enabled(self):
        self.make("ups-idle")
        prof = webui.api_profile()["profile"]
        self.assertNotIn("ups-idle", prof["global"])
        self.assertEqual(prof["version"], 1)

    def test_profile_lists_globally_enabled_kinds(self):
        self.make("ups-on")
        webui.links_enable("claude", ["ups-on"])
        webui.links_enable("agents", ["ups-on"])
        prof = webui.api_profile()["profile"]
        self.assertEqual(sorted(prof["global"]["ups-on"]), ["agents", "claude"])

    # ---- profile 导入 ----

    def test_apply_profile_enables_listed_kinds(self):
        self.make("ups-apply")
        r = webui.op_profile_apply({"profile": {"global": {"ups-apply": ["claude"]}}})
        self.assertTrue(r["ok"], r["out"])
        st = webui.entry_state(webui.ROOTS["claude"] / "ups-apply", "ups-apply")
        self.assertEqual(st, "hub-link")

    def test_apply_profile_skips_missing_skills(self):
        r = webui.op_profile_apply({"profile": {"global": {"no-such-skill": ["claude"]}}})
        self.assertTrue(r["ok"])
        self.assertIn("no-such-skill", r["out"])

    def test_apply_profile_rejects_bad_shape(self):
        r = webui.op_profile_apply({"profile": {"global": "not-a-dict"}})
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
