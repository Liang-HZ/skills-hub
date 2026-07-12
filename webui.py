#!/usr/bin/env python3
"""skills-hub 可视化管理台 — 纯 Skill 管理器。

启动:  python3 webui.py            (加 --no-open 不自动开浏览器,--port 换端口)
页面:  http://127.0.0.1:7799

设计原则:零理解成本——用户只需要知道"技能住在库里,开关拨绿=在那里能用"。
软链接、真源、挂载这些实现概念不出现在界面上。

产品边界(纯管理器):
  只管理 Skill 的本地存储、来源、组合、启用位置与手动更新。
  不判断 Skill 是否安全,不调用模型审核,不执行 Skill 自带脚本,
  不自动下载任何东西。所有联网动作(下载来源/检查更新/执行更新)
  都只在用户明确点击后发生,且检查与更新是两次独立授权:
  检查产生一枚绑定"来源+目标提交"的一次性短期令牌,更新只能消费该令牌。

主权模型:
  vendor/<源>/   外部仓库的惰性收件箱,怎么更新都不会直接生效
  library/       唯一真源;外部技能在这里是物化快照,不是软链接
  所有对外链接始终指向 library/。
"""
import filecmp
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

HUB = Path(os.environ.get("SKILLS_HUB_ROOT") or Path(__file__).resolve().parent)
LIB = HUB / "library"
SETS = HUB / "sets"
VENDOR = HUB / "vendor"
TARGETS_FILE = HUB / "targets.txt"
ORIGINS_FILE = LIB / ".origins.json"
UI_CONF_FILE = HUB / "config" / "ui.json"
NO_HOOKS_DIR = HUB / ".state" / "no-hooks"   # 空目录:管理器发起的 git 一律不跑任何 hook
PORT = 7799
SERVER_PORT = PORT
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
KINDS = ("claude", "codex", "agents")   # 技能可放置的目录族:.claude / .codex / .agents
ROOTS = {k: Path.home() / f".{k}/skills" for k in KINDS}

# 写 API 的会话令牌:每次启动随机生成,只随页面下发,POST 必须带上(防跨站)
CSRF_TOKEN = secrets.token_hex(16)
# 来源更新的一次性令牌:检查更新时签发,绑定 来源+目标提交,更新时消费
UPDATE_TOKENS = {}
UPDATE_TOKEN_TTL = 600


# ---------- 基础 ----------

def sh(args, cwd=None, timeout=300):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def git(args, cwd=None, timeout=300):
    """管理器发起的所有 git 都走这里:hooksPath 指向独立空目录,
    仓库/全局/模板里的任何 git hook 都不会被本地管理动作触发。"""
    NO_HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    return sh(["git", "-c", f"core.hooksPath={NO_HOOKS_DIR}", *args], cwd=cwd, timeout=timeout)


def load_json(f: Path, default):
    try:
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def save_json(f: Path, obj):
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(obj, ensure_ascii=False, indent=2))


def git_commit(msg):
    # 只提交技能内容(library/ + sets/),不用 -A:免得把工作区里无关改动裹进自动提交
    git(["add", "library", "sets"], cwd=HUB)
    git(["commit", "-m", f"webui: {msg}"], cwd=HUB)


def read_link(p: Path):
    """软链接/Windows junction 的目标;不是链接返回 None。"""
    try:
        return os.readlink(p)
    except (OSError, ValueError):
        return None


def dirs_equal(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    if mismatch or errors:
        return False
    return all(dirs_equal(a / d, b / d) for d in cmp.common_dirs)


def entry_state(p: Path, name: str) -> str:
    link = read_link(p)
    if link is not None:
        if not p.exists():
            return "broken-link"
        return "hub-link" if str(Path(link)).startswith(str(LIB)) else "foreign-link"
    if p.is_dir():
        if (LIB / name).is_dir():
            return "copy-synced" if dirs_equal(LIB / name, p) else "copy-diverged"
        return "local-dir"
    return "absent"


def parse_desc(text: str) -> str:
    """从 SKILL.md 头部抽 description,支持 YAML 折叠写法(description: >- 后跟缩进行)。"""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, l in enumerate(lines[1:], 1):
            if l.strip() == "---":
                lines = lines[1:i]
                break
    for i, line in enumerate(lines):
        if line.startswith("description:"):
            val = line[len("description:"):].strip().strip("\"'")
            if val in (">", ">-", ">+", "|", "|-", "|+"):
                block = []
                for cont in lines[i + 1:]:
                    if cont[:1] in (" ", "\t"):
                        block.append(cont.strip())
                    elif not cont.strip() and block:
                        break  # 折叠块里的空行 = 段落结束,一行摘要够用了
                    elif cont.strip():
                        break
                return " ".join(block)
            return val
    return ""


def desc_of(name: str) -> str:
    try:
        return parse_desc((LIB / name / "SKILL.md").read_text())
    except OSError:
        return ""


def open_in_file_manager(p: Path):
    if sys.platform == "darwin":
        sh(["open", str(p)])
    elif os.name == "nt":
        os.startfile(str(p))  # noqa: 仅 Windows
    else:
        sh(["xdg-open", str(p)])


# ---------- 链接与放置(原 skillctl 的核心操作,纯 Python 跨平台实现) ----------

def resolve_place(target: str) -> Path:
    if target in KINDS:
        return ROOTS[target]
    proj, _, kind = target.partition("::")
    return Path(proj).expanduser() / f".{kind or 'claude'}" / "skills"


def make_link(entry: Path, src: Path) -> str:
    """优先软链接;Windows 无权限时退到 junction;再不行退到副本(如实告知)。"""
    try:
        os.symlink(src, entry, target_is_directory=True)
        return "已链接"
    except OSError:
        if os.name == "nt":
            r = sh(["cmd", "/c", "mklink", "/J", str(entry), str(src)])
            if r.returncode == 0:
                return "已链接"
        shutil.copytree(src, entry)
        return "已复制(此系统不支持链接,库里改动后需重新开启一次来同步)"


def remove_entry(p: Path):
    if read_link(p) is not None:
        try:
            p.unlink()
        except OSError:
            os.rmdir(p)   # Windows junction 用 rmdir 摘除,不碰目标内容
    elif p.is_dir():
        shutil.rmtree(p)


def expand_names(names) -> list:
    out = []
    for n in names:
        if n.startswith("@"):
            f = SETS / f"{n[1:]}.txt"
            if not f.exists():
                raise ValueError(f"没有组合「{n[1:]}」")
            out += [l.strip() for l in f.read_text().splitlines()
                    if l.strip() and not l.strip().startswith("#")]
        else:
            out.append(n)
    for n in out:
        if not (LIB / n).is_dir():
            raise ValueError(f"库里没有技能「{n}」")
    return out


def links_enable(target: str, names) -> dict:
    try:
        skills = expand_names(names)
    except ValueError as e:
        return {"ok": False, "out": str(e)}
    dest = resolve_place(target)
    dest.mkdir(parents=True, exist_ok=True)
    register_target(target)
    done, skipped = [], []
    for s in skills:
        e = dest / s
        st = entry_state(e, s)
        if st in ("hub-link", "broken-link", "copy-synced"):
            remove_entry(e)
        elif st != "absent":
            skipped.append(f"{s}({st},非本库管理,请先手动处理)")
            continue
        make_link(e, LIB / s)
        done.append(s)
    out = f"已开启 {len(done)} 个" if done else "没有开启任何技能"
    if skipped:
        out += ";跳过: " + ", ".join(skipped)
    return {"ok": bool(done) or not skipped, "out": out}


def links_disable(target: str, names) -> dict:
    try:
        skills = expand_names(names)
    except ValueError as e:
        return {"ok": False, "out": str(e)}
    dest = resolve_place(target)
    done, refused = [], []
    for s in skills:
        e = dest / s
        st = entry_state(e, s)
        if st in ("hub-link", "broken-link", "copy-synced"):
            remove_entry(e)
            done.append(s)
        elif st == "absent":
            done.append(s)
        else:
            refused.append(f"{s}({st}:有本地改动或非本库管理,请手动处理)")
    out = f"已关闭 {len(done)} 个" if done else "没有关闭任何技能"
    if refused:
        out += ";拒绝: " + ", ".join(refused)
    return {"ok": not refused, "out": out}


# ---------- 项目注册表 ----------

def ui_conf():
    return load_json(UI_CONF_FILE, {"clean_empty_dirs": True})


def read_targets():
    if not TARGETS_FILE.exists():
        return []
    return [l.strip() for l in TARGETS_FILE.read_text().splitlines() if l.strip()]


def register_target(target: str):
    if target in KINDS:
        return
    p = str(Path(target.partition("::")[0]).expanduser().resolve())
    ts = read_targets()
    if p not in ts:
        ts.append(p)
        TARGETS_FILE.write_text("\n".join(ts) + "\n")


def clean_targets():
    kept, removed = [], []
    for t in read_targets():
        alive = False
        for kind in KINDS:
            d = Path(t) / f".{kind}" / "skills"
            if d.is_dir() and any(
                entry_state(e, e.name) in ("hub-link", "copy-synced", "copy-diverged", "broken-link")
                for e in d.iterdir() if not e.name.startswith(".")
            ):
                alive = True
                break
        (kept if alive else removed).append(t)
    TARGETS_FILE.write_text("\n".join(kept) + ("\n" if kept else ""))
    return removed


def cleanup_target_dirs(target: str):
    """从项目移除技能后:skills 目录空了就删掉,上层 .claude/.codex/.agents 也空了就一并删。
    只作用于项目目录,永不碰全局家目录。"""
    if target in KINDS:
        return
    proj, _, kind = target.partition("::")
    d = Path(proj) / f".{kind or 'claude'}" / "skills"
    try:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            if not any(d.parent.iterdir()):
                d.parent.rmdir()
    except OSError:
        pass
    if not any((Path(proj) / f".{k}" / "skills").is_dir() for k in KINDS):
        kept = [t for t in read_targets() if t != proj]
        if len(kept) != len(read_targets()):
            TARGETS_FILE.write_text("\n".join(kept) + ("\n" if kept else ""))


# ---------- 来历 ----------

def origins():
    return load_json(ORIGINS_FILE, {})


def set_origin(name, info):
    o = origins()
    if info is None:
        o.pop(name, None)
    else:
        o[name] = info
    save_json(ORIGINS_FILE, o)


# ---------- 外部来源(vendor) ----------

def sync_snapshot(name: str, src_dir: Path):
    """把用户确认的新内容原子替换进 library/<name>(物化快照前进)。"""
    dst = LIB / name
    tmp = LIB / f".{name}.new"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src_dir, tmp, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
    if read_link(dst) is not None:
        remove_entry(dst)
    else:
        shutil.rmtree(dst, ignore_errors=True)
    tmp.rename(dst)


def vendor_head(d: Path) -> str:
    r = git(["log", "-1", "--format=%h %ad %s", "--date=short"], cwd=d)
    return r.stdout.strip() if r.returncode == 0 else "(非 git,手动放入的目录)"


def vendor_sources():
    out = []
    if not VENDOR.is_dir():
        return out
    imported = {}
    for sk, info in origins().items():
        if info.get("source"):
            imported.setdefault(info["source"], {})[info.get("subpath", "")] = \
                {"name": sk, "type": info["type"]}
    for d in sorted(VENDOR.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        url = git(["remote", "get-url", "origin"], cwd=d).stdout.strip()
        skills = []
        for sm in sorted(d.rglob("SKILL.md")):
            if ".git" in sm.parts or len(sm.relative_to(d).parts) > 5:
                continue
            sub = str(sm.parent.relative_to(d))
            imp = imported.get(d.name, {}).get(sub)
            try:
                desc = parse_desc(sm.read_text())[:90]
            except OSError:
                desc = ""
            skills.append({"subpath": sub, "name": sm.parent.name, "desc": desc,
                           "imported_as": imp["name"] if imp else None,
                           "imported_type": imp["type"] if imp else None})
        out.append({"name": d.name, "url": url, "head": vendor_head(d),
                    "is_git": (d / ".git").exists(), "skills": skills})
    return out


def remote_head(src_dir: Path):
    git(["fetch", "--quiet", "origin"], cwd=src_dir)
    r = git(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=src_dir)
    ref = r.stdout.strip() if r.returncode == 0 else ""
    if not ref:
        for cand in ("origin/main", "origin/master"):
            if git(["rev-parse", cand], cwd=src_dir).returncode == 0:
                ref = cand
                break
    if not ref:
        raise RuntimeError("找不到远端默认分支")
    return ref


def issue_update_token(source: str, commit: str) -> str:
    now = time.time()
    for k in [k for k, v in UPDATE_TOKENS.items() if v["exp"] < now]:
        UPDATE_TOKENS.pop(k, None)
    tok = secrets.token_hex(16)
    UPDATE_TOKENS[tok] = {"source": source, "commit": commit, "exp": now + UPDATE_TOKEN_TTL}
    return tok


def affected_skills(d: Path, upto: str):
    """HEAD..<upto> 之间,哪些"跟随更新"的技能有文件变动。"""
    out = []
    for sk, info in origins().items():
        if info.get("source") == d.name and info.get("type") == "ref":
            diff = git(["diff", "--name-only", f"HEAD..{upto}", "--", info["subpath"]],
                       cwd=d).stdout.strip()
            if diff:
                out.append({"skill": sk, "subpath": info["subpath"], "files": diff.splitlines()})
    return out


def source_check(source: str):
    """第一步授权:用户点了「检查远端更新」才联网 fetch;
    发现新提交时签发绑定 来源+目标提交 的一次性令牌,供第二步消费。"""
    d = VENDOR / source
    if not (d / ".git").exists():
        return {"ref": "", "behind": 0, "commits": "", "affected": [],
                "note": "这个来源不是 git 仓库。更新方式:把新内容放进它的目录,再重新引入需要的技能。"}
    ref = remote_head(d)
    target = git(["rev-parse", ref], cwd=d).stdout.strip()
    behind = int(git(["rev-list", "--count", f"HEAD..{ref}"], cwd=d).stdout.strip() or 0)
    commits = git(["log", "--format=%h %ad %s", "--date=short", f"HEAD..{ref}"], cwd=d).stdout.strip()
    res = {"ref": ref, "behind": behind, "commits": commits,
           "affected": affected_skills(d, target)}
    if behind:
        res["token"] = issue_update_token(source, target)
        res["target"] = target[:10]
    return res


def source_update(source: str, token: str):
    """第二步授权:只消费检查时签发的令牌,快进到令牌绑定的那个提交;
    不再联网,也不会自行解析"更新的新版本"。"""
    rec = UPDATE_TOKENS.pop(token or "", None)
    if not rec or rec["source"] != source or rec["exp"] < time.time():
        return {"ok": False, "out": "更新令牌无效或已过期。请先点「检查远端更新」,查看差异后再更新。"}
    d = VENDOR / source
    if not (d / ".git").exists():
        return {"ok": False, "out": "来源不存在或不是 git 仓库"}
    commit = rec["commit"]
    if git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=d).returncode != 0:
        return {"ok": False, "out": "目标提交不在本地,请重新检查更新。"}
    affected = affected_skills(d, commit)
    r = git(["merge", "--ff-only", commit], cwd=d)
    if r.returncode != 0:
        return {"ok": False, "out": f"合并失败(本地有分叉?): {r.stderr[:300]}"}
    short = git(["rev-parse", "--short", "HEAD"], cwd=d).stdout.strip()
    for a in affected:
        info = origins().get(a["skill"])
        if not info:
            continue
        sync_snapshot(a["skill"], d / a["subpath"])
        info["commit"] = short
        set_origin(a["skill"], info)
    git_commit(f"更新来源 {source} 到 {short}")
    n = len(affected)
    return {"ok": True, "out": f"已更新「{source}」到 {short}" +
            (f",{n} 个跟随更新的技能已同步新快照" if n else ",没有跟随更新的技能受影响")}


# ---------- 状态汇总 ----------

def api_state():
    org = origins()
    projects = [t for t in read_targets() if Path(t).exists()]
    stale = [t for t in read_targets() if not Path(t).exists()]
    # 项目级放置点:每个项目下实际存在的 .claude/.codex/.agents skills 目录各算一个
    proj_targets = [{"path": p, "kind": kind, "target": f"{p}::{kind}"}
                    for p in projects for kind in KINDS
                    if (Path(p) / f".{kind}" / "skills").is_dir()]
    skills = []
    for dname in sorted(os.listdir(LIB)) if LIB.is_dir() else []:
        d = LIB / dname
        if dname.startswith(".") or not d.is_dir() or not (d / "SKILL.md").exists():
            continue
        places = {k: entry_state(ROOTS[k] / dname, dname) for k in KINDS}
        places["projects"] = {t["target"]: entry_state(
            Path(t["path"]) / f".{t['kind']}" / "skills" / dname, dname) for t in proj_targets}
        skills.append({"name": dname, "desc": desc_of(dname), "origin": org.get(dname),
                       "places": places})
    warnings = []
    roots = [(f"{k.capitalize()} 全局", ROOTS[k]) for k in KINDS] + \
            [(f"项目 {t['path']}(.{t['kind']})",
              Path(t["path"]) / f".{t['kind']}" / "skills") for t in proj_targets]
    few = lambda names: "、".join(names[:3]) + (" 等" if len(names) > 3 else "")
    for label, root in roots:
        if not root.is_dir():
            continue
        broken, diverged, unmanaged = [], [], []
        for e in sorted(root.iterdir()):
            if e.name.startswith("."):
                continue
            s = entry_state(e, e.name)
            if s == "broken-link":
                broken.append(e.name)
            elif s == "copy-diverged":
                diverged.append(e.name)
            elif s == "local-dir" and adoptable(e):
                unmanaged.append(e.name)
        if broken:
            warnings.append(f"{label} 有 {len(broken)} 个失联的技能链接({few(broken)}),把开关拨掉即可清除")
        if diverged:
            warnings.append(f"{label} 有 {len(diverged)} 个独立副本内容已和库里不同({few(diverged)})")
        if unmanaged:
            warnings.append(f"{label} 有 {len(unmanaged)} 个技能还没进库({few(unmanaged)}),可用「扫描收编」接管")
    for t in stale:
        warnings.append(f"项目 {t} 目录已不存在,可在「使用情况」页点\"清理失效项目\"")
    sets_raw = {f.stem: f.read_text() for f in sorted(SETS.glob("*.txt"))}
    sets = {k: [l.strip() for l in v.splitlines() if l.strip() and not l.strip().startswith("#")]
            for k, v in sets_raw.items()}
    autostart = False
    if sys.platform == "darwin":
        autostart = "com.skills-hub.webui" in sh(["launchctl", "list"]).stdout
    return {"skills": skills, "projects": projects, "proj_targets": proj_targets,
            "agents_root": ROOTS["agents"].is_dir(),
            "stale_targets": stale, "warnings": warnings,
            "sets": sets, "sets_raw": sets_raw, "sources": vendor_sources(),
            "clean_empty_dirs": ui_conf().get("clean_empty_dirs", True),
            "platform": sys.platform, "autostart": autostart}


# ---------- 变更操作 ----------

def op_toggle(b):
    if b["on"]:
        r = links_enable(b["target"], [b["skill"]])
    else:
        r = links_disable(b["target"], [b["skill"]])
        if r["ok"] and ui_conf().get("clean_empty_dirs", True):
            cleanup_target_dirs(b["target"])
    return r


def op_set_apply(b):
    if b["on"]:
        r = links_enable(b["target"], ["@" + b["set"]])
    else:
        r = links_disable(b["target"], ["@" + b["set"]])
        if r["ok"] and ui_conf().get("clean_empty_dirs", True):
            cleanup_target_dirs(b["target"])
    return r


def op_save_skill(b):
    name = b["name"]
    if not NAME_RE.match(name) or not (LIB / name).is_dir():
        return {"ok": False, "out": "技能不存在"}
    info = origins().get(name)
    if info and info.get("type") == "ref":
        return {"ok": False, "out": "这是跟随上游更新的技能,内容由来源仓库决定,不能在这里改。想自己改就先「转为我的副本」。"}
    (LIB / name / "SKILL.md").write_text(b["content"])
    git_commit(f"编辑 {name}")
    return {"ok": True, "out": f"已保存「{name}」,所有开启的位置即时生效"}


def op_new(b):
    name = b["name"].strip()
    if not NAME_RE.match(name):
        return {"ok": False, "out": "名字只能用小写字母、数字、连字符"}
    if (LIB / name).exists():
        return {"ok": False, "out": f"库里已有「{name}」"}
    (LIB / name).mkdir(parents=True)
    (LIB / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: <一句话:做什么 + 什么时候用(触发词)>\n---\n\n# {name}\n\n<正文>\n")
    set_origin(name, {"type": "own", "created": datetime.now().isoformat(timespec="seconds")})
    git_commit(f"新建 {name}")
    return {"ok": True, "out": f"已创建「{name}」"}


def op_delete(b):
    name = b["name"]
    if not NAME_RE.match(name) or not (LIB / name).exists():
        return {"ok": False, "out": "技能不存在"}
    info = origins().get(name) or {}
    clean = ui_conf().get("clean_empty_dirs", True)
    for t in list(KINDS) + [f"{p}::{k}" for p in read_targets() for k in KINDS]:
        links_disable(t, [name])
        if clean:
            cleanup_target_dirs(t)
    p = LIB / name
    if read_link(p) is not None:  # 旧版遗留的软链接引用
        remove_entry(p)
        msg = f"已移除外部引用「{name}」(来源仓库原件未动,可随时重新引入)"
    else:
        trash = HUB / "attic" / "trash" / datetime.now().strftime("%Y%m%d-%H%M%S")
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(trash / name))
        msg = (f"已移除「{name}」的快照(进回收站;来源仓库原件未动,可随时重新引入)"
               if info.get("type") == "ref" else
               f"已把「{name}」移入回收站(attic/trash,没有真删)")
    set_origin(name, None)
    git_commit(f"删除 {name}")
    return {"ok": True, "out": msg}


def op_adopt(b):
    src = Path(b["path"]).expanduser()
    src = Path(str(src).rstrip("/"))
    if not (src / "SKILL.md").exists():
        return {"ok": False, "out": f"{src} 里没有 SKILL.md,不是技能目录"}
    name = src.name
    if not NAME_RE.match(name):
        return {"ok": False, "out": "名字须小写字母/数字/连字符,改名后再收编"}
    if (LIB / name).exists():
        return {"ok": False, "out": f"库里已有同名技能「{name}」,先对比处理"}
    shutil.move(str(src), str(LIB / name))
    note = ""
    try:
        os.symlink(LIB / name, src, target_is_directory=True)
    except OSError:
        if os.name == "nt" and sh(["cmd", "/c", "mklink", "/J", str(src), str(LIB / name)]).returncode == 0:
            pass
        else:
            note = "(原位置未能留下链接,请在页面上重新开启)"
    set_origin(name, {"type": "own", "adopted_from": str(src)})
    if src.parent.name == "skills" and src.parent.parent.name in (".claude", ".codex", ".agents"):
        root = src.parent.parent.parent
        if root != Path.home():
            register_target(str(root))
    git_commit(f"收编 {name}")
    return {"ok": True, "out": f"已收编「{name}」入库,原位置用法不变{note}"}


def adoptable(e: Path) -> bool:
    """目录算不算"可收编的散装技能":要有真实的 SKILL.md。
    SKILL.md 本身是软链的目录属于别的工具在管理(挪走会弄坏人家),不算。"""
    f = e / "SKILL.md"
    return f.exists() and read_link(f) is None


def op_scan_local(_b):
    """扫描全局与各项目的 .claude/.codex/.agents,找出还没进库的技能。纯本地文件遍历。"""
    found, seen = [], set()

    def check(droot: Path, label: str):
        if not droot.is_dir():
            return
        for e in sorted(droot.iterdir()):
            if e.name.startswith(".") or read_link(e) is not None or not e.is_dir():
                continue
            if not adoptable(e):
                continue
            if entry_state(e, e.name) not in ("local-dir", "copy-diverged"):
                continue  # 已是本库的链接/同步副本
            key = str(e.resolve())
            if key in seen:
                continue
            seen.add(key)
            found.append({"path": str(e), "name": e.name, "place": label,
                          "conflict": (LIB / e.name).exists(),
                          "valid": bool(NAME_RE.match(e.name))})

    for k in KINDS:
        check(ROOTS[k], f"{k.capitalize()} 全局")
    for p in read_targets():
        for k in KINDS:
            check(Path(p) / f".{k}" / "skills", f"{Path(p).name} · .{k}")
    return {"ok": True, "found": found}


def op_adopt_bulk(b):
    done, failed = [], []
    for pstr in b.get("paths") or []:
        src = Path(pstr).expanduser()
        r = op_adopt({"path": pstr})
        (done if r["ok"] else failed).append(src.name)
    out = f"已收编 {len(done)} 个技能" if done else "没有收编任何技能"
    if failed:
        out += f";失败: {', '.join(failed)}"
    return {"ok": bool(done) or not failed, "out": out}


def op_import(b):
    """从任意目录导入:单个技能目录(内有 SKILL.md),或含多个技能子目录的父目录。
    只认 SKILL.md 这一个标准,目录里其余文件原样保留。导入是复制,原目录不动。"""
    if b.get("probe"):
        src = Path((b.get("path") or "").strip()).expanduser()
        if not src.is_dir():
            return {"ok": False, "out": "目录不存在"}
        items = [src] if (src / "SKILL.md").exists() else \
                [d for d in sorted(src.iterdir()) if d.is_dir() and (d / "SKILL.md").exists()]
        found = []
        for d in items:
            try:
                desc = parse_desc((d / "SKILL.md").read_text())[:80]
            except OSError:
                desc = ""
            found.append({"path": str(d), "name": d.name, "desc": desc,
                          "conflict": (LIB / d.name).exists(),
                          "valid": bool(NAME_RE.match(d.name))})
        return {"ok": True, "found": found}
    done, failed = [], []
    for pstr in b.get("paths") or []:
        d = Path(pstr).expanduser()
        name = d.name
        if not d.is_dir() or not (d / "SKILL.md").exists():
            failed.append(f"{name}(不是技能目录)")
            continue
        if not NAME_RE.match(name):
            failed.append(f"{name}(名字须小写字母/数字/连字符,改名后再导)")
            continue
        if (LIB / name).exists():
            failed.append(f"{name}(库里已有同名)")
            continue
        shutil.copytree(d, LIB / name, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
        set_origin(name, {"type": "own", "imported_from": str(d),
                          "created": datetime.now().isoformat(timespec="seconds")})
        done.append(name)
    if done:
        git_commit(f"导入 {', '.join(done)}")
    out = f"已导入 {len(done)} 个技能" if done else "没有导入任何技能"
    if failed:
        out += f";跳过: {'; '.join(failed)}"
    return {"ok": bool(done) or not failed, "out": out}


def op_open(b):
    """在文件管理器里打开技能库目录(或某个技能的目录)。只允许开库内路径。"""
    name = (b.get("name") or "").strip()
    p = LIB / name if name else LIB
    if name and (not NAME_RE.match(name) or not p.is_dir()):
        return {"ok": False, "out": "技能不存在"}
    open_in_file_manager(p)
    return {"ok": True, "out": "已在文件管理器打开"}


def op_set_delete(b):
    name = (b.get("name") or "").strip()
    f = SETS / f"{name}.txt"
    if not NAME_RE.match(name) or not f.exists():
        return {"ok": False, "out": "组合不存在"}
    f.unlink()
    git_commit(f"删除组合 {name}")
    return {"ok": True, "out": f"已删除组合「{name}」(组合只是清单,技能本身不受影响)"}


def op_source_add(b):
    """用户点了「下载来源」才会执行的联网动作。只克隆,不引入、不启用、不执行任何内容。"""
    url = b["url"].strip()
    name = (b.get("name") or "").strip() or re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1])
    if not NAME_RE.match(name):
        return {"ok": False, "out": "来源名只能用小写字母、数字、连字符(可在输入框指定)"}
    if (VENDOR / name).exists():
        return {"ok": False, "out": f"来源「{name}」已存在"}
    VENDOR.mkdir(exist_ok=True)
    r = git(["clone", url, str(VENDOR / name)], timeout=600)
    if r.returncode != 0:
        return {"ok": False, "out": f"克隆失败: {r.stderr[:300]}"}
    return {"ok": True, "out": f"已下载来源「{name}」。在下方挑选要引入的技能;"
            "引入前请自行阅读内容,本工具不验证第三方内容的安全性"}


def op_source_import(b):
    source, subpath, mode = b["source"], b["subpath"], b["mode"]
    src = VENDOR / source / subpath
    if not (src / "SKILL.md").exists():
        return {"ok": False, "out": "来源里没有这个技能"}
    name = (b.get("newname") or "").strip() or src.name
    if not NAME_RE.match(name):
        return {"ok": False, "out": "技能名不合法"}
    if (LIB / name).exists():
        return {"ok": False, "out": f"库里已有「{name}」,换个名字引入"}
    commit = git(["rev-parse", "--short", "HEAD"], cwd=VENDOR / source).stdout.strip() or "worktree"
    # 两种模式都物化快照进库(主权隔离:vendor 怎么变都不直接生效);
    # 区别只在于 ref 可在你手动检查、确认后跟进上游更新,copy 从此与上游脱钩。
    sync_snapshot(name, src)
    set_origin(name, {"type": mode if mode in ("copy", "ref") else "copy",
                      "source": source, "subpath": subpath, "commit": commit})
    git_commit(f"引入 {name}(来自 {source},{mode})")
    return {"ok": True, "out": f"已引入「{name}」(当前版本的快照)。开关默认关闭,开启前请自行阅读内容"}


def op_source_fork(b):
    """把跟随更新的技能转成独立副本(内容已是快照,只改归属)。"""
    name = b["name"]
    info = origins().get(name)
    if not info or info.get("type") != "ref":
        return {"ok": False, "out": "只有跟随更新的技能才需要转独立副本"}
    p = LIB / name
    if read_link(p) is not None:  # 旧版遗留:先物化
        real = Path(os.readlink(p))
        remove_entry(p)
        shutil.copytree(real, p, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
    info["type"] = "copy"
    set_origin(name, info)
    git_commit(f"{name} 转为独立副本")
    return {"ok": True, "out": f"「{name}」已转为独立副本,以后可自由编辑,不再跟随来源更新"}


def op_source_remove(b):
    source = b["source"]
    used = [sk for sk, i in origins().items() if i.get("source") == source and i.get("type") == "ref"]
    if used:
        return {"ok": False, "out": f"还有跟随更新的技能在用这个来源: {', '.join(used)}。先删除它们或转成独立副本。"}
    shutil.rmtree(VENDOR / source, ignore_errors=True)
    return {"ok": True, "out": f"已移除来源「{source}」"}


def op_settings(b):
    if "clean_empty_dirs" in b:
        c = ui_conf()
        c["clean_empty_dirs"] = bool(b["clean_empty_dirs"])
        save_json(UI_CONF_FILE, c)
    return {"ok": True, "out": "已保存"}


def op_targets_clean(_b):
    removed = clean_targets()
    return {"ok": True, "out": f"已清理 {len(removed)} 个失效项目" if removed else "没有需要清理的项目"}


def op_save_set(b):
    name = (b.get("name") or "").strip()
    if not NAME_RE.match(name):
        return {"ok": False, "out": "组合名只能用小写字母、数字、连字符"}
    SETS.mkdir(exist_ok=True)
    (SETS / f"{name}.txt").write_text(b["content"])
    git_commit(f"编辑组合 {name}")
    return {"ok": True, "out": "组合已保存"}


POST_OPS = {
    "/api/toggle": op_toggle, "/api/set-apply": op_set_apply, "/api/skill": op_save_skill,
    "/api/new": op_new, "/api/delete": op_delete, "/api/adopt": op_adopt,
    "/api/source/add": op_source_add, "/api/source/import": op_source_import,
    "/api/source/fork": op_source_fork, "/api/source/remove": op_source_remove,
    "/api/settings": op_settings, "/api/targets/clean": op_targets_clean,
    "/api/set": op_save_set, "/api/set-delete": op_set_delete,
    "/api/scan": op_scan_local, "/api/adopt-bulk": op_adopt_bulk,
    "/api/import": op_import, "/api/open": op_open,
    "/api/source/check": lambda b: {"ok": True, **source_check(b["source"])},
    "/api/source/update": lambda b: source_update(b["source"], b.get("token")),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_allowed(self) -> bool:
        """写 API 的授权校验,不依赖页面流程:
        loopback Host + 同源 Origin + JSON Content-Type + 页面会话令牌,缺一不可。"""
        allowed = {f"127.0.0.1:{SERVER_PORT}", f"localhost:{SERVER_PORT}"}
        if (self.headers.get("Host") or "").strip() not in allowed:
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin and origin not in {f"http://{h}" for h in allowed}:
            return False
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return False
        return secrets.compare_digest(self.headers.get("X-Hub-Token") or "", CSRF_TOKEN)

    def do_GET(self):
        path = self.path.partition("?")[0]
        query = self.path.partition("?")[2]
        q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path == "/":
            body = PAGE.replace("__CSRF__", CSRF_TOKEN).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            try:
                self._json(api_state())
            except Exception as e:
                self._json({"ok": False, "out": f"读取状态失败: {e}"}, 500)
        elif path == "/api/skill":
            name = unquote(q.get("name", ""))
            f = LIB / name / "SKILL.md"
            if not NAME_RE.match(name) or not f.exists():
                return self._json({"ok": False, "out": "技能不存在"}, 404)
            self._json({"ok": True, "content": f.read_text(),
                        "readonly": (origins().get(name) or {}).get("type") == "ref"})
        else:
            self._json({"ok": False, "out": "not found"}, 404)

    def do_POST(self):
        path = self.path.partition("?")[0]
        op = POST_OPS.get(path)
        if not op:
            return self._json({"ok": False, "out": "not found"}, 404)
        if not self._write_allowed():
            return self._json({"ok": False, "out": "请求被拒绝(仅限本页面发起的操作)"}, 403)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            res = op(body)
            self._json(res if isinstance(res, dict) else {"ok": True})
        except Exception as e:
            self._json({"ok": False, "out": f"操作失败: {e}"}, 500)


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>技能库</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='24' fill='%234655d4'/%3E%3Ctext x='50' y='72' font-size='60' text-anchor='middle' fill='%23fff'%3E✦%3C/text%3E%3C/svg%3E">
<style>
:root{
  --bg:#f4f5f7;--panel:#fbfbfc;--card:#fff;--ink:#181f2a;--muted:#68717f;--faint:#98a0ac;
  --line:#e4e7ec;--accent:#4655d4;--accent-soft:#eef0fd;--accent-ink:#3743ad;
  --ok:#188945;--okbg:#e3f5ea;--warn:#96620b;--warnbg:#fdf2d7;--bad:#c22f2f;--badbg:#fde7e7;
  --info:#215fb0;--infobg:#e6effc;--shadow:0 1px 2px rgba(16,24,40,.05);
  --r:14px;--rs:9px;
}
@media(prefers-color-scheme:dark){:root{
  --bg:#101318;--panel:#161a21;--card:#1b202a;--ink:#e8ebf1;--muted:#9aa4b2;--faint:#6b7482;
  --line:#2a303c;--accent:#7d8cf8;--accent-soft:#232a4d;--accent-ink:#aab4ff;
  --ok:#5fd08c;--okbg:#15301f;--warn:#e8b34c;--warnbg:#38290e;--bad:#f27b7b;--badbg:#3d1a1a;
  --info:#7fb0ee;--infobg:#16273f;--shadow:none;
}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);display:flex;
font:14.5px/1.65 -apple-system,"PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif}

/* ---- 侧边栏 ---- */
.side{width:212px;flex:none;background:var(--panel);border-right:1px solid var(--line);
display:flex;flex-direction:column;padding:18px 12px 14px;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:10px;padding:2px 10px 16px}
.brand .logo{width:32px;height:32px;border-radius:9px;background:var(--accent);color:#fff;
display:flex;align-items:center;justify-content:center;font-size:17px;flex:none}
.brand b{font-size:15.5px;display:block;line-height:1.2}
.brand .sub{font-size:11px;color:var(--faint)}
.nav{display:flex;flex-direction:column;gap:2px}
.nav button{display:flex;align-items:center;gap:10px;width:100%;padding:8px 11px;border:none;
background:none;color:var(--muted);font:inherit;font-size:13.5px;border-radius:var(--rs);
cursor:pointer;text-align:left}
.nav button:hover{background:var(--accent-soft);color:var(--accent-ink)}
.nav button.active{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
.nav svg{width:17px;height:17px;flex:none}
.nav .cnt{margin-left:auto;font-size:11px;color:var(--faint);font-weight:400}
.sidefoot{margin-top:auto;padding:10px;font-size:11.5px;color:var(--faint);line-height:1.9}
.sidefoot .st{display:flex;align-items:center;gap:6px}
.sidefoot .st::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--faint)}
.sidefoot .st.on::before{background:var(--ok)}

/* ---- 主区 ---- */
.main{flex:1;min-width:0;height:100vh;overflow-y:auto}
.page{max-width:920px;margin:0 auto;padding:26px 30px 90px}
.pagehead{display:flex;align-items:flex-start;gap:12px;margin-bottom:6px;flex-wrap:wrap}
.pagehead h1{font-size:21px;margin:0;letter-spacing:.2px}
.pagehead .sub{flex-basis:100%;color:var(--muted);font-size:13px;margin-top:2px}
.pagehead .acts{margin-left:auto;display:flex;gap:8px;align-items:center}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:16px 18px;margin-top:14px;box-shadow:var(--shadow)}
.card h2{font-size:14.5px;margin:0 0 4px}
.hint{font-size:12.5px;color:var(--muted)}
.warnbox{background:var(--warnbg);color:var(--warn);border-radius:var(--rs);padding:8px 13px;
font-size:13px;margin-top:10px}
.banner{background:var(--infobg);color:var(--info);border-radius:var(--r);padding:13px 17px;
margin-top:14px;font-size:13.5px}
.banner b{display:block;margin-bottom:3px}
.banner a{color:inherit}

/* ---- 控件 ---- */
button{font:inherit;font-size:13px;padding:6px 13px;border-radius:var(--rs);
border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;
transition:border-color .12s,color .12s}
button:hover{border-color:var(--accent);color:var(--accent-ink)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.primary:hover{opacity:.92;color:#fff}
button.ghost{border-color:transparent;color:var(--muted)}
button.ghost:hover{color:var(--accent-ink);border-color:transparent;background:var(--accent-soft)}
button.danger:hover{border-color:var(--bad);color:var(--bad)}
button:disabled{opacity:.45;cursor:default;pointer-events:none}
input[type=text],select{font:inherit;font-size:13.5px;padding:7px 11px;
border:1px solid var(--line);border-radius:var(--rs);background:var(--card);color:var(--ink)}
input:focus,select:focus,textarea:focus{outline:2px solid var(--accent-soft);border-color:var(--accent)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.field{display:flex;flex-direction:column;gap:4px;font-size:12.5px;color:var(--muted)}
textarea{width:100%;font:13px/1.55 ui-monospace,Menlo,Consolas,monospace;border:1px solid var(--line);
border-radius:var(--rs);padding:10px;background:var(--bg);color:var(--ink);resize:vertical}
pre{font-size:12px;color:var(--muted);overflow-x:auto;margin:6px 0 0;white-space:pre-wrap}
.mono{font:12px ui-monospace,Menlo,Consolas,monospace}

/* ---- 徽章 ---- */
.tag{display:inline-flex;align-items:center;gap:3px;font-size:10px;padding:1px 7px;flex:none;
border-radius:99px;border:1px solid var(--line);color:var(--muted);white-space:nowrap;font-weight:500}
.tag.src-own{background:transparent}
.tag.src-ref{background:var(--infobg);color:var(--info);border-color:transparent}
.tag.src-copy{background:var(--accent-soft);color:var(--accent-ink);border-color:transparent}
.tag.done{background:var(--okbg);color:var(--ok);border-color:transparent}
.tag.miss{color:var(--bad);border-color:var(--bad)}

/* ---- 技能卡(紧凑两列,同「使用情况」) ---- */
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0 2px}
.chip{font-size:12.5px;padding:4px 13px;border-radius:99px;border:1px solid var(--line);
background:var(--card);color:var(--muted);cursor:pointer;user-select:none}
.chip.active{background:var(--ink);color:var(--card);border-color:var(--ink);font-weight:600}
.skgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
@media(max-width:820px){.skgrid{grid-template-columns:1fr}}
.skcard{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:11px 14px 10px;box-shadow:var(--shadow);display:flex;flex-direction:column;min-width:0}
.sk-head{display:flex;gap:6px;align-items:center;min-width:0}
.sk-name{font-weight:650;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sk-desc{color:var(--muted);font-size:12px;line-height:1.55;margin:3px 0 8px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sk-acts{margin-left:auto;display:flex;flex:none}
.sk-acts button{padding:2px 6px;font-size:11.5px}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;padding:3px 10px;
border-radius:99px;border:1.5px solid var(--line);cursor:pointer;color:var(--muted);
user-select:none;background:var(--card);transition:border-color .12s}
.pill:hover{border-color:var(--accent)}
.pill.on{background:var(--okbg);border-color:var(--ok);color:var(--ok);font-weight:600}
.pill.on::before{content:"✓"}
.pill.warn{border-color:var(--warn);color:var(--warn)}
.pill.add{border-style:dashed;color:var(--faint)}
.pills{display:flex;gap:5px;flex-wrap:wrap;align-items:center;margin-top:auto}

/* ---- 使用情况 ---- */
.usegrid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
@media(max-width:760px){.usegrid{grid-template-columns:1fr}}
.usecard{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:15px 17px;box-shadow:var(--shadow)}
.usecard h3{font-size:14px;margin:0;display:flex;align-items:center;gap:8px}
.usecard h3 .n{margin-left:auto;font-size:12px;color:var(--faint);font-weight:400}
.usecard .path{font-size:11.5px;color:var(--faint);margin:1px 0 10px;word-break:break-all}
.loc-ico{width:26px;height:26px;border-radius:7px;background:var(--accent-soft);color:var(--accent-ink);
display:flex;align-items:center;justify-content:center;font-size:13px;flex:none}

/* ---- 来源 ---- */
.srcskill{display:flex;gap:8px;align-items:baseline;padding:7px 0;border-bottom:1px dashed var(--line);flex-wrap:wrap}
.srcskill:last-child{border-bottom:none}
.pendbox{background:var(--infobg);border-radius:var(--rs);padding:11px 14px;margin-top:10px;font-size:13px;color:var(--info)}
.pendbox b{display:block;margin-bottom:2px}

/* ---- 弹层 ---- */
dialog{border:1px solid var(--line);border-radius:var(--r);background:var(--card);color:var(--ink);
width:min(860px,94vw);padding:20px 22px}
dialog::backdrop{background:rgba(10,14,20,.45)}
dialog h2{margin:0 0 10px;font-size:16px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);
color:var(--bg);padding:10px 20px;border-radius:12px;font-size:13px;opacity:0;transition:.25s;
pointer-events:none;max-width:82vw;z-index:99}
.toast.show{opacity:.96}
.empty{color:var(--faint);font-size:13px;padding:22px 0;text-align:center}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--info);
border-top-color:transparent;border-radius:50%;animation:sp 1s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}

@media(max-width:860px){
  body{flex-direction:column}
  .side{width:100%;height:auto;position:static;flex-direction:row;align-items:center;
  padding:10px 12px;gap:6px;overflow-x:auto}
  .brand{padding:0 6px 0 0}.brand .sub{display:none}
  .nav{flex-direction:row}.nav button{padding:7px 10px;white-space:nowrap}
  .nav .cnt{display:none}.sidefoot{display:none}
  .main{height:auto}.page{padding:16px 14px 70px}
}
</style></head><body>

<aside class="side">
  <div class="brand"><div class="logo">✦</div><div><b>技能库</b><span class="sub">一处管理 · 处处可用</span></div></div>
  <nav class="nav" id="nav"></nav>
  <div class="sidefoot" id="sidefoot"></div>
</aside>

<div class="main"><div class="page" id="page"></div></div>

<dialog id="editor">
  <h2 id="edTitle"></h2>
  <textarea id="edBody" style="height:56vh" spellcheck="false"></textarea>
  <div class="row" style="justify-content:flex-end;margin-top:12px">
    <span class="hint" id="edHint" style="margin-right:auto"></span>
    <button class="ghost" id="edOpen" title="打开这个技能的文件夹,放脚本等其他文件" onclick="post('/api/open',{name:ED.name})">打开目录</button>
    <button onclick="editor.close()">取消</button>
    <button class="primary" id="edSave" onclick="saveEditor()">保存</button>
  </div>
</dialog>

<dialog id="ask">
  <h2 id="askTitle"></h2>
  <div class="hint" id="askHint" style="margin-bottom:10px"></div>
  <div id="askBody"></div>
  <div class="row" style="justify-content:flex-end;margin-top:14px">
    <button onclick="ask.close()">取消</button>
    <button class="primary" id="askOk">确定</button>
  </div>
</dialog>

<div class="toast" id="toast"></div>

<script>
const $=s=>document.querySelector(s);
const esc=s=>(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
const base=p=>p.split(/[\\/]/).filter(Boolean).pop();
const TOKEN="__CSRF__";
let S=null, TAB=localStorage.getItem("tab")||"skills", FILTER="all", ED=null;

function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),3600)}

async function api(p,b){const r=await fetch(p,{method:"POST",
  headers:{"Content-Type":"application/json","X-Hub-Token":TOKEN},
  body:JSON.stringify(b)});return await r.json()}
async function post(p,b){const j=await api(p,b);if(j.out)toast(j.out);await load();return j}

function typing(){ // 正在页面内的输入框里打字时,刷新不要重渲染把内容吞掉
  const a=document.activeElement;
  return a&&["INPUT","TEXTAREA","SELECT"].includes(a.tagName)&&$("#page").contains(a);
}
async function load(){S=await (await fetch("/api/state")).json();
  if(typing())renderNav();else render()}

function show(t){TAB=t;localStorage.setItem("tab",t);render()}

/* ---------- 徽章 ---------- */
function originTag(o){
  if(!o||o.type==="own")return `<span class="tag src-own">自建</span>`;
  if(o.type==="ref")return `<span class="tag src-ref" title="内容是引入时的快照;你手动检查、确认后可跟进上游更新">⇅ ${esc(o.source)} · 跟随更新</span>`;
  return `<span class="tag src-copy" title="从来源复制后已与上游脱钩,归你所有">⧉ ${esc(o.source)} · 独立副本</span>`;
}
function pill(skill,target,label,state,title){
  if(state==="hub-link"||state==="copy-synced")
    return `<span class="pill on" title="点击关闭 · ${esc(title)}" onclick="toggle('${esc(target)}','${skill}',false)">${esc(label)}</span>`;
  if(state==="absent")
    return `<span class="pill" title="点击开启 · ${esc(title)}" onclick="toggle('${esc(target)}','${skill}',true)">${esc(label)}</span>`;
  return `<span class="pill warn" title="${state}:这一处不归本库管,见顶部提示">${esc(label)} ⚠</span>`;
}

/* ---------- 侧边栏 ---------- */
const ICONS={
skills:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
sets:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l9 5-9 5-9-5 9-5z"/><path d="M3 12l9 5 9-5"/><path d="M3 17l9 5 9-5"/></svg>',
usage:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h6l2-7 4 14 2-7h4"/></svg>',
sources:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.7 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.7-3.8-9S9.5 5.6 12 3z"/></svg>',
settings:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.01a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55h.01a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.01a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z"/></svg>'};

function renderNav(){
  const items=[
    ["skills","技能",`<span class="cnt">${S.skills.length}</span>`],
    ["sets","组合",`<span class="cnt">${Object.keys(S.sets).length||""}</span>`],
    ["usage","使用情况",""],
    ["sources","网上来源",""],
    ["settings","设置",""]];
  $("#nav").innerHTML=items.map(([id,label,extra])=>
    `<button class="${TAB===id?'active':''}" onclick="show('${id}')">${ICONS[id]}${label}${extra}</button>`).join("");
  $("#sidefoot").innerHTML=S.platform==="darwin"
    ?`<div class="st ${S.autostart?'on':''}">管理台常驻 ${S.autostart?"运行中":"未注册"}</div>`:"";
}

/* ---------- 技能页 ---------- */
function skillMatch(k){
  const kw=(window._kw||"").toLowerCase();
  if(kw&&!k.name.includes(kw)&&!(k.desc||"").toLowerCase().includes(kw))return false;
  if(FILTER==="own")return !k.origin||k.origin.type==="own";
  if(FILTER==="ext")return k.origin&&(k.origin.type==="ref"||k.origin.type==="copy");
  return true;
}
function projLabel(t){ // "path::kind" -> "目录名(·kind,claude 不标)"
  const [p,kind]=t.split("::");
  return base(p)+(kind&&kind!=="claude"?" ·"+kind:"");
}
function skillCards(){
  return S.skills.filter(skillMatch).map(k=>{
    const projPills=Object.entries(k.places.projects).filter(([t,st])=>st!=="absent")
      .map(([t,st])=>pill(k.name,t,projLabel(t),st,t.replace("::","/.")+"/skills")).join("");
    const isRef=k.origin&&k.origin.type==="ref";
    return `<div class="skcard">
      <div class="sk-head"><span class="sk-name" title="${k.name}">${k.name}</span>${originTag(k.origin)}
        <span class="sk-acts">
          <button class="ghost" onclick="editSkill('${k.name}')">${isRef?"查看":"编辑"}</button>
          ${isRef?`<button class="ghost" title="复制一份归自己,以后可编辑,不再跟随来源更新" onclick="post('/api/source/fork',{name:'${k.name}'})">转副本</button>`:""}
          <button class="ghost danger" onclick="delSkill('${k.name}')">删除</button></span></div>
      <div class="sk-desc" title="${esc(k.desc)}">${esc(k.desc)||'<i>还没写 description</i>'}</div>
      <div class="pills">
        ${pill(k.name,"claude","Claude 全局",k.places.claude,"~/.claude/skills")}
        ${pill(k.name,"codex","Codex 全局",k.places.codex,"~/.codex/skills")}
        ${S.agents_root||k.places.agents!=="absent"?pill(k.name,"agents","Agents 全局",k.places.agents,"~/.agents/skills"):""}
        ${projPills}
        <span class="pill add" title="在某个项目目录里单独启用" onclick="addProject('${k.name}')">＋ 项目</span>
      </div></div>`}).join("")||`<div class="empty" style="grid-column:1/-1">没有匹配的技能</div>`;
}
function pageSkills(){
  const chips=[["all","全部"],["own","自建"],["ext","网上引入"]];
  return `
  <div class="pagehead"><h1>技能</h1>
    <span class="acts">
      <button class="ghost" title="打开技能库目录(library/)" onclick="post('/api/open',{})">打开库目录</button>
      <button title="扫描本机 .claude/.codex/.agents 里还没进库的技能" onclick="scanLocal()">扫描收编</button>
      <button title="从任意文件夹导入技能目录(复制进库)" onclick="importDialog()">导入目录</button>
      <button onclick="show('sources')">从网上添加</button>
      <button class="primary" onclick="newSkill()">＋ 新建技能</button></span>
    <span class="sub">技能保存在库里,删不丢、改全生效。开关拨绿 = 在那个地方能用。</span>
  </div>
  ${onboard()}
  ${S.warnings.map(w=>`<div class="warnbox">⚠ ${esc(w)}</div>`).join("")}
  <div class="row" style="margin-top:14px">
    <input type="text" id="search" placeholder="搜技能名或描述…" style="width:260px"
      value="${esc(window._kw||"")}" oninput="window._kw=this.value;$('#sklist').innerHTML=skillCards()">
    <span class="chips" style="margin:0">${chips.map(([id,l])=>
      `<span class="chip ${FILTER===id?'active':''}" onclick="FILTER='${id}';render()">${l}</span>`).join("")}</span>
  </div>
  <div id="sklist" class="skgrid">${skillCards()}</div>`;
}

/* ---------- 组合页 ---------- */
function pageSets(){
  return `
  <div class="pagehead"><h1>组合</h1>
    <span class="acts"><button class="primary" onclick="newSet()">＋ 新建组合</button></span>
    <span class="sub">把常一起用的技能存成一组,一键开到某个地方、一键关掉。组合只是清单,不影响技能本身。</span></div>
  ${Object.entries(S.sets).map(([n,list])=>`<div class="card">
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">${esc(n)} <span class="hint" style="font-weight:400">${list.length} 个技能</span></h2>
      <span class="row">
        <button onclick="applySet('${esc(n)}',true)">开启到…</button>
        <button class="ghost" onclick="applySet('${esc(n)}',false)">关闭…</button>
        <button class="ghost" onclick="editSet('${esc(n)}')">编辑</button>
        <button class="ghost danger" onclick="delSet('${esc(n)}')">删除</button></span></div>
    <div class="pills" style="margin-top:8px">${list.map(s=>{
      const k=S.skills.find(x=>x.name===s);
      return `<span class="tag ${k?"":"miss"}" title="${k?esc(k.desc):"库里没有这个技能"}">${esc(s)}${k?"":" ?"}</span>`}).join("")
      ||'<span class="hint">(空组合,点「编辑」加技能)</span>'}</div>
  </div>`).join("")||`<div class="empty">还没有组合。把常一起用的技能建一组,以后一键开关。</div>`}`;
}
async function delSet(n){
  if(confirm("删除组合「"+n+"」?只删清单,技能本身不受影响。"))
    await post("/api/set-delete",{name:n});
}
function onboard(){
  if(localStorage.getItem("onboarded4"))return "";
  return `<div class="banner"><b>三句话看懂这个页面</b>
  ① 你所有的 AI 技能都保存在这台电脑的技能库里,删不丢、改全生效。
  ② 每个技能下面有一排开关,拨绿 = 在那里能用,再点一下就关。
  ③ 网上来源的下载、检查更新、合入更新都只在你点击时发生;本工具只负责管理,不验证第三方内容的安全性,引入前请自行阅读。
  <a href="#" onclick="localStorage.setItem('onboarded4','1');render();return false">知道了,不再显示</a></div>`;
}

/* ---------- 使用情况页 ---------- */
function useCard(icon,label,path,target,get){
  const used=S.skills.filter(k=>{const st=get(k);return st&&st!=="absent"});
  return `<div class="usecard">
    <h3><span class="loc-ico">${icon}</span>${esc(label)}<span class="n">${used.length} 个技能</span></h3>
    <div class="path">${esc(path)}</div>
    <div class="pills">${used.map(k=>pill(k.name,target,k.name,get(k),"点击在这里关闭")).join("")||'<span class="hint">这里没开任何技能</span>'}
      <span class="pill add" onclick="pickSkill('${esc(target)}','${esc(label)}')">＋ 开启技能</span></div></div>`;
}
function projCards(){
  const q=(window._pq||"").toLowerCase();
  const pts=S.proj_targets.filter(t=>t.path.toLowerCase().includes(q));
  return pts.map(t=>useCard("📁",projLabel(t.target),t.path+"/."+t.kind+"/skills",
    t.target,k=>k.places.projects[t.target])).join("")
    ||`<div class="empty" style="grid-column:1/-1">${q?"没有匹配的项目":"还没有项目在用技能。在「技能」页点某个技能的「＋ 项目」即可。"}</div>`;
}
function pageUsage(){
  return `
  <div class="pagehead"><h1>使用情况</h1>
    <span class="acts"><button onclick="post('/api/targets/clean',{})">清理失效项目</button></span>
    <span class="sub">每个地方各自开了哪些技能。全局 = 所有会话都能用;项目 = 只在那个目录里能用。</span></div>
  <h2 style="font-size:13px;color:var(--muted);margin:18px 0 0">全局</h2>
  <div class="usegrid">
    ${useCard("C","Claude Code(全局)","~/.claude/skills","claude",k=>k.places.claude)}
    ${useCard("X","Codex(全局)","~/.codex/skills","codex",k=>k.places.codex)}
    ${S.agents_root?useCard("A","Agents(通用,全局)","~/.agents/skills","agents",k=>k.places.agents):""}
  </div>
  <div class="row" style="margin:20px 0 0">
    <h2 style="font-size:13px;color:var(--muted);margin:0">各项目 <span class="hint" style="font-weight:400">${S.proj_targets.length} 处</span></h2>
    ${S.proj_targets.length>4?`<input type="text" id="projq" placeholder="搜项目…" style="width:180px;margin-left:auto"
      value="${esc(window._pq||"")}" oninput="window._pq=this.value;$('#projgrid').innerHTML=projCards()">`:""}
  </div>
  <div class="usegrid" id="projgrid">${projCards()}</div>
  ${S.stale_targets.length?`<div class="warnbox">失效项目(目录已不存在):${S.stale_targets.map(esc).join("、")}</div>`:""}`;
}

/* ---------- 来源页 ---------- */
function pageSources(){
  return `
  <div class="pagehead"><h1>网上来源</h1>
    <span class="sub">别人的技能仓库先下载到本机隔离目录,挑着引入;引入的是当时内容的快照。下载、检查更新、合入更新都只在你点击时发生。<b>本工具只负责管理,不验证第三方内容的安全性——引入或更新前请自行阅读内容。</b></span></div>
  <div class="card">
    <h2>添加技能仓库</h2>
    <div class="row" style="margin-top:8px">
      <input type="text" id="srcUrl" placeholder="https://github.com/xxx/skills.git" style="flex:1;min-width:260px">
      <button class="primary" id="srcAddBtn" onclick="addSource()">下载来源(联网)</button></div></div>
  ${S.sources.map(s=>`<div class="card">
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">${esc(s.name)}</h2>
      <span class="row">${s.is_git?`<button class="ghost" onclick="checkSource('${s.name}')">检查远端更新(联网)</button>`:""}
        <button class="ghost danger" onclick="removeSource('${s.name}')">移除来源</button></span></div>
    <div class="hint mono">${esc(s.url||"(本地目录)")} · ${esc(s.head)}</div>
    <div id="chk-${s.name}"></div>
    <div style="margin-top:8px">${s.skills.map(k=>`<div class="srcskill">
      <b>${esc(k.name)}</b><span class="hint" style="flex:1">${esc(k.desc)}</span>
      ${k.imported_as?`<span class="tag done">已引入为 ${k.imported_as}(${k.imported_type==="ref"?"跟随更新":"独立副本"})</span>`
        :`<button class="ghost" onclick="importSkill('${s.name}','${esc(k.subpath)}','ref')" title="以后可在你手动检查、确认后跟进上游更新">引入 · 跟随更新</button>
          <button class="ghost" onclick="importSkill('${s.name}','${esc(k.subpath)}','copy')" title="复制一份归自己,与来源脱钩">引入 · 独立副本</button>`}
    </div>`).join("")||'<span class="hint">这个仓库里没找到技能</span>'}</div></div>`).join("")
  ||`<div class="empty">还没有添加任何来源。粘贴一个技能仓库地址试试。</div>`}`;
}

/* ---------- 设置页 ---------- */
function pageSettings(){
  return `
  <div class="pagehead"><h1>设置</h1></div>
  <div class="card">
    <h2>行为</h2>
    <label class="hint" style="display:block;margin-top:6px"><input type="checkbox" id="cleanEmpty" ${S.clean_empty_dirs?"checked":""}
      onchange="post('/api/settings',{clean_empty_dirs:this.checked})">
      从项目移除技能后,若 .claude/.codex/.agents 目录已空则一并删掉(保持项目干净;全局目录永不动)</label>
  </div>
  <div class="card">
    <h2>本工具的边界</h2>
    <div class="hint" style="line-height:2">
      · 只管理技能的存储、来源、组合与启用位置,不执行技能内容,不自动下载任何东西。<br>
      · 所有联网动作(下载来源 / 检查更新 / 执行更新)都只在你点击对应按钮时发生。<br>
      · 不验证第三方技能的内容;引入、开启前请自行阅读。</div>
  </div>
  ${S.platform==="darwin"?`<div class="card">
    <h2>后台服务</h2>
    <div class="hint" style="line-height:2.2">
      ${S.autostart?"●":"○"} 管理台常驻(开机自启):${S.autostart?"运行中":"未注册(见 README 配置)"}
    </div>
  </div>`:""}`;
}

/* ---------- 渲染入口 ---------- */
function render(){
  if(!S)return;
  renderNav();
  $("#page").innerHTML={skills:pageSkills,sets:pageSets,usage:pageUsage,sources:pageSources,
                        settings:pageSettings}[TAB]();
}

/* ---------- 交互 ---------- */
async function toggle(target,skill,on){await post("/api/toggle",{target,skill,on})}
const ask=$("#ask");
function askDialog(title,hint,bodyHtml,onOk){
  $("#askTitle").textContent=title;$("#askHint").textContent=hint;
  $("#askBody").innerHTML=bodyHtml;$("#askOk").onclick=async()=>{await onOk();ask.close()};
  ask.showModal();
}
function newSkill(){askDialog("新建技能","名字只能用小写字母、数字、连字符。页面只编辑 SKILL.md;脚本等其他文件建好后用「打开目录」放进技能文件夹。",
  `<input type="text" id="askIn" style="width:100%" placeholder="my-skill">`,
  async()=>{const n=$("#askIn").value.trim();if(!n)return;
    const j=await post("/api/new",{name:n});if(j.ok)editSkill(n)})}
function checkRow(f,extra){ // 扫描/导入结果里的一行(带勾选框)
  const bad=f.conflict?"库里已有同名,跳过":!f.valid?"名字须小写字母/数字/连字符,改名后再来":"";
  return `<label class="srcskill" style="cursor:${bad?"default":"pointer"}">
    <input type="checkbox" data-p="${esc(f.path)}" ${bad?"disabled":"checked"}>
    <b>${esc(f.name)}</b><span class="hint" style="flex:1">${esc(extra||"")}</span>
    ${bad?`<span class="tag miss">${bad}</span>`:""}</label>`;
}
function checkedPaths(sel){return [...document.querySelectorAll(sel+' input:checked')].map(x=>x.dataset.p)}
async function scanLocal(){
  toast("正在扫描本机 .claude/.codex/.agents…");
  const r=await post("/api/scan",{});
  const list=r.found||[];
  if(!list.length){toast("没有发现库外的技能,都已在库里了");return}
  askDialog(`发现 ${list.length} 个库外技能`,"勾选要收进库的,点确定。收编 = 移进库、原位置留引用,用法不变;之后就能统一开关。",
    list.map(f=>checkRow(f,f.place)).join(""),
    async()=>{const ps=checkedPaths("#askBody");if(ps.length)await post("/api/adopt-bulk",{paths:ps})});
}
function importDialog(){
  askDialog("从目录导入技能","支持单个技能目录(内有 SKILL.md),或装着多个技能子目录的文件夹。只认 SKILL.md 这一个标准,其余文件原样保留;导入是复制,原目录不动。",
   `<div class="row"><input type="text" id="impPath" style="flex:1;min-width:0" placeholder="/Users/you/Downloads/xxx-skills">
    <button onclick="impProbe()">查找技能</button></div><div id="impList" style="margin-top:8px"></div>`,
   async()=>{const ps=checkedPaths("#impList");if(ps.length)await post("/api/import",{paths:ps})});
}
async function impProbe(){
  const p=$("#impPath").value.trim();if(!p)return;
  const r=await post("/api/import",{path:p,probe:true});
  if(!r.ok)return;
  $("#impList").innerHTML=(r.found||[]).map(f=>checkRow(f,f.desc)).join("")
    ||'<div class="empty">这个目录里没找到带 SKILL.md 的技能</div>';
}
function addProject(skill){
  const opts=S.projects.map(p=>`<option value="${esc(p)}">`).join("");
  askDialog("在某个项目里用「"+skill+"」","输入项目目录的完整路径,并选择放进哪个目录;这个技能将只对该项目生效",
  `<input type="text" id="askIn" list="projList" style="width:100%" placeholder="/Users/you/my-project">
   <datalist id="projList">${opts}</datalist>
   <div class="row" style="margin-top:10px">
     <label class="hint"><input type="radio" name="pkind" value="claude" checked> .claude(Claude Code)</label>
     <label class="hint"><input type="radio" name="pkind" value="codex"> .codex(Codex)</label>
     <label class="hint"><input type="radio" name="pkind" value="agents"> .agents(通用)</label>
   </div>`,
  async()=>{const p=$("#askIn").value.trim();if(!p)return;
    const kind=document.querySelector('input[name=pkind]:checked').value;
    await toggle(p+"::"+kind,skill,true)})}
function pickSkill(target,label){
  const here=S.skills.filter(k=>{
    const st=["claude","codex","agents"].includes(target)?k.places[target]:(k.places.projects[target]||"absent");
    return st==="absent"});
  askDialog("在「"+label+"」开启技能","点一个立即开启",
    here.map(k=>`<div class="srcskill"><b>${k.name}</b><span class="hint" style="flex:1">${esc(k.desc).slice(0,60)}</span>
      <button class="ghost" onclick="toggle('${esc(target)}','${k.name}',true).then(()=>ask.close())">开启</button></div>`).join("")
    ||'<div class="empty">所有技能都已在这里开启</div>',
    async()=>{});
  $("#askOk").style.display="none";
  ask.addEventListener("close",()=>{$("#askOk").style.display=""},{once:true});
}
function applySet(name,on){
  askDialog((on?"开启":"关闭")+" 组合「"+name+"」","选择作用位置",
  `<select id="askIn" style="width:100%">
    <option value="claude">Claude 全局</option><option value="codex">Codex 全局</option>
    ${S.agents_root?'<option value="agents">Agents 全局</option>':""}
    ${S.proj_targets.map(t=>`<option value="${esc(t.target)}">项目:${esc(t.path)}(.${t.kind})</option>`).join("")}</select>`,
  async()=>{await post("/api/set-apply",{set:name,target:$("#askIn").value,on})})}
async function delSkill(n){
  if(confirm("确定删除「"+n+"」?自建技能会进回收站(不真删),网上引入的只是撤掉快照。"))
    await post("/api/delete",{name:n})}
async function removeSource(n){
  if(confirm("移除来源「"+n+"」?已转为独立副本的技能不受影响。"))
    await post("/api/source/remove",{source:n})}
async function importSkill(source,subpath,mode){await post("/api/source/import",{source,subpath,mode})}
async function addSource(){const u=$("#srcUrl").value.trim();if(!u)return;
  const b=$("#srcAddBtn");b.disabled=true;b.textContent="下载中…";
  try{await post("/api/source/add",{url:u})}finally{b.disabled=false;b.textContent="下载来源(联网)"}}
async function checkSource(name){
  const el=$("#chk-"+name);el.innerHTML='<div class="hint" style="margin-top:8px"><span class="spin"></span> 正在联网检查远端…</div>';
  const r=await api("/api/source/check",{source:name});
  if(!r.ok){el.innerHTML='<div class="warnbox">'+esc(r.out||"检查失败")+'</div>';return}
  if(r.note){el.innerHTML='<div class="hint" style="margin-top:8px">'+esc(r.note)+'</div>';return}
  if(!r.behind){el.innerHTML='<div class="hint" style="margin-top:8px">✓ 已是最新</div>';return}
  el.innerHTML=`<div class="pendbox"><b>远端有 ${r.behind} 个新提交(目标版本 ${esc(r.target)})</b>
    影响 ${r.affected.length} 个跟随更新的技能(${r.affected.map(a=>a.skill).join("、")||"无"})。
    先看下面的提交列表和受影响文件,确认后再更新;更新只会前进到上面这个版本。
    ${r.affected.length?`<pre>${esc(r.affected.map(a=>a.skill+":\n  "+a.files.join("\n  ")).join("\n"))}</pre>`:""}
    <pre>${esc(r.commits)}</pre>
    <div class="row" style="margin-top:8px">
      <button class="primary" onclick="updateSource('${name}','${esc(r.token)}')">更新已关联技能到该版本</button>
    </div></div>`;
}
async function updateSource(name,token){
  const el=$("#chk-"+name);
  el.innerHTML='<div class="hint" style="margin-top:8px"><span class="spin"></span> 正在同步快照…</div>';
  await post("/api/source/update",{source:name,token});
  if($("#chk-"+name))$("#chk-"+name).innerHTML="";
}
const editor=$("#editor");
async function editSkill(name){
  const j=await (await fetch("/api/skill?name="+encodeURIComponent(name))).json();
  if(!j.ok){toast(j.out);return}
  ED={type:"skill",name};
  $("#edTitle").textContent=(j.readonly?"查看 ":"编辑 ")+name;
  $("#edHint").textContent=j.readonly?"跟随更新的技能只读;想改就先「转为我的副本」":"这里只编辑 SKILL.md;脚本等其他文件用「打开目录」放进去。保存即处处生效";
  $("#edSave").style.display=j.readonly?"none":"";
  $("#edOpen").style.display="";
  $("#edBody").value=j.content;editor.showModal();
}
function editSet(name){ED={type:"set",name};$("#edTitle").textContent="编辑组合 "+name;
  $("#edHint").textContent="一行一个技能名,# 开头是注释";$("#edSave").style.display="";
  $("#edOpen").style.display="none";
  $("#edBody").value=S.sets_raw[name]||"";editor.showModal()}
function newSet(){askDialog("新建组合","组合名(小写字母、数字、连字符)",
  `<input type="text" id="askIn" style="width:100%" placeholder="my-set">`,
  async()=>{const n=$("#askIn").value.trim();if(!n)return;
    ED={type:"set",name:n};$("#edTitle").textContent="新建组合 "+n;
    $("#edHint").textContent="一行一个技能名";$("#edSave").style.display="";
    $("#edOpen").style.display="none";
    $("#edBody").value="# 什么时候用这组\n";editor.showModal()})}
async function saveEditor(){await post(ED.type==="skill"?"/api/skill":"/api/set",{name:ED.name,content:$("#edBody").value});editor.close()}

load();
</script></body></html>
"""


def ensure_hub():
    """首次运行时把库目录和本地 git 仓库(变更历史 = 回滚路径)准备好。纯本地,不联网。"""
    LIB.mkdir(parents=True, exist_ok=True)
    SETS.mkdir(parents=True, exist_ok=True)
    NO_HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    if not (HUB / ".git").exists():
        git(["init"], cwd=HUB)
        git_commit("初始化技能库")


def serve(port=PORT, open_browser=True):
    global SERVER_PORT
    os.chdir(HUB)
    ensure_hub()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        print(f"端口 {port} 已被占用,管理台可能已在运行。")
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{port}")
        return None
    SERVER_PORT = port
    print(f"skills-hub 管理台: http://127.0.0.1:{port}")
    if open_browser:
        threading.Timer(0.6, webbrowser.open, args=[f"http://127.0.0.1:{port}"]).start()
    return srv


def main():
    port = PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    srv = serve(port, open_browser="--no-open" not in sys.argv)
    if srv is None:
        return
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
