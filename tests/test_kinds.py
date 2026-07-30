# 目录族(KINDS)的回归测试:新增一族(zcode / workbuddy)后,发现·放置·前端渲染都跟着走。
# 运行:  python3 -m unittest discover -s tests -v
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="hub-kinds-test-"))
os.environ.setdefault("SKILLS_HUB_ROOT", str(TMP / "hub"))

sys.path.insert(0, str(REPO))
import webui  # noqa: E402  (必须在设好 SKILLS_HUB_ROOT 之后导入)

NEW_KINDS = ("zcode", "workbuddy")


class KindsTests(unittest.TestCase):
    """全局放置点一律指进临时目录,测试绝不触碰真实家目录。"""

    @classmethod
    def setUpClass(cls):
        webui.ensure_hub()

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=TMP))
        self._old_roots = dict(webui.ROOTS)
        for k in webui.KINDS:
            webui.ROOTS[k] = self.home / f".{k}/skills"
        self._made = []

    def tearDown(self):
        webui.ROOTS.update(self._old_roots)
        for name in self._made:
            shutil.rmtree(webui.LIB / name, ignore_errors=True)
            webui.set_origin(name, None)

    def make_skill(self, name):
        d = webui.LIB / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 测试\n---\n正文\n")
        self._made.append(name)
        return d

    def test_kinds_registered_with_home_roots(self):
        for k in NEW_KINDS:
            self.assertIn(k, webui.KINDS)
            self.assertIn(k, webui.KIND_META)
        self.assertEqual(webui.resolve_place("zcode"), self.home / ".zcode/skills")
        self.assertEqual(webui.resolve_place("/p::workbuddy"), Path("/p/.workbuddy/skills"))

    def test_toggle_places_link_in_new_kind(self):
        self.make_skill("kind-skill")
        for k in NEW_KINDS:
            r = webui.op_toggle({"target": k, "skill": "kind-skill", "on": True})
            self.assertTrue(r["ok"], r)
            entry = webui.ROOTS[k] / "kind-skill"
            self.assertIsNotNone(webui.read_link(entry), f"{k} 应放的是链接")
            webui.op_toggle({"target": k, "skill": "kind-skill", "on": False})
            self.assertEqual(webui.entry_state(entry, "kind-skill"), "absent")

    def test_scan_finds_unmanaged_skill_in_new_kind(self):
        for k in NEW_KINDS:
            d = webui.ROOTS[k] / f"loose-{k}"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: loose-{k}\ndescription: x\n---\n")
        found = webui.op_scan_local({"scope": "global"})["found"]
        by_target = {f["target"]: f["name"] for f in found}
        for k in NEW_KINDS:
            self.assertEqual(by_target.get(k), f"loose-{k}", f"{k} 下的库外技能应被扫到")

    def test_in_skill_root_covers_new_kinds(self):
        for k in NEW_KINDS:
            self.assertTrue(webui.in_skill_root(Path(f"/x/.{k}/skills/some-skill")))
        self.assertFalse(webui.in_skill_root(Path("/x/.zcodeish/skills/some-skill")))

    def test_state_exposes_kind_list_for_frontend(self):
        webui.ROOTS["zcode"].mkdir(parents=True)
        st = webui.api_state()
        kinds = {x["kind"]: x for x in st["kinds"]}
        for k in webui.KINDS:
            self.assertIn(k, kinds)
            self.assertEqual(kinds[k]["root"], f"~/.{k}/skills")
        self.assertTrue(st["kind_roots"]["zcode"])          # 目录存在 → 界面上露出
        self.assertFalse(st["kind_roots"]["workbuddy"])     # 没这个目录 → 不占版面
        self.assertTrue(kinds["claude"]["always"])          # claude/codex 一直显示

    def test_page_has_labels_for_every_kind(self):
        for k in webui.KINDS:
            for key in (f"pill_{k}", f"cc_{k}", f"radio_{k}"):
                self.assertIn(f"{key}:", webui.PAGE, f"缺少 {key} 文案(中英各一份)")
                self.assertEqual(webui.PAGE.count(f"{key}:"), 2, f"{key} 应中英各一份")

    def test_page_does_not_hardcode_kind_triples(self):
        # 前端一律从 S.kinds 取目录族;再出现写死的三件套就说明新增族会被漏掉
        for token in ('["claude","codex","agents"]', "GLOBAL_SKILL_DIRS"):
            self.assertNotIn(token, webui.PAGE, f"前端不应再写死 {token}")


if __name__ == "__main__":
    unittest.main()
