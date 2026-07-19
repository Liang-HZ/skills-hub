# 完整性检查(结构 lint)回归测试:只报结构事实,不判断内容。
# 运行:  python3 -m unittest discover -s tests -v
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="hub-lint-test-"))
os.environ.setdefault("SKILLS_HUB_ROOT", str(TMP / "hub"))

sys.path.insert(0, str(REPO))
import webui  # noqa: E402  (必须在设好 SKILLS_HUB_ROOT 之后导入)


def issues_for(name):
    return [i for i in webui.api_lint()["issues"] if i["skill"] == name]


def kinds_for(name):
    return {i["kind"] for i in issues_for(name)}


class LintTests(unittest.TestCase):
    """每个用例用唯一技能名,结束即删,不影响共享 LIB 里的其他测试。"""

    @classmethod
    def setUpClass(cls):
        webui.ensure_hub()

    def setUp(self):
        self._made = []

    def tearDown(self):
        for name in self._made:
            shutil.rmtree(webui.LIB / name, ignore_errors=True)
            webui.set_origin(name, None)
        webui.git(["add", "library"], cwd=webui.HUB)
        webui.git(["commit", "-m", "test cleanup", "--allow-empty"], cwd=webui.HUB)

    def make(self, name, content, commit=True, extra_files=None):
        d = webui.LIB / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(content)
        for rel in extra_files or []:
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        self._made.append(name)
        if commit:
            webui.git_commit(f"test add {name}")
        return d

    @staticmethod
    def good(name, desc="一个用于测试的技能描述"):
        return f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n正文。\n"

    def test_good_skill_has_no_issues(self):
        self.make("lint-good", self.good("lint-good"))
        self.assertEqual(issues_for("lint-good"), [])

    def test_missing_frontmatter(self):
        self.make("lint-nofm", "# 没有 frontmatter\n直接正文。\n")
        self.assertIn("fm_missing", kinds_for("lint-nofm"))

    def test_unterminated_frontmatter_counts_as_missing(self):
        self.make("lint-badfm", "---\nname: lint-badfm\n没有闭合的头\n")
        self.assertIn("fm_missing", kinds_for("lint-badfm"))

    def test_name_mismatch(self):
        self.make("lint-mismatch", self.good("other-name"))
        ks = issues_for("lint-mismatch")
        self.assertIn("name_mismatch", {i["kind"] for i in ks})
        self.assertEqual([i["detail"] for i in ks if i["kind"] == "name_mismatch"], ["other-name"])

    def test_missing_description(self):
        self.make("lint-nodesc", "---\nname: lint-nodesc\n---\n正文\n")
        self.assertIn("desc_missing", kinds_for("lint-nodesc"))

    def test_overlong_description(self):
        self.make("lint-longdesc", self.good("lint-longdesc", desc="很长" * 600))
        self.assertIn("desc_long", kinds_for("lint-longdesc"))

    def test_dead_relative_link_reported(self):
        body = self.good("lint-deadlink") + "\n见 [参考](reference/missing.md)。\n"
        self.make("lint-deadlink", body)
        ks = issues_for("lint-deadlink")
        self.assertEqual([i["detail"] for i in ks if i["kind"] == "dead_link"],
                         ["reference/missing.md"])

    def test_existing_link_and_external_links_not_reported(self):
        body = (self.good("lint-oklink")
                + "\n[有的](reference/ok.md) [外链](https://example.com/x.md) [锚点](#sec)\n")
        self.make("lint-oklink", body, extra_files=["reference/ok.md"])
        self.assertNotIn("dead_link", kinds_for("lint-oklink"))

    def test_uncommitted_local_edit_reported_as_dirty(self):
        d = self.make("lint-dirty", self.good("lint-dirty"))
        (d / "SKILL.md").write_text(self.good("lint-dirty") + "\n手改了一行。\n")
        ks = issues_for("lint-dirty")
        self.assertIn("dirty", {i["kind"] for i in ks})
        self.assertNotIn("dirty_ref", {i["kind"] for i in ks})

    def test_ref_skill_local_edit_reported_as_dirty_ref(self):
        d = self.make("lint-refdirty", self.good("lint-refdirty"))
        webui.set_origin("lint-refdirty", {"type": "ref", "source": "some-src",
                                            "subpath": "lint-refdirty", "commit": "abc1234"})
        (d / "SKILL.md").write_text(self.good("lint-refdirty") + "\n手改。\n")
        self.assertIn("dirty_ref", kinds_for("lint-refdirty"))

    def test_committed_skill_not_dirty(self):
        self.make("lint-clean", self.good("lint-clean"))
        ks = kinds_for("lint-clean")
        self.assertNotIn("dirty", ks)
        self.assertNotIn("dirty_ref", ks)


class ParseFrontmatterTests(unittest.TestCase):
    def test_basic(self):
        fm = webui.parse_frontmatter("---\nname: a\ndescription: b\n---\nbody")
        self.assertEqual(fm, {"name": "a", "description": "b"})

    def test_no_frontmatter(self):
        self.assertIsNone(webui.parse_frontmatter("# just body"))

    def test_unterminated(self):
        self.assertIsNone(webui.parse_frontmatter("---\nname: a\nbody"))

    def test_nested_lines_ignored(self):
        fm = webui.parse_frontmatter("---\nname: a\nmetadata:\n  type: x\n---\n")
        self.assertEqual(fm.get("name"), "a")
        self.assertIn("metadata", fm)


if __name__ == "__main__":
    unittest.main()
