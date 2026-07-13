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
    divergences = []     # 结构化:每个"独立副本已和库不同"的技能,供前端做可点击入口
    roots = [(k, f"{k.capitalize()} 全局", ROOTS[k]) for k in KINDS] + \
            [(f"{t['path']}::{t['kind']}",
              f"项目 {t['path']}(.{t['kind']})",
              Path(t["path"]) / f".{t['kind']}" / "skills") for t in proj_targets]
    few = lambda names: "、".join(names[:3]) + (" 等" if len(names) > 3 else "")
    for target, label, root in roots:
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
            divergences += [{"name": n, "target": target, "label": label} for n in diverged]
        if unmanaged:
            warnings.append(f"{label} 有 {len(unmanaged)} 个技能还没进库({few(unmanaged)}),可用「扫描本机技能」接管")
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
            "stale_targets": stale, "warnings": warnings, "divergences": divergences,
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


def op_scan_local(b):
    """扫描全局与/或各项目的 .claude/.codex/.agents,找出还没进库的技能。纯本地文件遍历。
    scope: "all"(默认)| "global"(只看 ~/.claude 等全局目录)| "project"(只看已登记项目)。"""
    scope = b.get("scope") or "all"
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

    if scope in ("all", "global"):
        for k in KINDS:
            check(ROOTS[k], f"{k.capitalize()} 全局")
    if scope in ("all", "project"):
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


def op_pick_dir(b):
    """弹出系统原生的目录选择框,把选中的路径回填到导入框。仅本地可信环境使用,
    服务只监听 127.0.0.1,不会被远程触发。"""
    start = (b.get("start") or "").strip()
    start = str(Path(start).expanduser()) if start and Path(start).expanduser().is_dir() else str(Path.home())
    try:
        if sys.platform == "darwin":
            r = sh(["osascript", "-e",
                    f'POSIX path of (choose folder with prompt "选择要导入的目录" '
                    f'default location (POSIX file "{start}"))'])
            if r.returncode != 0:
                return {"ok": False, "out": ""}  # 用户取消,不算错误
            return {"ok": True, "path": r.stdout.strip()}
        if os.name == "nt":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
                  f"$f.SelectedPath='{start}';"
                  "if($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){Write-Output $f.SelectedPath}")
            r = sh(["powershell", "-NoProfile", "-Command", ps])
            path = r.stdout.strip()
            return {"ok": bool(path), "path": path}
        for cmd in (["zenity", "--file-selection", "--directory", f"--filename={start}/"],
                    ["kdialog", "--getexistingdirectory", start]):
            if shutil.which(cmd[0]):
                r = sh(cmd)
                return {"ok": r.returncode == 0 and bool(r.stdout.strip()), "path": r.stdout.strip()}
        return {"ok": False, "out": "没找到系统目录选择器(zenity/kdialog),请在输入框里手动填路径"}
    except Exception as e:
        return {"ok": False, "out": f"打开目录选择框失败:{e}"}


def op_relink(b):
    """把某个放置点上"内容已和库里不同"的独立副本收回为软链接。

    独立副本 = 真目录(非软链接),且内容与 library 不一致。此操作把旧副本
    备份进 attic/trash,再重建一条指向 library 的软链接,使该处重新跟随库。
    库内容视为真源,副本里的本地改动只进备份、不回写库。
    """
    name = (b.get("name") or "").strip()
    target = (b.get("target") or "").strip()
    if not NAME_RE.match(name) or not (LIB / name).is_dir():
        return {"ok": False, "out": "技能不存在"}
    dest = resolve_place(target)
    e = dest / name
    st = entry_state(e, name)
    if st != "copy-diverged":
        return {"ok": False, "out": f"「{name}」在该处不是独立副本(状态:{st}),无需收回"}
    trash = HUB / "attic" / "trash" / datetime.now().strftime("%Y%m%d-%H%M%S")
    trash.mkdir(parents=True, exist_ok=True)
    shutil.move(str(e), str(trash / name))
    make_link(e, LIB / name)
    git_commit(f"收回 {name} 副本为软链接({target})")
    return {"ok": True,
            "out": f"已把「{name}」收回为软链接(跟随库);旧副本备份在 attic/trash"}


def op_diff(b):
    """只读:返回某放置点副本与库内容的统一 diff。不联网、不写盘。"""
    name = (b.get("name") or "").strip()
    target = (b.get("target") or "").strip()
    if not NAME_RE.match(name) or not (LIB / name).is_dir():
        return {"ok": False, "out": "技能不存在"}
    dest = resolve_place(target)
    e = dest / name
    if not e.is_dir():
        return {"ok": False, "out": "该处没有这个技能的副本"}
    import difflib
    parts = []          # 拼成一段 unified diff 文本
    lib = LIB / name

    def walk(a: Path, b: Path, rel=""):
        a_files = {p.name for p in a.iterdir() if not p.name.startswith(".")} if a.is_dir() else set()
        b_files = {p.name for p in b.iterdir() if not p.name.startswith(".")} if b.is_dir() else set()
        for fn in sorted(a_files | b_files):
            ap, bp, r = a / fn, b / fn, (rel + "/" + fn).lstrip("/")
            if ap.is_dir() and bp.is_dir():
                walk(ap, bp, r)
            elif ap.is_dir() or bp.is_dir():
                parts.append(f"文件/目录类型不同:{r}")
            else:
                at = ap.read_text(errors="replace").splitlines(keepends=True) if ap.is_file() else []
                bt = bp.read_text(errors="replace").splitlines(keepends=True) if bp.is_file() else []
                if at != bt:
                    parts.extend(difflib.unified_diff(
                        at, bt, fromfile=f"library/{name}/{r}", tofile=f"{target}/{name}/{r}"))
    walk(lib, e)
    return {"ok": True, "diff": "".join(parts) or "(内容相同)"}


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
    "/api/import": op_import, "/api/open": op_open, "/api/pick-dir": op_pick_dir,
    "/api/relink": op_relink, "/api/diff": op_diff,
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
/* ---- 拆分按钮(主操作 + 下拉次选项) ---- */
.splitbtn{display:inline-flex;position:relative}
.splitbtn>button{border-radius:var(--rs) 0 0 var(--rs);border-right:none}
.splitbtn>details.dropdown{position:relative}
.splitbtn>details.dropdown>summary{list-style:none;display:flex;align-items:center;justify-content:center;
  width:26px;height:100%;font-size:13px;padding:6px 6px;border-radius:0 var(--rs) var(--rs) 0;
  border:1px solid var(--line);background:var(--card);color:var(--muted);cursor:pointer;
  transition:border-color .12s,color .12s}
.splitbtn>details.dropdown>summary::-webkit-details-marker{display:none}
.splitbtn>details.dropdown>summary:hover{border-color:var(--accent);color:var(--accent-ink)}
.splitbtn>details.dropdown[open]>summary{border-color:var(--accent);color:var(--accent-ink)}
.dropdown-menu{position:absolute;top:calc(100% + 6px);right:0;min-width:220px;z-index:20;
  background:var(--card);border:1px solid var(--line);border-radius:var(--rs);box-shadow:0 6px 20px rgba(16,24,40,.12);
  padding:5px;display:flex;flex-direction:column;gap:2px}
.dropdown-menu button{width:100%;text-align:left;border:none;background:none;padding:7px 9px;
  border-radius:6px;display:flex;flex-direction:column;gap:1px;align-items:flex-start}
.dropdown-menu button:hover{background:var(--accent-soft);border-color:transparent;color:var(--ink)}
.dropdown-menu button .dd-sub{font-size:11px;color:var(--faint);font-weight:400}
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
.sk-name{font-weight:650;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
flex:1 1 auto;min-width:0}
.sk-desc{color:var(--muted);font-size:12px;line-height:1.55;margin:3px 0 8px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sk-acts{margin-left:auto;display:flex;flex:none;gap:2px}
.sk-acts button{padding:2px 7px;font-size:12px;line-height:1.4}
.src-link{color:inherit;text-decoration:none;cursor:pointer;max-width:110px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap;display:inline-block;vertical-align:bottom}
.src-link:hover{text-decoration:underline}
/* 勾选式技能卡(组合编辑器用):整卡可点,勾上高亮 */
.skcard.sel{cursor:pointer}
.skcard.sel:hover{border-color:var(--accent)}
.skcard.sel.on{background:var(--accent-soft);border-color:var(--accent)}
.skcard.sel .se-head{display:flex;align-items:center;gap:7px;min-width:0}
.skcard.sel input[type=checkbox]{flex:none;width:15px;height:15px;cursor:pointer}
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
.diff-pre{background:var(--panel);border:1px solid var(--line);border-radius:var(--rs);
padding:12px 14px;max-height:58vh;overflow:auto;font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
white-space:pre-wrap;word-break:break-word;margin:0}
.df-h{color:var(--muted)}
.df-add{color:var(--ok)}
.df-del{color:var(--bad)}
.warnbox .dv-item{display:inline-block;background:rgba(0,0,0,.05);border-radius:6px;padding:1px 7px;margin:2px 4px 2px 0}
.warnbox .dv-link{color:inherit;text-decoration:underline;cursor:pointer;font-size:12px;margin-left:4px;opacity:.85}
.warnbox .dv-link:hover{opacity:1}
code{background:rgba(0,0,0,.07);border-radius:4px;padding:0 4px;font:inherit;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em}
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
  <div class="brand"><div class="logo">✦</div><div><b data-i18n="app_name">技能库</b><span class="sub" data-i18n="app_sub">一处管理 · 处处可用</span></div></div>
  <nav class="nav" id="nav"></nav>
  <div class="sidefoot" id="sidefoot"></div>
</aside>

<div class="main"><div class="page" id="page"></div></div>

<dialog id="editor">
  <h2 id="edTitle"></h2>
  <textarea id="edBody" style="height:56vh" spellcheck="false"></textarea>
  <div class="row" style="justify-content:flex-end;margin-top:12px">
    <span class="hint" id="edHint" style="margin-right:auto"></span>
    <button class="ghost" id="edOpen" data-i18n-title="t_open_dir" data-i18n="ed_open_skill" onclick="post('/api/open',{name:ED.name})">打开目录</button>
    <button data-i18n="ed_cancel" onclick="editor.close()">取消</button>
    <button class="primary" id="edSave" data-i18n="ed_save" onclick="saveEditor()">保存</button>
  </div>
</dialog>

<dialog id="ask">
  <h2 id="askTitle"></h2>
  <div class="hint" id="askHint" style="margin-bottom:10px"></div>
  <div id="askBody"></div>
  <div class="row" style="justify-content:flex-end;margin-top:14px">
    <button data-i18n="ask_cancel" onclick="ask.close()">取消</button>
    <button class="primary" id="askOk" data-i18n="ask_ok">确定</button>
  </div>
</dialog>

<dialog id="diff">
  <h2 id="diffTitle" data-i18n="diff_title">差异</h2>
  <div class="hint" id="diffHint" style="margin-bottom:8px" data-i18n="diff_hint">左 library/=库(真源),右=该放置点副本。绿行=库里有的, 红行=副本独有的改动。</div>
  <pre id="diffBody" class="diff-pre"></pre>
  <div class="row" style="justify-content:flex-end;margin-top:12px">
    <button data-i18n="diff_close" onclick="diff.close()">关闭</button>
    <button class="primary" id="diffRelink" data-i18n="diff_relink">收回为软链接</button>
  </div>
</dialog>

<dialog id="setEditor">
  <h2 id="seTitle" data-i18n="nav_sets">组合</h2>
  <div class="row" style="gap:8px;margin-bottom:4px">
    <label class="hint" style="white-space:nowrap" data-i18n="se_name_label">组合名</label>
    <input type="text" id="setName" style="flex:1" data-i18n-ph="se_ph" placeholder="my-set(小写字母、数字、连字符)">
  </div>
  <div class="hint" style="margin-bottom:8px" data-i18n="se_hint">勾选要放进这组的技能;悬浮卡片可看完整说明。</div>
  <div class="skgrid" id="seCards" style="max-height:52vh;overflow-y:auto;align-content:start"></div>
  <div class="row" style="justify-content:flex-end;margin-top:12px">
    <button data-i18n="se_cancel" onclick="setEditor.close()">取消</button>
    <button class="primary" data-i18n="se_save" onclick="saveSetEditor()">保存</button>
  </div>
</dialog>

<dialog id="srcCheck">
  <h2 id="srcChkTitle" data-i18n="t_check_update">检查更新</h2>
  <div id="srcChkBody" style="min-height:60px"></div>
  <div class="row" style="justify-content:flex-end;margin-top:12px">
    <button data-i18n="sc_close" onclick="srcCheck.close()">关闭</button>
  </div>
</dialog>

<div class="toast" id="toast"></div>

<script>
const $=s=>document.querySelector(s);
const esc=s=>(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
const base=p=>p.split(/[\\/]/).filter(Boolean).pop();
const TOKEN="__CSRF__";
let S=null, TAB=localStorage.getItem("tab")||"skills", FILTER="all", ED=null, SE_ORIG=null;
let LANG=localStorage.getItem("lang")||detectLang();
function detectLang(){
  const bl=(navigator.language||navigator.languages?.[0]||"").toLowerCase();
  return bl.startsWith("en")?"en":"zh";
}

const I18N={
zh:{
app_name:"技能库",app_sub:"一处管理 · 处处可用",
nav_skills:"技能",nav_sets:"组合",nav_usage:"使用情况",nav_sources:"网上来源",nav_settings:"设置",
autostart_on:"管理台常驻 运行中",autostart_off:"管理台常驻 未注册",lang_switch:"EN",
// 技能页
h_skills:"技能",sub_skills:"技能保存在库里,删不丢、改全生效。开关拨绿 = 在那个地方能用。",
btn_open_lib:"打开库目录",btn_scan:"扫描本机技能",btn_import:"导入目录",btn_browse:"浏览…",btn_add_online:"从网上添加",btn_new_skill:"＋ 新建技能",
t_scan_range:"在这些位置里找还没进库的技能:",t_scan_range_proj:"已登记项目(各自的 .claude/.codex/.agents/skills):",
dd_more:"更多扫描范围",dd_scan_global:"只扫描全局目录",dd_scan_global_hint:".claude/skills、.codex/skills、.agents/skills",
dd_scan_project:"只扫描项目目录",dd_scan_project_empty:"还没有登记任何项目,先在某个技能卡片上点「＋项目」",
t_import_hint:"导入目录 = 你指定任意目录(单个技能或一堆技能),复制进库,原目录不动。跟「扫描本机技能」的区别:那个只在固定的几个位置(全局 + 已登记项目)里找,这个你想导哪就导哪。",
ph_search:"搜技能名或描述…",chip_all:"全部",chip_own:"自建",chip_ext:"网上引入",
empty_skills:"没有匹配的技能",no_desc:"还没写 description",
pill_claude:"Claude 全局",pill_codex:"Codex 全局",pill_agents:"Agents 全局",pill_add_proj:"＋ 项目",
t_edit:"编辑",t_view:"查看",t_check_update:"检查上游更新",t_fork:"转为独立副本,以后可编辑,不再跟随来源",t_delete:"删除",
t_open_dir:"打开这个技能的文件夹,放脚本等其他文件",t_open_dir_btn:"打开目录",
t_add_proj_title:"在某个项目目录里单独启用",
// 警告
w_diverged:"独立副本内容已和库里不同",w_view_diff:"查看差异",w_relink:"收回为软链接",
// onboard
ob_title:"三句话看懂这个页面",ob_1:"① 你所有的 AI 技能都保存在这台电脑的技能库里,删不丢、改全生效。",
ob_2:"② 每个技能下面有一排开关,拨绿 = 在那里能用,再点一下就关。",
ob_3:"③ 网上来源的下载、检查更新、合入更新都只在你点击时发生;本工具只负责管理,不验证第三方内容的安全性,引入前请自行阅读。",
ob_gotit:"知道了,不再显示",
// 组合页
h_sets:"组合",sub_sets:"把常一起用的技能存成一组,一键开到某个地方、一键关掉。组合只是清单,不影响技能本身。",
btn_new_set:"＋ 新建组合",n_skills:"个技能",btn_apply:"开启到…",btn_close:"关闭…",btn_edit:"编辑",btn_delete:"删除",
empty_sets:"还没有组合。把常一起用的技能建一组,以后一键开关。",empty_set_hint:"(空组合,点「编辑」加技能)",
set_missing:"库里没有这个技能",
// 使用情况页
h_usage:"使用情况",sub_usage:"每个地方各自开了哪些技能。全局 = 所有会话都能用;项目 = 只在那个目录里能用。",
btn_clean:"清理失效项目",h_global:"全局",h_projects:"各项目",n_places:"处",ph_search_proj:"搜项目…",
empty_proj_new:"还没有项目在用技能。在「技能」页点某个技能的「＋ 项目」即可。",empty_proj_match:"没有匹配的项目",
use_n_skills:"个技能",use_no_skill:"这里没开任何技能",use_pick:"＋ 开启技能",use_close_title:"点击在这里关闭",
cc_claude:"Claude Code(全局)",cc_codex:"Codex(全局)",cc_agents:"Agents(通用,全局)",
stale_proj:"失效项目(目录已不存在):",
// 来源页
h_sources:"网上来源",
sub_sources:"别人的技能仓库先下载到本机隔离目录,挑着引入;引入的是当时内容的快照。下载、检查更新、合入更新都只在你点击时发生。",
sub_sources_b:"本工具只负责管理,不验证第三方内容的安全性--引入或更新前请自行阅读内容。",
h_add_repo:"添加技能仓库",btn_download:"下载来源(联网)",btn_check_remote:"检查远端更新(联网)",
btn_remove_src:"移除来源",btn_expand:"展开",btn_collapse:"收起",skill_list_suffix:"技能列表",
n_imported:"已引入",local_dir:"(本地目录)",empty_src:"还没有添加任何来源。粘贴一个技能仓库地址试试。",
empty_src_skills:"这个仓库里没找到技能",
imp_ref:"引入 · 跟随更新",imp_copy:"引入 · 独立副本",
imp_ref_title:"以后可在你手动检查、确认后跟进上游更新",imp_copy_title:"复制一份归自己,与来源脱钩",
tag_imported_ref:"跟随更新",tag_imported_copy:"独立副本",tag_imported_prefix:"已引入为",
// 设置页
h_settings:"设置",h_behavior:"行为",clean_empty_label:"从项目移除技能后,若 .claude/.codex/.agents 目录已空则一并删掉(保持项目干净;全局目录永不动)",
h_boundary:"本工具的边界",
bnd_1:"· 只管理技能的存储、来源、组合与启用位置,不执行技能内容,不自动下载任何东西。",
bnd_2:"· 所有联网动作(下载来源 / 检查更新 / 执行更新)都只在你点击对应按钮时发生。",
bnd_3:"· 不验证第三方技能的内容;引入、开启前请自行阅读。",
h_service:"后台服务",svc_on:"● 管理台常驻(开机自启):运行中",svc_off:"○ 管理台常驻(开机自启):未注册(见 README 配置)",
// dialog 通用
cancel:"取消",save:"保存",ok:"确定",close:"关闭",confirm_btn:"确定",
// editor dialog
ed_open_skill:"打开目录",ed_save:"保存",ed_cancel:"取消",
// ask dialog
ask_cancel:"取消",ask_ok:"确定",
// diff dialog
diff_title:"差异",diff_hint:"左 library/=库(真源),右=该放置点副本。绿行=库里有的, 红行=副本独有的改动。",
diff_close:"关闭",diff_relink:"收回为软链接",
// setEditor dialog
se_name_label:"组合名",se_hint:"勾选要放进这组的技能;悬浮卡片可看完整说明。",se_cancel:"取消",se_save:"保存",
se_ph:"my-set(小写字母、数字、连字符)",
// srcCheck dialog
sc_close:"关闭",
// JS 动态消息
m_new_skill_t:"新建技能",m_new_skill_h:"名字只能用小写字母、数字、连字符。页面只编辑 SKILL.md;脚本等其他文件建好后用「打开目录」放进技能文件夹。",
m_scan_toast:"正在扫描本机 .claude/.codex/.agents…",m_scan_none:"没有发现库外的技能,都已在库里了",
m_scan_t:"发现 COUNT 个库外技能",m_scan_h:"勾选要收进库的,点确定。收编 = 移进库、原位置留引用,用法不变;之后就能统一开关。",
m_import_t:"从目录导入技能",m_import_h:"支持单个技能目录(内有 SKILL.md),或装着多个技能子目录的文件夹。只认 SKILL.md 这一个标准,其余文件原样保留;导入是复制,原目录不动。",
m_import_find:"查找技能",m_import_empty:"这个目录里没找到带 SKILL.md 的技能",
m_imp_ph:"/Users/you/Downloads/xxx-skills",
m_addproj_t:"在某个项目里用「SKILL」",m_addproj_h:"输入项目目录的完整路径,并选择放进哪个目录;这个技能将只对该项目生效",
m_addproj_ph:"/Users/you/my-project",
m_pick_t:"在「LABEL」开启技能",m_pick_h:"点一个立即开启",m_pick_open:"开启",m_pick_empty:"所有技能都已在这里开启",
m_apply_t:"ACT 组合「NAME」",m_apply_h:"选择作用位置:从下面选一个,或在下方填自定义项目目录",
m_apply_sel:"选择已注册的位置",m_apply_custom:"或填一个自定义项目目录(留空则用上面选的)",
m_del_skill:"确定删除「NAME」?自建技能会进回收站(不真删),网上引入的只是撤掉快照。",
m_del_set:"删除组合「NAME」?只删清单,技能本身不受影响。",
m_relink_c:"把「NAME」在 WHERE 的独立副本收回为软链接(跟随库)?\n\n库内容视为真源。副本里的本地改动会备份到 attic/trash(不真删),之后该处跟随库。",
m_remove_src_c:"移除来源「NAME」?已转为独立副本的技能不受影响。",
m_remove_blocked:"⚠ 还有 COUNT 个\"跟随更新\"的技能引用此来源,无法删除来源仓库(它们还需要仓库做检查更新):",
m_remove_fork:"转副本",m_remove_fork_hint:"转副本后即可删除来源",m_remove_toast:"来源被跟随更新的技能引用,无法删除(见卡片内提示)",
m_remove_hint:"已引入的技能在「技能」页用「检查更新」跟进上游。",
m_dl_ing:"下载中…",m_chk_remote_t:"检查更新 · NAME",m_chk_ing:"正在联网检查远端…",m_chk_fail:"检查失败",
m_chk_latest:"✓ 已是最新",m_chk_behind:"远端有 COUNT 个新提交(目标版本 VER)",
m_chk_affect:"影响 COUNT 个跟随更新的技能(NAMES)。先看下面的提交列表和受影响文件,确认后再更新;更新只会前进到上面这个版本。",
m_chk_update_btn:"更新已关联技能到该版本",m_sync_ing:"正在同步快照…",m_sync_done:"✓ 更新完成",
m_diff_ing:"正在比较…",m_diff_fail:"读取失败",m_diff_none:"(无差异)",
m_diff_title:"差异:NAME",
m_ed_ref:"查看 NAME",m_ed_edit:"编辑 NAME",
m_ed_hint_ref:"跟随更新的技能只读;想改就先「转为我的副本」",m_ed_hint_own:"这里只编辑 SKILL.md;脚本等其他文件用「打开目录」放进去。保存即处处生效",
m_se_edit:"编辑组合",m_se_new:"新建组合",m_se_empty:"库里还没有技能",
m_se_name_err:"组合名只能用小写字母、数字、连字符",m_se_dup:"组合「NAME」已存在,换一个名字或用编辑改它",
m_no_desc_i:"还没写 description",
m_row_conflict:"库里已有同名,跳过",m_row_invalid:"名字须小写字母/数字/连字符,改名后再来",
m_proj_label:"项目",m_empty_count:"处",
radio_claude:".claude(Claude Code)",radio_codex:".codex(Codex)",radio_agents:".agents(通用)"
},
en:{
app_name:"Skills Hub",app_sub:"Manage once · Use everywhere",
nav_skills:"Skills",nav_sets:"Sets",nav_usage:"Usage",nav_sources:"Sources",nav_settings:"Settings",
autostart_on:"Service: Running",autostart_off:"Service: Not registered",lang_switch:"中文",
h_skills:"Skills",sub_skills:"Skills live in the library. Toggle green = available there. Changes propagate everywhere.",
btn_open_lib:"Open Library",btn_scan:"Scan Local Skills",btn_import:"Import Dir",btn_browse:"Browse…",btn_add_online:"Add Online",btn_new_skill:"＋ New Skill",
t_scan_range:"Looks for unmanaged skills in these locations:",t_scan_range_proj:"Registered projects (each project's .claude/.codex/.agents/skills):",
dd_more:"More scan scopes",dd_scan_global:"Scan global dirs only",dd_scan_global_hint:".claude/skills, .codex/skills, .agents/skills",
dd_scan_project:"Scan project dirs only",dd_scan_project_empty:"No registered projects yet — click \"+ Project\" on a skill card first",
t_import_hint:"Import Dir = pick any directory (one skill, or a folder of skills) and copy it into the library, original untouched. Unlike \"Scan Local Skills\", which only looks in fixed locations (global + registered projects), this can import from anywhere.",
ph_search:"Search name or description…",chip_all:"All",chip_own:"Own",chip_ext:"Imported",
empty_skills:"No matching skills",no_desc:"No description yet",
pill_claude:"Claude Global",pill_codex:"Codex Global",pill_agents:"Agents Global",pill_add_proj:"＋ Project",
t_edit:"Edit",t_view:"View",t_check_update:"Check upstream for updates",t_fork:"Convert to standalone copy (editable, no longer follows source)",t_delete:"Delete",
t_open_dir:"Open this skill's folder to add scripts and other files",t_open_dir_btn:"Open Dir",
t_add_proj_title:"Enable in a specific project directory",
w_diverged:"standalone copies differ from library",w_view_diff:"View diff",w_relink:"Relink to library",
ob_title:"This page in 3 sentences",ob_1:"① All your AI skills are stored locally on this machine. Changes propagate everywhere.",
ob_2:"② Each skill has a row of toggles. Green = enabled there. Click again to disable.",
ob_3:"③ Downloads, update checks, and merges only happen when you click. This tool manages only — it does not vet third-party content. Read before importing.",
ob_gotit:"Got it, don't show again",
h_sets:"Sets",sub_sets:"Group skills you use together. Toggle a set on/off to a location. Sets are just lists — they don't modify skills.",
btn_new_set:"＋ New Set",n_skills:"skills",btn_apply:"Apply to…",btn_close:"Remove from…",btn_edit:"Edit",btn_delete:"Delete",
empty_sets:"No sets yet. Group skills you use together for one-click toggling.",empty_set_hint:"(empty set, click Edit to add skills)",
set_missing:"This skill is not in the library",
h_usage:"Usage",sub_usage:"Which skills are enabled where. Global = all sessions; Project = that directory only.",
btn_clean:"Clean Stale Projects",h_global:"Global",h_projects:"Projects",n_places:"places",ph_search_proj:"Search projects…",
empty_proj_new:"No projects using skills yet. Click ＋ Project on a skill in the Skills page.",empty_proj_match:"No matching projects",
use_n_skills:"skills",use_no_skill:"No skills enabled here",use_pick:"＋ Enable Skill",use_close_title:"Click to disable here",
cc_claude:"Claude Code (global)",cc_codex:"Codex (global)",cc_agents:"Agents (general, global)",
stale_proj:"Stale projects (directory no longer exists): ",
h_sources:"Online Sources",
sub_sources:"Clone skill repos to an isolated local directory, then pick skills to import. Imports are snapshots of the current version. Downloads and updates only happen when you click.",
sub_sources_b:"This tool manages only — it does not vet third-party content. Read before importing or updating.",
h_add_repo:"Add Skill Repository",btn_download:"Download (online)",btn_check_remote:"Check Remote Updates (online)",
btn_remove_src:"Remove Source",btn_expand:"Expand",btn_collapse:"Collapse",skill_list_suffix:"skill list",
n_imported:"imported",local_dir:"(local directory)",empty_src:"No sources added yet. Paste a skill repo URL to try.",
empty_src_skills:"No skills found in this repo",
imp_ref:"Import · Follow Updates",imp_copy:"Import · Standalone Copy",
imp_ref_title:"Manually check and follow upstream updates later",imp_copy_title:"Copy as your own, detached from upstream",
tag_imported_ref:"Follow Updates",tag_imported_copy:"Standalone Copy",tag_imported_prefix:"Imported as",
h_settings:"Settings",h_behavior:"Behavior",clean_empty_label:"After removing a skill from a project, delete empty .claude/.codex/.agents dirs (keeps projects clean; global dirs are never touched)",
h_boundary:"Boundaries",
bnd_1:"· Only manages skill storage, sources, sets, and enable locations. Does not execute skill content or auto-download anything.",
bnd_2:"· All network actions (download / check updates / apply updates) only happen when you click the corresponding button.",
bnd_3:"· Does not vet third-party skill content. Read before importing or enabling.",
h_service:"Background Service",svc_on:"● Service (auto-start): Running",svc_off:"○ Service (auto-start): Not registered (see README)",
cancel:"Cancel",save:"Save",ok:"OK",close:"Close",confirm_btn:"OK",
ed_open_skill:"Open Dir",ed_save:"Save",ed_cancel:"Cancel",
ask_cancel:"Cancel",ask_ok:"OK",
diff_title:"Diff",diff_hint:"Left library/=source of truth, right=copy at this location. Green=library-only, Red=copy-only changes.",
diff_close:"Close",diff_relink:"Relink to Library",
se_name_label:"Set name",se_hint:"Check skills to include in this set. Hover a card for full description.",se_cancel:"Cancel",se_save:"Save",
se_ph:"my-set (lowercase, digits, hyphens)",
sc_close:"Close",
m_new_skill_t:"New Skill",m_new_skill_h:"Name must be lowercase, digits, hyphens. Only SKILL.md is edited here; add scripts and other files via Open Dir.",
m_scan_toast:"Scanning local .claude/.codex/.agents…",m_scan_none:"No unmanaged skills found — all are already in the library",
m_scan_t:"Found COUNT unmanaged skills",m_scan_h:"Check the ones to adopt. Adopt = move into library, leave a link at the original location. Usage unchanged.",
m_import_t:"Import from Directory",m_import_h:"Supports a single skill directory (with SKILL.md) or a folder of skill subdirectories. Only SKILL.md is recognized; other files are preserved as-is. Import copies — original is untouched.",
m_import_find:"Find Skills",m_import_empty:"No skills with SKILL.md found in this directory",
m_imp_ph:"/Users/you/Downloads/xxx-skills",
m_addproj_t:"Enable \"SKILL\" in a Project",m_addproj_h:"Enter the full project directory path and choose which folder. The skill will only be available in that project.",
m_addproj_ph:"/Users/you/my-project",
m_pick_t:"Enable Skill in \"LABEL\"",m_pick_h:"Click one to enable immediately",m_pick_open:"Enable",m_pick_empty:"All skills are already enabled here",
m_apply_t:"ACT set \"NAME\"",m_apply_h:"Choose a target: pick from the list below, or enter a custom project directory",
m_apply_sel:"Select a registered location",m_apply_custom:"Or enter a custom project directory (leave empty to use the selection above)",
m_del_skill:"Delete \"NAME\"? Own skills go to trash (not permanently deleted). Imported skills just drop the snapshot.",
m_del_set:"Delete set \"NAME\"? Only the list is removed; skills are unaffected.",
m_relink_c:"Relink the standalone copy of \"NAME\" at WHERE to the library (follow library)?\n\nLibrary content is the source of truth. Local changes in the copy will be backed up to attic/trash (not deleted).",
m_remove_src_c:"Remove source \"NAME\"? Skills already converted to standalone copies are unaffected.",
m_remove_blocked:"⚠ COUNT \"Follow Updates\" skill(s) still reference this source — cannot delete (they need the repo for update checks):",
m_remove_fork:"Make Copy",m_remove_fork_hint:"Convert to copy, then you can delete the source",m_remove_toast:"Source is referenced by follow-update skills — cannot delete (see card hint)",
m_remove_hint:"Imported skills can check for updates from the Skills page.",
m_dl_ing:"Downloading…",m_chk_remote_t:"Check Updates · NAME",m_chk_ing:"Checking remote…",m_chk_fail:"Check failed",
m_chk_latest:"✓ Up to date",m_chk_behind:"Remote has COUNT new commit(s) (target version VER)",
m_chk_affect:"Affects COUNT follow-update skill(s): NAMES. Review the commits and affected files below before updating. Update only advances to the version shown above.",
m_chk_update_btn:"Update Followed Skills to This Version",m_sync_ing:"Syncing snapshot…",m_sync_done:"✓ Update complete",
m_diff_ing:"Comparing…",m_diff_fail:"Read failed",m_diff_none:"(no differences)",
m_diff_title:"Diff: NAME",
m_ed_ref:"View NAME",m_ed_edit:"Edit NAME",
m_ed_hint_ref:"Follow-update skill is read-only. Convert to standalone copy to edit.",m_ed_hint_own:"Only SKILL.md is edited here. Use Open Dir for scripts and other files. Changes propagate everywhere.",
m_se_edit:"Edit Set",m_se_new:"New Set",m_se_empty:"No skills in the library yet",
m_se_name_err:"Set name must be lowercase, digits, hyphens",m_se_dup:"Set \"NAME\" already exists — use Edit to modify it",
m_no_desc_i:"No description yet",
m_row_conflict:"Name exists in library, skipped",m_row_invalid:"Name must be lowercase/digits/hyphens",
m_proj_label:"Project",m_empty_count:"places",
radio_claude:".claude (Claude Code)",radio_codex:".codex (Codex)",radio_agents:".agents (general)"
}};
function t(k){return (I18N[LANG]&&I18N[LANG][k])||I18N.zh[k]||k}
function tf(k,vars){let s=t(k);if(vars)for(const[k2,v]of Object.entries(vars))s=s.replace(k2,v);return s}
function applyI18n(){
  document.querySelectorAll('[data-i18n]').forEach(e=>e.textContent=t(e.dataset.i18n));
  document.querySelectorAll('[data-i18n-ph]').forEach(e=>e.placeholder=t(e.dataset.i18nPh));
  document.querySelectorAll('[data-i18n-title]').forEach(e=>e.title=t(e.dataset.i18nTitle));
}
function toggleLang(){LANG=LANG==="zh"?"en":"zh";localStorage.setItem("lang",LANG);render();applyI18n()}

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
function trunc(s,n){return s&&s.length>n?s.slice(0,n)+"…":s||""}
function sourceUrl(name){
  const src=S.sources.find(s=>s.name===name);
  if(!src||!src.url)return "";
  let u=src.url.replace(/\.git$/,"");
  // SSH 格式 git@github.com:owner/repo -> https://github.com/owner/repo
  const m=u.match(/^git@([^:]+):(.+)$/);
  return m?`https://${m[1]}/${m[2]}`:u;
}
function originTag(o){
  if(!o||o.type==="own")return `<span class="tag src-own">${t('chip_own')}</span>`;
  const url=o.source?sourceUrl(o.source):"";
  const name=esc(trunc(o.source||"",12));
  const link=url?`<a class="src-link" href="${esc(url)}" target="_blank" rel="noopener" title="${esc(o.source||"")}">${name}</a>`:`<span>${name}</span>`;
  if(o.type==="ref")return `<span class="tag src-ref" title="${t('tag_imported_ref')}">${'⇅'} ${link}</span>`;
  return `<span class="tag src-copy" title="${t('tag_imported_copy')}">${'⧉'} ${link}</span>`;
}
function pill(skill,target,label,state,title){
  const offLabel=LANG==='en'?'Click to disable':'点击关闭';
  const onLabel=LANG==='en'?'Click to enable':'点击开启';
  if(state==="hub-link"||state==="copy-synced")
    return `<span class="pill on" title="${offLabel} · ${esc(title)}" onclick="toggle('${esc(target)}','${skill}',false)">${esc(label)}</span>`;
  if(state==="absent")
    return `<span class="pill" title="${onLabel} · ${esc(title)}" onclick="toggle('${esc(target)}','${skill}',true)">${esc(label)}</span>`;
  const warnTitle=LANG==='en'?'Not managed by this library - see top notice':'这一处不归本库管,见顶部提示';
  return `<span class="pill warn" title="${state}: ${warnTitle}">${esc(label)} ⚠</span>`;
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
    ["skills",t('nav_skills'),`<span class="cnt">${S.skills.length}</span>`],
    ["sets",t('nav_sets'),`<span class="cnt">${Object.keys(S.sets).length||""}</span>`],
    ["usage",t('nav_usage'),""],
    ["sources",t('nav_sources'),""],
    ["settings",t('nav_settings'),""]];
  $("#nav").innerHTML=items.map(([id,label,extra])=>
    `<button class="${TAB===id?'active':''}" onclick="show('${id}')">${ICONS[id]}${label}${extra}</button>`).join("");
  let sf=S.platform==="darwin"
    ?`<div class="st ${S.autostart?'on':''}">${S.autostart?t('autostart_on'):t('autostart_off')}</div>`:"";
  $("#sidefoot").innerHTML=sf;
}

/* ---------- 技能页 ---------- */
function warningsHtml(){
  const plain = S.warnings.filter(w => !w.includes("独立副本内容已和库里不同") && !w.includes("standalone copies differ"));
  let html = plain.map(w=>`<div class="warnbox">⚠ ${esc(w)}</div>`).join("");
  if(S.divergences && S.divergences.length){
    const byLabel = {};
    for(const d of S.divergences){ (byLabel[d.label]=byLabel[d.label]||[]).push(d); }
    for(const [label, ds] of Object.entries(byLabel)){
      const items = ds.map(d=>
        `<span class="dv-item">${esc(d.name)}
           <a class="dv-link" onclick="showDiff('${esc(d.name)}','${esc(d.target)}')">${t('w_view_diff')}</a>
           <a class="dv-link" onclick="relink('${esc(d.name)}','${esc(d.target)}','${esc(label)}')">${t('w_relink')}</a>
         </span>`).join("");
      html += `<div class="warnbox">⚠ ${esc(label)} ${LANG==='en'?'has':'有'} ${ds.length} ${t('w_diverged')}${LANG==='en'?'':':'}${items}</div>`;
    }
  }
  return html;
}
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
          <button class="ghost" title="${isRef?t('t_view'):t('t_edit')}" onclick="editSkill('${k.name}')">✍</button>
          ${isRef?`<button class="ghost" title="${t('t_check_update')}" onclick="checkSource('${esc(k.origin.source)}')">↻</button>`:""}
          ${isRef?`<button class="ghost" title="${t('t_fork')}" onclick="post('/api/source/fork',{name:'${k.name}'})">⧉</button>`:""}
          <button class="ghost danger" title="${t('t_delete')}" onclick="delSkill('${k.name}')">✕</button></span></div>
      <div class="sk-desc" title="${esc(k.desc)}">${esc(k.desc)||`<i>${t('m_no_desc_i')}</i>`}</div>
      <div class="pills">
        ${pill(k.name,"claude",t('pill_claude'),k.places.claude,"~/.claude/skills")}
        ${pill(k.name,"codex",t('pill_codex'),k.places.codex,"~/.codex/skills")}
        ${S.agents_root||k.places.agents!=="absent"?pill(k.name,"agents",t('pill_agents'),k.places.agents,"~/.agents/skills"):""}
        ${projPills}
        <span class="pill add" title="${t('t_add_proj_title')}" onclick="addProject('${k.name}')">${t('pill_add_proj')}</span>
      </div></div>`}).join("")||`<div class="empty" style="grid-column:1/-1">${t('empty_skills')}</div>`;
}
function pageSkills(){
  const chips=[["all",t('chip_all')],["own",t('chip_own')],["ext",t('chip_ext')]];
  return `
  <div class="pagehead"><h1>${t('h_skills')}</h1>
    <span class="acts">
      <button class="ghost" title="${t('btn_open_lib')}" onclick="post('/api/open',{})">${t('btn_open_lib')}</button>
      <span class="splitbtn">
        <button title="${esc(scanRangeTitle())}" onclick="scanLocal('all')">${t('btn_scan')}</button>
        <details class="dropdown"><summary title="${t('dd_more')}">▾</summary>
          <div class="dropdown-menu">
            <button onclick="this.closest('details').open=false;scanLocal('global')">
              ${t('dd_scan_global')}<span class="dd-sub">${t('dd_scan_global_hint')}</span></button>
            <button onclick="this.closest('details').open=false;scanLocal('project')">
              ${t('dd_scan_project')}<span class="dd-sub">${esc(projRangeHint())}</span></button>
          </div>
        </details>
      </span>
      <button title="${esc(t('t_import_hint'))}" onclick="importDialog()">${t('btn_import')}</button>
      <button onclick="show('sources')">${t('btn_add_online')}</button>
      <button class="primary" onclick="newSkill()">${t('btn_new_skill')}</button></span>
    <span class="sub">${t('sub_skills')}</span>
  </div>
  ${onboard()}
  ${warningsHtml()}
  <div class="row" style="margin-top:14px">
    <input type="text" id="search" placeholder="${t('ph_search')}" style="width:260px"
      value="${esc(window._kw||"")}" oninput="window._kw=this.value;$('#sklist').innerHTML=skillCards()">
    <span class="chips" style="margin:0">${chips.map(([id,l])=>
      `<span class="chip ${FILTER===id?'active':''}" onclick="FILTER='${id}';render()">${l}</span>`).join("")}</span>
  </div>
  <div id="sklist" class="skgrid">${skillCards()}</div>`;
}

/* ---------- 组合页 ---------- */
function pageSets(){
  return `
  <div class="pagehead"><h1>${t('h_sets')}</h1>
    <span class="acts"><button class="primary" onclick="newSet()">${t('btn_new_set')}</button></span>
    <span class="sub">${t('sub_sets')}</span></div>
  ${Object.entries(S.sets).map(([n,list])=>`<div class="card">
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">${esc(n)} <span class="hint" style="font-weight:400">${list.length} ${t('n_skills')}</span></h2>
      <span class="row">
        <button onclick="applySet('${esc(n)}',true)">${t('btn_apply')}</button>
        <button class="ghost" onclick="applySet('${esc(n)}',false)">${t('btn_close')}</button>
        <button class="ghost" onclick="editSet('${esc(n)}')">${t('btn_edit')}</button>
        <button class="ghost danger" onclick="delSet('${esc(n)}')">${t('btn_delete')}</button></span></div>
    <div class="pills" style="margin-top:8px">${list.map(s=>{
      const k=S.skills.find(x=>x.name===s);
      return `<span class="tag ${k?"":"miss"}" title="${k?esc(k.desc):t('set_missing')}">${esc(s)}${k?"":" ?"}</span>`}).join("")
      ||`<span class="hint">${t('empty_set_hint')}</span>`}</div>
  </div>`).join("")||`<div class="empty">${t('empty_sets')}</div>`}`;
}
async function delSet(n){
  if(confirm(tf('m_del_set',{NAME:n})))
    await post("/api/set-delete",{name:n});
}
function onboard(){
  if(localStorage.getItem("onboarded4"))return "";
  return `<div class="banner"><b>${t('ob_title')}</b>
  ${t('ob_1')}<br>${t('ob_2')}<br>${t('ob_3')}
  <a href="#" onclick="localStorage.setItem('onboarded4','1');render();return false">${t('ob_gotit')}</a></div>`;
}

/* ---------- 使用情况页 ---------- */
function useCard(icon,label,path,target,get){
  const used=S.skills.filter(k=>{const st=get(k);return st&&st!=="absent"});
  return `<div class="usecard">
    <h3><span class="loc-ico">${icon}</span>${esc(label)}<span class="n">${used.length} ${t('use_n_skills')}</span></h3>
    <div class="path">${esc(path)}</div>
    <div class="pills">${used.map(k=>pill(k.name,target,k.name,get(k),t('use_close_title'))).join("")||`<span class="hint">${t('use_no_skill')}</span>`}
      <span class="pill add" onclick="pickSkill('${esc(target)}','${esc(label)}')">${t('use_pick')}</span></div></div>`;
}
function projCards(){
  const q=(window._pq||"").toLowerCase();
  const pts=S.proj_targets.filter(t=>t.path.toLowerCase().includes(q));
  return pts.map(t=>useCard("📁",projLabel(t.target),t.path+"/."+t.kind+"/skills",
    t.target,k=>k.places.projects[t.target])).join("")
    ||`<div class="empty" style="grid-column:1/-1">${q?t('empty_proj_match'):t('empty_proj_new')}</div>`;
}
function pageUsage(){
  return `
  <div class="pagehead"><h1>${t('h_usage')}</h1>
    <span class="acts"><button onclick="post('/api/targets/clean',{})">${t('btn_clean')}</button></span>
    <span class="sub">${t('sub_usage')}</span></div>
  <h2 style="font-size:13px;color:var(--muted);margin:18px 0 0">${t('h_global')}</h2>
  <div class="usegrid">
    ${useCard("C",t('cc_claude'),"~/.claude/skills","claude",k=>k.places.claude)}
    ${useCard("X",t('cc_codex'),"~/.codex/skills","codex",k=>k.places.codex)}
    ${S.agents_root?useCard("A",t('cc_agents'),"~/.agents/skills","agents",k=>k.places.agents):""}
  </div>
  <div class="row" style="margin:20px 0 0">
    <h2 style="font-size:13px;color:var(--muted);margin:0">${t('h_projects')} <span class="hint" style="font-weight:400">${S.proj_targets.length} ${t('n_places')}</span></h2>
    ${S.proj_targets.length>4?`<input type="text" id="projq" placeholder="${t('ph_search_proj')}" style="width:180px;margin-left:auto"
      value="${esc(window._pq||"")}" oninput="window._pq=this.value;$('#projgrid').innerHTML=projCards()">`:""}
  </div>
  <div class="usegrid" id="projgrid">${projCards()}</div>
  ${S.stale_targets.length?`<div class="warnbox">${t('stale_proj')}${S.stale_targets.map(esc).join("、")}</div>`:""}`;
}

/* ---------- 来源页 ---------- */
let SRC_COLLAPSED={};   // 来源名 -> 是否收起技能列表
function pageSources(){
  return `
  <div class="pagehead"><h1>${t('h_sources')}</h1>
    <span class="sub">${t('sub_sources')}<b>${t('sub_sources_b')}</b></span></div>
  <div class="card">
    <h2>${t('h_add_repo')}</h2>
    <div class="row" style="margin-top:8px">
      <input type="text" id="srcUrl" placeholder="https://github.com/xxx/skills.git" style="flex:1;min-width:260px">
      <button class="primary" id="srcAddBtn" onclick="addSource()">${t('btn_download')}</button></div></div>
  ${S.sources.map(s=>{
    const hasImp=s.skills.some(k=>k.imported_as);
    const collapsed=SRC_COLLAPSED[s.name]!==undefined?SRC_COLLAPSED[s.name]:hasImp;
    return `<div class="card">
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">${esc(s.name)} <span class="hint" style="font-weight:400">${s.skills.length} ${t('n_skills').replace('skills','skill(s)')}${hasImp?` · ${s.skills.filter(k=>k.imported_as).length} ${t('n_imported')}`:""}</span></h2>
      <span class="row">${s.is_git?`<button class="ghost" onclick="checkSource('${s.name}')">${t('btn_check_remote')}</button>`:""}
        <button class="ghost" onclick="toggleSrcList('${s.name}')" id="tog-${s.name}">${collapsed?t('btn_expand'):t('btn_collapse')}${t('skill_list_suffix')}</button>
        <button class="ghost danger" onclick="removeSource('${s.name}')">${t('btn_remove_src')}</button></span></div>
    <div class="hint mono">${esc(s.url||t('local_dir'))} · ${esc(s.head)}</div>
    <div id="chk-${s.name}"></div>
    <div id="list-${s.name}" style="margin-top:8px;${collapsed?"display:none":""}">${s.skills.map(k=>`<div class="srcskill">
      <b>${esc(k.name)}</b><span class="hint" style="flex:1">${esc(k.desc)}</span>
      ${k.imported_as?`<span class="tag done">${t('tag_imported_prefix')} ${k.imported_as}(${k.imported_type==="ref"?t('tag_imported_ref'):t('tag_imported_copy')})</span>`
        :`<button class="ghost" onclick="importSkill('${s.name}','${esc(k.subpath)}','ref')" title="${t('imp_ref_title')}">${t('imp_ref')}</button>
          <button class="ghost" onclick="importSkill('${s.name}','${esc(k.subpath)}','copy')" title="${t('imp_copy_title')}">${t('imp_copy')}</button>`}
    </div>`).join("")||`<span class="hint">${t('empty_src_skills')}</span>`}</div></div>`}).join("")
  ||`<div class="empty">${t('empty_src')}</div>`}`;
}
function toggleSrcList(name){
  const list=$("#list-"+name),tog=$("#tog-"+name);
  const now=list.style.display==="none";
  list.style.display=now?"":"none";
  tog.textContent=(now?t('btn_expand'):t('btn_collapse'))+t('skill_list_suffix');
  SRC_COLLAPSED[name]=!now;
}

/* ---------- 设置页 ---------- */
function pageSettings(){
  return `
  <div class="pagehead"><h1>${t('h_settings')}</h1></div>
  <div class="card">
    <h2>${t('h_behavior')}</h2>
    <label class="hint" style="display:block;margin-top:6px"><input type="checkbox" id="cleanEmpty" ${S.clean_empty_dirs?"checked":""}
      onchange="post('/api/settings',{clean_empty_dirs:this.checked})">
      ${t('clean_empty_label')}</label>
  </div>
  <div class="card">
    <h2>${t('h_boundary')}</h2>
    <div class="hint" style="line-height:2">
      ${t('bnd_1')}<br>
      ${t('bnd_2')}<br>
      ${t('bnd_3')}</div>
  </div>
  ${S.platform==="darwin"?`<div class="card">
    <h2>${t('h_service')}</h2>
    <div class="hint" style="line-height:2.2">
      ${S.autostart?t('svc_on'):t('svc_off')}
    </div>
  </div>`:""}`;
}

/* ---------- 渲染入口 ---------- */
function render(){
  if(!S)return;
  renderNav();
  $("#page").innerHTML={skills:pageSkills,sets:pageSets,usage:pageUsage,sources:pageSources,
                        settings:pageSettings}[TAB]();
  // 语言切换按钮:注入到每个页面顶部 pagehead 右侧
  let acts=document.querySelector(".pagehead .acts");
  if(!acts){const ph=document.querySelector(".pagehead");if(ph){acts=document.createElement("span");acts.className="acts";ph.appendChild(acts)}}
  if(acts&&!acts.querySelector(".lang-toggle")){
    const b=document.createElement("button");b.className="ghost lang-toggle";
    b.style.cssText="font-size:12px;padding:4px 12px;font-weight:600";
    b.textContent=t('lang_switch');b.onclick=toggleLang;
    acts.appendChild(b);
  }
  applyI18n();
}

/* ---------- 交互 ---------- */
async function toggle(target,skill,on){await post("/api/toggle",{target,skill,on})}
const ask=$("#ask");
function askDialog(title,hint,bodyHtml,onOk){
  $("#askTitle").textContent=title;$("#askHint").textContent=hint;
  $("#askBody").innerHTML=bodyHtml;$("#askOk").onclick=async()=>{await onOk();ask.close()};
  ask.showModal();
}
function newSkill(){askDialog(t('m_new_skill_t'),t('m_new_skill_h'),
  `<input type="text" id="askIn" style="width:100%" placeholder="my-skill">`,
  async()=>{const n=$("#askIn").value.trim();if(!n)return;
    const j=await post("/api/new",{name:n});if(j.ok)editSkill(n)})}
function checkRow(f,extra){
  const bad=f.conflict?t('m_row_conflict'):!f.valid?t('m_row_invalid'):"";
  return `<label class="srcskill" style="cursor:${bad?"default":"pointer"}">
    <input type="checkbox" data-p="${esc(f.path)}" ${bad?"disabled":"checked"}>
    <b>${esc(f.name)}</b><span class="hint" style="flex:1">${esc(extra||"")}</span>
    ${bad?`<span class="tag miss">${bad}</span>`:""}</label>`;
}
function checkedPaths(sel){return [...document.querySelectorAll(sel+' input:checked')].map(x=>x.dataset.p)}
const GLOBAL_SKILL_DIRS=["~/.claude/skills","~/.codex/skills","~/.agents/skills"];
function scanRangeTitle(){
  let lines=[t('t_scan_range'),...GLOBAL_SKILL_DIRS];
  if(S.projects&&S.projects.length){
    lines.push(t('t_scan_range_proj'));
    lines=lines.concat(S.projects.map(p=>`${p}/.{claude,codex,agents}/skills`));
  }
  return lines.join("\n");
}
function projRangeHint(){
  return S.projects&&S.projects.length?S.projects.join("、"):t('dd_scan_project_empty');
}
async function scanLocal(scope){
  if(scope==="project"&&(!S.projects||!S.projects.length)){toast(t('dd_scan_project_empty'));return}
  toast(t('m_scan_toast'));
  const r=await post("/api/scan",{scope});
  const list=r.found||[];
  if(!list.length){toast(t('m_scan_none'));return}
  askDialog(tf('m_scan_t',{COUNT:list.length}),t('m_scan_h'),
    list.map(f=>checkRow(f,f.place)).join(""),
    async()=>{const ps=checkedPaths("#askBody");if(ps.length)await post("/api/adopt-bulk",{paths:ps})});
}
function importDialog(){
  askDialog(t('m_import_t'),t('m_import_h'),
   `<div class="row"><input type="text" id="impPath" style="flex:1;min-width:0" placeholder="${t('m_imp_ph')}">
    <button onclick="pickImportDir()">${t('btn_browse')}</button>
    <button onclick="impProbe()">${t('m_import_find')}</button></div><div id="impList" style="margin-top:8px"></div>`,
   async()=>{const ps=checkedPaths("#impList");if(ps.length)await post("/api/import",{paths:ps})});
}
async function pickImportDir(){
  const r=await post("/api/pick-dir",{start:$("#impPath").value.trim()});
  if(!r.ok){if(r.out)toast(r.out);return}
  if(!r.path)return;  // 用户取消
  $("#impPath").value=r.path;
  await impProbe();
}
async function impProbe(){
  const p=$("#impPath").value.trim();if(!p)return;
  const r=await post("/api/import",{path:p,probe:true});
  if(!r.ok)return;
  $("#impList").innerHTML=(r.found||[]).map(f=>checkRow(f,f.desc)).join("")
    ||`<div class="empty">${t('m_import_empty')}</div>`;
}
function addProject(skill){
  const opts=S.projects.map(p=>`<option value="${esc(p)}">`).join("");
  askDialog(tf('m_addproj_t',{SKILL:skill}),t('m_addproj_h'),
  `<input type="text" id="askIn" list="projList" style="width:100%" placeholder="${t('m_addproj_ph')}">
   <datalist id="projList">${opts}</datalist>
   <div class="row" style="margin-top:10px">
     <label class="hint"><input type="radio" name="pkind" value="claude" checked> ${t('radio_claude')}</label>
     <label class="hint"><input type="radio" name="pkind" value="codex"> ${t('radio_codex')}</label>
     <label class="hint"><input type="radio" name="pkind" value="agents"> ${t('radio_agents')}</label>
   </div>`,
  async()=>{const p=$("#askIn").value.trim();if(!p)return;
    const kind=document.querySelector('input[name=pkind]:checked').value;
    await toggle(p+"::"+kind,skill,true)})}
function pickSkill(target,label){
  const here=S.skills.filter(k=>{
    const st=["claude","codex","agents"].includes(target)?k.places[target]:(k.places.projects[target]||"absent");
    return st==="absent"});
  askDialog(tf('m_pick_t',{LABEL:label}),t('m_pick_h'),
    here.map(k=>`<div class="srcskill"><b>${k.name}</b><span class="hint" style="flex:1">${esc(k.desc).slice(0,60)}</span>
      <button class="ghost" onclick="toggle('${esc(target)}','${k.name}',true).then(()=>ask.close())">${t('m_pick_open')}</button></div>`).join("")
    ||`<div class="empty">${t('m_pick_empty')}</div>`,
    async()=>{});
  $("#askOk").style.display="none";
  ask.addEventListener("close",()=>{$("#askOk").style.display=""},{once:true});
}
function applySet(name,on){
  const opts=S.projects.map(p=>`<option value="${esc(p)}">`).join("");
  askDialog(tf('m_apply_t',{ACT:on?t('btn_apply'):t('btn_close'),NAME:name}),t('m_apply_h'),
  `<div class="hint" style="margin-bottom:4px">${t('m_apply_sel')}</div>
   <select id="askSel" style="width:100%">
    <option value="claude">${t('pill_claude')}</option><option value="codex">${t('pill_codex')}</option>
    ${S.agents_root?`<option value="agents">${t('pill_agents')}</option>`:""}
    ${S.proj_targets.map(t2=>`<option value="${esc(t2.target)}">${t('m_proj_label')}:${esc(t2.path)}(.${t2.kind})</option>`).join("")}</select>
   <div class="hint" style="margin:12px 0 4px">${t('m_apply_custom')}</div>
   <input type="text" id="askProj" list="projList2" style="width:100%" placeholder="${t('m_addproj_ph')}">
   <datalist id="projList2">${opts}</datalist>
   <div class="row" style="margin-top:10px">
     <label class="hint"><input type="radio" name="pkind" value="claude" checked> ${t('radio_claude')}</label>
     <label class="hint"><input type="radio" name="pkind" value="codex"> ${t('radio_codex')}</label>
     <label class="hint"><input type="radio" name="pkind" value="agents"> ${t('radio_agents')}</label>
   </div>`,
  async()=>{
    const p=$("#askProj").value.trim();
    let target;
    if(p){
      const kind=document.querySelector('input[name=pkind]:checked').value;
      target=p+"::"+kind;
    }else{
      target=$("#askSel").value;
    }
    await post("/api/set-apply",{set:name,target,on})})}
async function delSkill(n){
  if(confirm(tf('m_del_skill',{NAME:n})))
    await post("/api/delete",{name:n})}
async function removeSource(n){
  const src=S.sources.find(s=>s.name===n);
  const refs=(src&&src.skills||[]).filter(k=>k.imported_type==="ref");
  if(refs.length){
    const el=$("#chk-"+n);
    if(el) el.innerHTML=`<div class="warnbox" style="margin-top:8px">
      ${tf('m_remove_blocked',{COUNT:refs.length})}
      ${refs.map(k=>`<div class="row" style="margin:4px 0"><b>${esc(k.imported_as)}</b>
        <button class="ghost" style="font-size:11px;padding:1px 8px" onclick="post('/api/source/fork',{name:'${esc(k.imported_as)}'}).then(()=>removeSource('${esc(n)}'))">${t('m_remove_fork')}</button>
        <span class="hint" style="font-size:11px">${t('m_remove_fork_hint')}</span></div>`).join("")}
      <div class="hint" style="margin-top:4px">${t('m_remove_hint')}</div></div>`;
    toast(t('m_remove_toast'));
    return;
  }
  if(confirm(tf('m_remove_src_c',{NAME:n})))
    await post("/api/source/remove",{source:n})}
async function importSkill(source,subpath,mode){await post("/api/source/import",{source,subpath,mode})}
async function addSource(){const u=$("#srcUrl").value.trim();if(!u)return;
  const b=$("#srcAddBtn");b.disabled=true;b.textContent=t('m_dl_ing');
  try{await post("/api/source/add",{url:u})}finally{b.disabled=false;b.textContent=t('btn_download')}}
async function checkSource(name){
  const body=$("#srcChkBody");
  $("#srcChkTitle").textContent=tf('m_chk_remote_t',{NAME:name});
  body.innerHTML=`<div class="hint"><span class="spin"></span> ${t('m_chk_ing')}</div>`;
  srcCheck.showModal();
  const r=await api("/api/source/check",{source:name});
  if(!r.ok){body.innerHTML='<div class="warnbox">'+esc(r.out||t('m_chk_fail'))+'</div>';return}
  if(r.note){body.innerHTML='<div class="hint">'+esc(r.note)+'</div>';return}
  if(!r.behind){body.innerHTML=`<div class="hint">${t('m_chk_latest')}</div>`;return}
  body.innerHTML=`<div class="pendbox"><b>${tf('m_chk_behind',{COUNT:r.behind,VER:esc(r.target)})}</b>
    ${tf('m_chk_affect',{COUNT:r.affected.length,NAMES:r.affected.map(a=>a.skill).join("、")||"—"})}
    ${r.affected.length?`<pre>${esc(r.affected.map(a=>a.skill+":\n  "+a.files.join("\n  ")).join("\n"))}</pre>`:""}
    <pre>${esc(r.commits)}</pre>
    <div class="row" style="margin-top:8px">
      <button class="primary" onclick="updateSource('${name}','${esc(r.token)}')">${t('m_chk_update_btn')}</button>
    </div></div>`;
}
async function updateSource(name,token){
  const body=$("#srcChkBody");
  body.innerHTML=`<div class="hint"><span class="spin"></span> ${t('m_sync_ing')}</div>`;
  await post("/api/source/update",{source:name,token});
  body.innerHTML=`<div class="hint">${t('m_sync_done')}</div>`;
}
const editor=$("#editor");
async function showDiff(name,target){
  const d=$("#diffBody");
  $("#diffTitle").textContent=tf('m_diff_title',{NAME:name});
  d.innerHTML=`<span class="spin"></span> ${t('m_diff_ing')}`;
  $("#diff").showModal();
  const j=await api("/api/diff",{name,target});
  if(!j.ok){d.innerHTML='<div class="warnbox">'+esc(j.out||t('m_diff_fail'))+'</div>';return}
  const colored=esc(j.diff).split("\n").map(l=>{
    if(l.startsWith("---")||l.startsWith("+++")||l.startsWith("@@"))return '<span class="df-h">'+l+'</span>';
    if(l.startsWith("-"))return '<span class="df-del">'+l+'</span>';
    if(l.startsWith("+"))return '<span class="df-add">'+l+'</span>';
    return l;
  }).join("\n");
  d.innerHTML=colored||t('m_diff_none');
  $("#diffRelink").onclick=()=>{diff.close();relink(name,target,"")};
}
async function relink(name,target,label){
  const where=label||target;
  if(!confirm(tf('m_relink_c',{NAME:name,WHERE:where})))
    return;
  await post("/api/relink",{name,target});
}
async function editSkill(name){
  const j=await (await fetch("/api/skill?name="+encodeURIComponent(name))).json();
  if(!j.ok){toast(j.out);return}
  ED={type:"skill",name};
  $("#edTitle").textContent=j.readonly?tf('m_ed_ref',{NAME:name}):tf('m_ed_edit',{NAME:name});
  $("#edHint").textContent=j.readonly?t('m_ed_hint_ref'):t('m_ed_hint_own');
  $("#edSave").style.display=j.readonly?"none":"";
  $("#edOpen").style.display="";
  $("#edBody").value=j.content;editor.showModal();
}
function editSet(name){openSetEditor(name,S.sets[name]||[])}
function newSet(){openSetEditor(null,[])}
function openSetEditor(name,preset){
  SE_ORIG=name;
  $("#seTitle").textContent=name?t('m_se_edit'):t('m_se_new');
  $("#setName").value=name||"";
  $("#seCards").innerHTML=setEditorCards(preset);
  setEditor.showModal();
}
function setEditorCards(preset){
  const pre=new Set(preset);
  if(!S.skills.length)return `<div class="empty" style="grid-column:1/-1">${t('m_se_empty')}</div>`;
  return S.skills.map(k=>{
    const on=pre.has(k.name);
    return `<label class="skcard sel ${on?"on":""}" data-n="${esc(k.name)}">
      <div class="se-head">
        <input type="checkbox" ${on?"checked":""}>
        <span class="sk-name" title="${esc(k.name)}">${esc(k.name)}</span>
      </div>
      <div class="sk-desc" title="${esc(k.desc||"")}">${esc(k.desc)||`<i>${t('m_no_desc_i')}</i>`}</div>
    </label>`}).join("");
}
async function saveSetEditor(){
  const name=$("#setName").value.trim();
  if(!/^[a-z0-9][a-z0-9._-]*$/.test(name)){toast(t('m_se_name_err'));return}
  if(name!==SE_ORIG && S.sets[name]){toast(tf('m_se_dup',{NAME:name}));return}
  const picked=[...document.querySelectorAll("#seCards .skcard.on")].map(e=>e.dataset.n);
  await post("/api/set",{name,content:picked.join("\n")+(picked.length?"\n":"")});
  setEditor.close();
}
// 卡片整行点击切换勾选(复选框本身的事件会冒泡,这里防双触发)
document.addEventListener("change",e=>{
  const card=e.target.closest("#seCards .skcard.sel");
  if(!card)return;
  card.classList.toggle("on",e.target.checked);
});
document.addEventListener("click",e=>{
  const card=e.target.closest("#seCards .skcard.sel");
  if(!card||e.target.tagName==="INPUT")return;   // 点复选框交给 change 处理
  const cb=card.querySelector('input[type=checkbox]');
  cb.checked=!cb.checked;
  card.classList.toggle("on",cb.checked);
});
async function saveEditor(){await post("/api/skill",{name:ED.name,content:$("#edBody").value});editor.close()}
// 点下拉外面收起(<details> 默认不会自己收)
document.addEventListener("click",e=>{
  document.querySelectorAll("details.dropdown[open]").forEach(d=>{
    if(!d.contains(e.target))d.open=false;
  });
});

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


# ---------- Git 缺失时的引导页(自包含,不依赖正常启动流程) ----------

def git_available():
    """检查系统是否安装了 git。"""
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, OSError):
        return False


GIT_MISSING_PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Skills Hub - 需要 Git</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='24' fill='%23c22f2f'/%3E%3Ctext x='50' y='72' font-size='60' text-anchor='middle' fill='%23fff'%3E✦%3C/text%3E%3C/svg%3E">
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#181f2a;--muted:#68717f;--line:#e4e7ec;
--accent:#4655d4;--accent-soft:#eef0fd;--bad:#c22f2f;--badbg:#fde7e7;--ok:#188945;--okbg:#e3f5ea;--r:14px;--rs:9px}
@media(prefers-color-scheme:dark){:root{--bg:#101318;--card:#1b202a;--ink:#e8ebf1;--muted:#9aa4b2;--line:#2a303c;--accent:#7d8cf8;--accent-soft:#232a4d;--bad:#f27b7b;--badbg:#3d1a1a;--ok:#5fd08c;--okbg:#15301f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.box{max-width:620px;width:100%}
.logo{display:flex;align-items:center;gap:12px;margin-bottom:20px}
.logo .ic{width:40px;height:40px;border-radius:11px;background:var(--bad);color:#fff;display:flex;align-items:center;justify-content:center;font-size:20px;flex:none}
.logo b{font-size:19px}.logo .sub{font-size:12px;color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:24px 26px;margin-bottom:16px}
.alert{background:var(--badbg);color:var(--bad);border-radius:var(--rs);padding:10px 15px;font-size:14px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 8px}
.hint{color:var(--muted);font-size:13px;line-height:1.8}
code{background:rgba(0,0,0,.08);border-radius:4px;padding:1px 6px;font:13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media(prefers-color-scheme:dark){code{background:rgba(255,255,255,.1)}}
.prompt-box{background:var(--bg);border:1px solid var(--line);border-radius:var(--rs);padding:14px 16px;margin-top:10px;position:relative}
.prompt-box pre{margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.btn-row{display:flex;gap:10px;margin-top:18px}
button{font:inherit;font-size:14px;padding:9px 18px;border-radius:var(--rs);border:1px solid var(--line);
background:var(--card);color:var(--ink);cursor:pointer;transition:border-color .12s}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.ghost{background:none}
.ok-msg{background:var(--okbg);color:var(--ok);border-radius:var(--rs);padding:12px 16px;margin-top:12px;display:none}
.ok-msg.show{display:block}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);
padding:10px 22px;border-radius:12px;font-size:14px;opacity:0;transition:.25s;pointer-events:none}
.toast.show{opacity:.95}
.tabs{display:flex;gap:0;margin-top:12px;border-bottom:1px solid var(--line)}
.tab{padding:8px 16px;cursor:pointer;border-bottom:2px solid transparent;color:var(--muted);font-size:13px}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.tab-content{display:none;padding-top:12px}.tab-content.active{display:block}
</style></head><body>
<div class="box">
  <div class="logo"><div class="ic">✦</div><div><b>Skills Hub</b><div class="sub">一处管理 · 处处可用</div></div></div>
  <div class="card">
    <div class="alert">⚠ Skills Hub 需要 Git 才能运行。检测到系统未安装 Git。</div>
    <p class="hint">Skills Hub 用 Git 记录技能的每次变更(新增、编辑、删除),这是你的回滚路径。没有 Git,管理台无法启动。</p>

    <h2 style="margin-top:18px">安装 Git</h2>
    <div class="tabs">
      <div class="tab active" onclick="switchTab('mac')">macOS</div>
      <div class="tab" onclick="switchTab('win')">Windows</div>
    </div>
    <div id="tab-mac" class="tab-content active">
      <div class="hint">任选一种方式:</div>
      <div class="prompt-box"><pre>brew install git</pre></div>
      <div class="hint" style="margin:8px 0">或者(安装 Xcode Command Line Tools,含 Git):</div>
      <div class="prompt-box"><pre>xcode-select --install</pre></div>
    </div>
    <div id="tab-win" class="tab-content">
      <div class="hint">任选一种方式:</div>
      <div class="prompt-box"><pre>winget install Git.Git</pre></div>
      <div class="hint" style="margin:8px 0">或者从官网下载安装包:</div>
      <div class="prompt-box"><pre>https://git-scm.com/download/win</pre></div>
    </div>

    <h2 style="margin-top:20px">让 AI 帮你安装</h2>
    <div class="hint">把下面的 prompt 复制给你的 AI agent,它会检测平台并自动安装:</div>
    <div class="prompt-box">
      <pre>帮我安装 Git。先检测操作系统(macOS 或 Windows),然后用合适的方式安装(brew install git / xcode-select --install / winget install Git.Git)。安装完成后运行 git --version 确认安装成功。</pre>
      <button class="ghost" style="position:absolute;top:8px;right:8px;font-size:12px;padding:4px 10px" onclick="copyPrompt()">复制</button>
    </div>

    <div class="ok-msg" id="okMsg">✓ Git 已安装!请点击下方「重启管理台」,或重新双击启动脚本。</div>
    <div class="btn-row">
      <button class="primary" onclick="recheck()">已安装,重新检查</button>
      <button class="ghost" onclick="recheck()">重启管理台</button>
      <button class="ghost" onclick="exitApp()" style="margin-left:auto">退出</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
function switchTab(t){document.querySelectorAll('.tab').forEach((e,i)=>e.classList.toggle('active',i===(t==='mac'?0:1)));
  document.getElementById('tab-mac').classList.toggle('active',t==='mac');
  document.getElementById('tab-win').classList.toggle('active',t==='win')}
function copyPrompt(){const t=document.querySelector('.prompt-box pre').textContent;
  navigator.clipboard.writeText(t).then(()=>showToast('已复制到剪贴板')).catch(()=>showToast('复制失败,请手动选中复制'))}
function showToast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2500)}
async function recheck(){
  const r=await fetch('/api/recheck').then(r=>r.json()).catch(()=>({git:false}));
  if(r.git){document.getElementById('okMsg').classList.add('show');
    showToast('Git 已检测到!正在重启…');setTimeout(()=>location.reload(),2000)}
  else{showToast('仍未检测到 Git,请先安装')}}
async function exitApp(){await fetch('/api/exit').catch(()=>{});showToast('正在退出…');setTimeout(()=>window.close(),1000)}
</script>
</body></html>
"""


class GitMissingHandler(BaseHTTPRequestHandler):
    """Git 缺失时的最小 Handler:只服务引导页和 recheck/exit 两个只读 API。"""
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "":
            body = GIT_MISSING_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/recheck":
            self._json({"git": git_available()})
        elif self.path == "/api/exit":
            self._json({"ok": True})
            threading.Timer(0.3, os._exit, args=[0]).start()
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_git_missing(port=PORT, open_browser=True):
    """Git 未安装时启动的最小服务:只展示安装引导页,不调 ensure_hub、不需要 Git。"""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), GitMissingHandler)
    except OSError:
        print(f"端口 {port} 已被占用,管理台可能已在运行(无 Git 模式)。")
        return None
    print(f"⚠ Git 未安装,展示安装引导页: http://127.0.0.1:{port}")
    print("安装 Git 后重启管理台。")
    if open_browser:
        threading.Timer(0.6, webbrowser.open, args=[f"http://127.0.0.1:{port}"]).start()
    return srv


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
    # Git 是硬依赖:没有 Git 时不进 ensure_hub,启动最小引导页服务
    if not git_available():
        srv = serve_git_missing(port, open_browser="--no-open" not in sys.argv)
        if srv is None:
            return
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    srv = serve(port, open_browser="--no-open" not in sys.argv)
    if srv is None:
        return
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
