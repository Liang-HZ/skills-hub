# Windows 启动/常驻支持的回归测试(在 macOS/Linux 上运行,平台判断走 mock)。
# 运行:  python3 -m unittest discover -s tests -v
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
# 与 test_pure_manager 同进程共存:只在未设置时指向临时目录,绝不触碰真实家目录
os.environ.setdefault(
    "SKILLS_HUB_ROOT", str(Path(tempfile.mkdtemp(prefix="hub-win-test-")) / "hub"))

sys.path.insert(0, str(REPO))
import webui  # noqa: E402

SRC = (REPO / "webui.py").read_text()


class FakeSh:
    """替身 sh():记录调用并返回预设退出码,绝不真跑 schtasks。"""
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, args, cwd=None, timeout=300):
        self.calls.append([str(a) for a in args])
        return subprocess.CompletedProcess(args, self.returncode, "", "")


class PurePathAndArgs(unittest.TestCase):
    """纯函数:pythonw 路径推导、命令拼装、schtasks 参数。"""

    def test_pythonw_swapped_in_when_present(self):
        d = Path(tempfile.mkdtemp(prefix="pyw-"))
        (d / "python.exe").write_text("")
        (d / "pythonw.exe").write_text("")
        self.assertEqual(webui.windows_pythonw(d / "python.exe"), str(d / "pythonw.exe"))

    def test_pythonw_missing_keeps_original(self):
        d = Path(tempfile.mkdtemp(prefix="pyw-"))
        (d / "python.exe").write_text("")
        self.assertEqual(webui.windows_pythonw(d / "python.exe"), str(d / "python.exe"))

    def test_non_python_exe_untouched(self):
        self.assertEqual(webui.windows_pythonw("/usr/bin/python3"), "/usr/bin/python3")

    def test_task_command_quotes_paths_and_no_open(self):
        cmd = webui.windows_task_command(
            r"C:\Program Files\Python\pythonw.exe", r"C:\skills hub\webui.py")
        self.assertEqual(
            cmd, '"C:\\Program Files\\Python\\pythonw.exe" "C:\\skills hub\\webui.py" --no-open')

    def test_task_command_custom_port(self):
        cmd = webui.windows_task_command("p.exe", "w.py", port=8800)
        self.assertTrue(cmd.endswith("--no-open --port 8800"))
        # 默认端口不追加 --port
        self.assertNotIn("--port", webui.windows_task_command("p.exe", "w.py"))

    def test_schtasks_args(self):
        self.assertEqual(
            webui.windows_schtasks_create_args("CMD"),
            ["schtasks", "/Create", "/F", "/SC", "ONLOGON",
             "/TN", webui.WIN_TASK_NAME, "/TR", "CMD"])
        self.assertEqual(
            webui.windows_schtasks_delete_args(),
            ["schtasks", "/Delete", "/F", "/TN", webui.WIN_TASK_NAME])
        self.assertEqual(
            webui.windows_schtasks_run_args(),
            ["schtasks", "/Run", "/TN", webui.WIN_TASK_NAME])


class PromptDecision(unittest.TestCase):
    """should_prompt_autostart 真值表:仅 Windows + 交互终端 + 从未选择过 才询问。"""

    def test_truth_table(self):
        f = webui.should_prompt_autostart
        self.assertTrue(f(True, True, {}))
        self.assertFalse(f(False, True, {}))                     # 非 Windows
        self.assertFalse(f(True, False, {}))                     # 非交互(pythonw/计划任务)
        self.assertFalse(f(True, True, {"choice": "yes"}))       # 已选过 yes
        self.assertFalse(f(True, True, {"choice": "no"}))        # 已选过 no
        self.assertTrue(f(True, True, {"other": 1}))             # 标记文件损坏/无 choice 视为未选


class MarkerFile(unittest.TestCase):
    """选择标记文件的读写往返。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="marker-"))
        self.orig = webui.WIN_AUTOSTART_MARKER
        webui.WIN_AUTOSTART_MARKER = self.tmp / ".state" / "windows-autostart.json"

    def tearDown(self):
        webui.WIN_AUTOSTART_MARKER = self.orig

    def test_roundtrip(self):
        self.assertEqual(webui.load_autostart_marker(), {})   # 不存在 → 空
        webui.save_autostart_marker("yes", "CMD X")
        m = webui.load_autostart_marker()
        self.assertEqual(m["choice"], "yes")
        self.assertEqual(m["command"], "CMD X")
        self.assertEqual(m["task"], webui.WIN_TASK_NAME)
        webui.save_autostart_marker("no")
        self.assertEqual(webui.load_autostart_marker()["choice"], "no")

    def test_corrupt_marker_is_empty(self):
        webui.WIN_AUTOSTART_MARKER.parent.mkdir(parents=True, exist_ok=True)
        webui.WIN_AUTOSTART_MARKER.write_text("not json")
        self.assertEqual(webui.load_autostart_marker(), {})


class InstallUninstall(unittest.TestCase):
    """--install/--uninstall-autostart:非 Windows 只提示;Windows(mock)下走 schtasks。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inst-"))
        self.orig_marker = webui.WIN_AUTOSTART_MARKER
        self.orig_sh = webui.sh
        webui.WIN_AUTOSTART_MARKER = self.tmp / "windows-autostart.json"

    def tearDown(self):
        webui.WIN_AUTOSTART_MARKER = self.orig_marker
        webui.sh = self.orig_sh

    def test_non_windows_refuses_without_side_effects(self):
        fake = FakeSh()
        webui.sh = fake
        self.assertEqual(webui.install_windows_autostart(), 1)
        self.assertEqual(webui.uninstall_windows_autostart(), 1)
        self.assertEqual(fake.calls, [])                          # 没跑任何命令
        self.assertFalse(webui.WIN_AUTOSTART_MARKER.exists())     # 没写标记

    def test_install_success_registers_and_marks(self):
        fake = FakeSh(returncode=0)
        webui.sh = fake
        with mock.patch.object(webui, "is_windows", return_value=True):
            self.assertEqual(webui.install_windows_autostart(port=8800), 0)
        self.assertEqual(len(fake.calls), 1)
        args = fake.calls[0]
        self.assertEqual(args[:5], ["schtasks", "/Create", "/F", "/SC", "ONLOGON"])
        self.assertIn(webui.WIN_TASK_NAME, args)
        tr = args[-1]
        self.assertIn("webui.py", tr)
        self.assertIn("--no-open", tr)
        self.assertIn("--port 8800", tr)
        self.assertEqual(webui.load_autostart_marker()["choice"], "yes")

    def test_install_failure_no_marker(self):
        webui.sh = FakeSh(returncode=1)
        with mock.patch.object(webui, "is_windows", return_value=True):
            self.assertEqual(webui.install_windows_autostart(), 1)
        self.assertFalse(webui.WIN_AUTOSTART_MARKER.exists())

    def test_uninstall_deletes_and_marks_no(self):
        fake = FakeSh(returncode=0)
        webui.sh = fake
        with mock.patch.object(webui, "is_windows", return_value=True):
            self.assertEqual(webui.uninstall_windows_autostart(), 0)
        self.assertEqual(fake.calls[0][:2], ["schtasks", "/Delete"])
        self.assertEqual(webui.load_autostart_marker()["choice"], "no")

    def test_uninstall_missing_task_still_marks_no(self):
        webui.sh = FakeSh(returncode=1)   # 任务不存在
        with mock.patch.object(webui, "is_windows", return_value=True):
            self.assertEqual(webui.uninstall_windows_autostart(), 0)
        self.assertEqual(webui.load_autostart_marker()["choice"], "no")


class InteractiveFlow(unittest.TestCase):
    """首次启动的 Y/N 询问流程(mock 平台/终端/输入,不真跑 schtasks、不开浏览器)。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ask-"))
        self.orig_marker = webui.WIN_AUTOSTART_MARKER
        self.orig_sh = webui.sh
        webui.WIN_AUTOSTART_MARKER = self.tmp / "windows-autostart.json"

    def tearDown(self):
        webui.WIN_AUTOSTART_MARKER = self.orig_marker
        webui.sh = self.orig_sh

    def _run(self, answer, sh_rc=0):
        webui.sh = FakeSh(returncode=sh_rc)
        with mock.patch.object(webui, "is_windows", return_value=True), \
                mock.patch.object(webui.sys, "stdin") as stdin, \
                mock.patch("builtins.input", return_value=answer), \
                mock.patch.object(webui.time, "sleep"), \
                mock.patch.object(webui.webbrowser, "open") as wb:
            stdin.isatty.return_value = True
            handed_off = webui.maybe_ask_windows_autostart(7799)
        return handed_off, webui.sh, wb

    def test_non_windows_never_prompts(self):
        with mock.patch("builtins.input", side_effect=AssertionError("不该询问")):
            self.assertFalse(webui.maybe_ask_windows_autostart())

    def test_answer_no_runs_foreground_and_remembers(self):
        handed_off, fake, wb = self._run("n")
        self.assertFalse(handed_off)
        self.assertEqual(fake.calls, [])
        self.assertEqual(webui.load_autostart_marker()["choice"], "no")
        wb.assert_not_called()

    def test_answer_yes_installs_runs_and_hands_off(self):
        handed_off, fake, wb = self._run("Y")
        self.assertTrue(handed_off)
        self.assertEqual([c[1] for c in fake.calls], ["/Create", "/Run"])
        self.assertEqual(webui.load_autostart_marker()["choice"], "yes")
        wb.assert_called_once_with("http://127.0.0.1:7799")

    def test_answer_yes_but_schtasks_fails_falls_back(self):
        handed_off, fake, wb = self._run("y", sh_rc=1)
        self.assertFalse(handed_off)                              # 回退前台
        wb.assert_not_called()

    def test_remembered_choice_not_asked_again(self):
        webui.save_autostart_marker("no")
        with mock.patch.object(webui, "is_windows", return_value=True), \
                mock.patch("builtins.input", side_effect=AssertionError("不该再问")):
            self.assertFalse(webui.maybe_ask_windows_autostart())


class SourceAndLauncher(unittest.TestCase):
    """源码不变量 + Windows 启动脚本本体检查。"""

    def test_pythonw_none_stdout_guard_present(self):
        # pythonw 下 sys.stdout/stderr 为 None 的防御必须在 import usage_log 之前
        self.assertIn("if sys.stdout is None:", SRC)
        self.assertIn("if sys.stderr is None:", SRC)
        self.assertLess(SRC.index("sys.stdout is None"), SRC.index("import usage_log"))

    def test_main_wires_autostart_flags(self):
        self.assertIn("--install-autostart", SRC)
        self.assertIn("--uninstall-autostart", SRC)
        self.assertIn("maybe_ask_windows_autostart", SRC)

    def test_bat_is_ascii_crlf_and_probes_all_interpreters(self):
        raw = (REPO / "start-windows.bat").read_bytes()
        raw.decode("ascii")                                       # 纯 ASCII,任何代码页都不乱码
        self.assertNotIn(b"\r\r", raw)
        for line in raw.split(b"\r\n")[:-1]:
            self.assertNotIn(b"\n", line)                         # 所有换行都是 CRLF
        text = raw.decode("ascii")
        for probe in ("py -3 -c", "python -c", "python3 -c"):
            self.assertIn(probe, text)
        self.assertIn("webui.py", text)
        self.assertIn("python.org", text)                         # 安装指引

    def test_gitattributes_keeps_bat_crlf(self):
        self.assertIn("*.bat text eol=crlf", (REPO / ".gitattributes").read_text())


if __name__ == "__main__":
    unittest.main()
