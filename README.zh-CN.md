# Skills Hub · 技能库

**你装了一堆 agent 技能。到底哪些真的被触发过?**

Skills Hub 把本机所有 AI agent 技能收进一个本地库,并用真实数字回答这个问题——它读 Claude Code、Codex、OpenCode 各自的本地会话记录,数出每个技能实际被触发了多少次。开关、组合、更新都在同一个本地网页里完成。

[打开在线模拟 →](https://skills.liangai.org) · [English →](README.md)

![Skills Hub 用量分析:每个技能在 Claude Code / Codex / OpenCode 上的真实触发次数,从未触发过的技能一目了然](docs/screenshot.png)

所有技能(带 `SKILL.md` 的文件夹)的唯一真源放在本机的 `library/`,再链接到各个 agent 找技能的地方——Claude Code(`~/.claude/skills`)、Codex(`~/.codex/skills`)、通用 Agents(`~/.agents/skills`)或任意项目目录。改一处、处处生效;关掉开关只是摘链接,技能永远安全地留在库里。

- **真实触发次数,不是估算**——每个技能今天/近7天/近30天/累计的用量,可按 agent 拆分,直接读各 agent 自己的本地记录。从没用过的技能会自己沉到列表底部。
- **一个库,喂所有 agent**——Claude Code、Codex、OpenCode,或任何会读技能目录的工具;全局或按项目都行。
- **单文件,无需 npm/pip 安装**--只需 Python 3.9+ 和 Git。一个文件,一条命令。未安装 Git 时会显示安装引导。
- **纯本地**——只监听 `127.0.0.1:7799`,不上传任何数据,所有数据源只读不改。
- **步步可回退**——每次变更自动进本地 git 历史;删除只进回收站,从不真删。
- **跨平台 + 中英双语界面**——macOS / Linux 用软链接;Windows 依次尝试 symlink → junction → 副本;自动按浏览器语言切换。

## 快速开始

```bash
git clone https://github.com/Liang-HZ/skills-hub.git
cd skills-hub
python3 webui.py          # 自动打开 http://127.0.0.1:7799
```

Windows:双击 `start-windows.bat`(或 `py webui.py`)。
可选桌面窗口:`pip install pywebview && python3 desktop.py`。

页面三句话就能看懂:

1. 所有技能都保存在这台电脑的技能库里,删不丢、改全生效。
2. 每个技能下面一排开关,拨绿 = 在那里能用(Claude 全局 / Codex 全局 / 某个项目)。
3. 不点带"联网"字样的按钮,它绝不碰网络。

## 功能

| 标签页 | 用来做什么 |
|-------|-----------|
| 技能 | 新建、编辑、导入、收编本机散装技能(支持系统目录选择框或手动填路径);控制每个技能在哪里启用 |
| 组合 | 把常一起用的技能存成一组,一键整组开关 |
| 使用情况 | 每个地方(全局/各项目)各自开了什么,一键清理失效项目 |
| 用量分析 | 按引用数、按真实触发次数排行,可切今天 / 近7天 / 近30天 / 累计 |
| 网上来源 | 把第三方技能仓库克隆进隔离区,挑着引入快照,手动跟进更新 |
| 设置 | 行为选项 |

### 用量统计:数字从哪来

两个互相独立的口径,全部在本地算出——不上传任何东西,所有数据源都只读不改:

- **引用数** —— 这个技能当前在多少处是开着的(全局 + 各项目)。由开关状态实时算出,不额外存储。
- **触发次数** —— 技能真正被调用了多少次。增量扫描各 agent 自己写的本地会话记录,聚合进 `.state/usage.sqlite3`(Python 标准库 `sqlite3`,零新增依赖)。各家信号可靠度不同,所以把来源摊开写清楚,而不是混成一个数:

| Agent | 数据源 | 信号 |
|-------|-------|------|
| Claude Code | `~/.claude/projects/**/*.jsonl` | 精确 —— 每次调用都有一条结构化 `Skill` 工具记录 |
| OpenCode | `~/.local/share/opencode/opencode.db` | 精确 —— 官方内置 `skill` 工具调用 |
| Codex | `~/.codex/sessions/**/*.jsonl` | **启发式** —— Codex 没有专门的技能工具,这里数的是命令里出现 `.../<技能名>/SKILL.md` 路径的次数。比前两者粗糙(「读一眼」和「照着做」在日志里长得一样) |
| Cursor | —— | 暂不支持:它的本地存储是社区逆向出来的、官方不公开的 `state.vscdb` 格式,版本升级可能导致统计静默失效 |

扫描是增量的(日志文件按字节偏移、OpenCode 按 sqlite rowid 高水位),不会重复计数,也不会把没写完的半条记录当脏数据。首次要扫一遍已有历史,可能要几秒;之后基本秒开。

### 第三方技能的主权模型

第三方仓库克隆到 `vendor/<源>/`——一个**永不直接生效**的惰性收件箱。引入 = 把当时的内容复制成快照进库。更新是**两次独立授权**:

1. **检查**——此时才 `git fetch`。页面列出新提交、哪些已引入技能的哪些文件变了,并签发一枚绑定"该来源 + 该目标提交"的一次性短期令牌。
2. **更新**——消费令牌,只快进到你刚看过的那个提交,不会背着你重新解析"最新版"。

管理器发起的所有 git 命令都使用独立空 `core.hooksPath`,任何仓库或全局 git hook 都无法把管理动作变成代码执行。

### 它刻意不做什么

Skills Hub 是管理器,不是安全扫描器:

- **不判断**技能是否安全,永不执行技能自带的脚本、安装器、示例。
- **不自动下载**任何东西——来源、更新、依赖、模型都不会。所有联网动作都在明确标注"联网"的按钮后面。
- 它把差异和来历摆给你看,决定权在你。**第三方技能启用前请自行阅读。**

写 API 在后端强制校验(loopback Host + 同源 + JSON Content-Type + 会话 CSRF 令牌),恶意网页无法驱动你的管理器。

## 数据布局

```
library/               你的技能(唯一真源)
library/.origins.json  每个技能的来历(自建/跟随更新/独立副本)
sets/                  组合,一行一个技能名
vendor/                第三方仓库隔离区(永不直接生效)
targets.txt            用过技能的项目目录注册表
attic/trash/           "删除"实际去的地方
usage_log.py           触发次数扫描(读各 agent 会话记录,聚合进 .state/)
.state/                本地派生数据(用量统计缓存),已 gitignore,可随时重建
site/                  skills.liangai.org 上的在线模拟板(编译产物,不是手写的)
tools/build_demo.py    把 webui.py 的真实界面编译成那个模拟板(后端换成浏览器内存假数据)
```

技能与组合的每次变更自动提交进本地 git(只提交 `library/` 和 `sets/`),这就是你的撤销路径。想把数据放在代码目录之外:设置 `SKILLS_HUB_ROOT=/你的数据目录`,应用会在那里单独初始化数据仓库,升级时 `git pull` 毫无纠缠。

## 开机自启

macOS(launchd)、Linux(systemd)、Windows(任务计划程序)配置见 [docs/autostart.md](docs/autostart.md)。

## 命令行(macOS/Linux)

`skillctl` 是开关的 bash 等价物:`skillctl list | sets | status | enable <目标> <技能|@组合> | disable | add | new`。

## 测试

```bash
python3 -m unittest discover -s tests
```

回归测试钉死产品边界:无代码执行路径、点击之外无网络、令牌门禁的更新、CSRF 强制校验。

## 许可

[MIT](LICENSE)
