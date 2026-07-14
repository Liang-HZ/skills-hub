#!/usr/bin/env python3
"""把 webui.py 里的真实界面编译成 skills.liangai.org 上的纯前端模拟板。

思路:管理台的界面对后端的访问**全部**经过 fetch(),所以只要在 app 脚本跑起来之前
覆写 window.fetch,用一份烤进页面的快照数据应答,真实 UI 就能一行不改地跑在静态托管上。
好处是 demo 永远和产品长一个样 —— UI 改了,重跑一次这个脚本即可,不存在"两套界面各自漂移"。

用法:
    python3 tools/build_demo.py          # 生成 site/index.html
数据来源:
    tools/demo-data.json —— 从一个填了假数据的沙箱实例抓的 state/usage/技能正文快照,
    已洗掉所有本机路径。UI 的数据结构变了才需要重新生成。
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 让 import webui 别把当前目录当技能库(它只是取个 PAGE 常量,不该碰任何真实目录)
os.environ.setdefault("SKILLS_HUB_ROOT", str(ROOT / ".state" / "build-demo-noop"))

import webui  # noqa: E402

DATA = json.loads((ROOT / "tools" / "demo-data.json").read_text(encoding="utf-8"))
OUT = ROOT / "site" / "index.html"
REPO = "https://github.com/Liang-HZ/skills-hub"

# 统计脚本只出现在这里,不能进 webui.py:webui.py 是跑在使用者自己机器上的管理器,
# 给它加统计等于让一个本地工具往外回传。模拟板是 skills.liangai.org 上的公开页面,
# 统计的是访客,不是任何人的技能库。website id 非密钥,可公开提交。
HEAD = """<title>Skills Hub — interactive demo · 技能库在线模拟</title>
<meta name="description" content="Try Skills Hub in your browser: one local library for every AI agent skill (Claude Code / Codex / OpenCode), with real per-skill trigger counts. This is a simulation — nothing touches your machine.">
<meta property="og:title" content="Skills Hub — interactive demo">
<meta property="og:description" content="You've installed a pile of agent skills. Which ones actually fire? Click around a live simulation of Skills Hub.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://skills.liangai.org">
<meta property="og:image" content="https://skills.liangai.org/screenshot.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://skills.liangai.org/screenshot.png">
<script defer src="https://analytics.liangai.org/script.js" data-website-id="3c57e0ff-fa6c-4da7-85f9-a310036c21fa"></script>"""

# ---- 假后端:在 app 脚本之前覆写 fetch ----
SHIM = r"""
<script id="demo-backend">
/* 模拟板的后端。真实管理台里这些请求打到本机 127.0.0.1:7799 的 Python 服务;
   这里全部在浏览器内存里应答,页面一刷新就复原。 */
(function(){
  const DATA = __DEMO_DATA__;
  const state = DATA.state, usage = DATA.usage, bodies = DATA.bodies;

  const J = o => new Response(JSON.stringify(o),
    {status:200, headers:{"Content-Type":"application/json; charset=utf-8"}});
  // 和真实后端一样,按请求头里的语言给回话
  const L = (o, zh, en) => (o === "en" ? en : zh);

  function findSkill(n){ return state.skills.find(s => s.name === n); }

  function setPlace(name, target, on){
    const s = findSkill(name);
    if(!s) return false;
    const v = on ? "hub-link" : "absent";
    if(Object.prototype.hasOwnProperty.call(s.places, target)) s.places[target] = v;
    else s.places.projects[target] = v;
    return true;
  }

  function descOf(md){
    const m = /^description:\s*(.+)$/m.exec(md || "");
    return m ? m[1].trim().replace(/^["']|["']$/g, "") : "";
  }

  // 只在浏览器里改得动的操作 = 真模拟;需要真实文件系统/网络的 = 提示去装本地版
  const OPS = {
    "/api/toggle": (b, lang) => {
      if(!setPlace(b.skill, b.target, b.on)) return {ok:false, out:"?"};
      return {ok:true, out: L(lang,
        (b.on ? "已开启 " : "已关闭 ") + b.skill + "(模拟)",
        (b.on ? "Enabled " : "Disabled ") + b.skill + " (simulated)")};
    },
    "/api/set-apply": (b, lang) => {
      (state.sets[b.set] || []).forEach(n => setPlace(n, b.target, b.on));
      const n = (state.sets[b.set] || []).length;
      return {ok:true, out: L(lang,
        `组合「${b.set}」的 ${n} 个技能已${b.on ? "开启" : "关闭"}(模拟)`,
        `${n} skills in "${b.set}" ${b.on ? "enabled" : "disabled"} (simulated)`)};
    },
    "/api/skill": (b, lang) => {
      bodies[b.name] = b.content;
      const s = findSkill(b.name);
      if(s){ s.desc = descOf(b.content); s.updated = Date.now()/1000; }
      return {ok:true, out: L(lang, "已保存(模拟,刷新页面即复原)",
                                    "Saved (simulated — reload to reset)")};
    },
    "/api/new": (b, lang) => {
      if(findSkill(b.name)) return {ok:false, out: L(lang, `库里已有「${b.name}」`,
                                                           `"${b.name}" already exists in the library`)};
      const md = `---\nname: ${b.name}\ndescription: <one line: what it does + when to use it>\n---\n\n# ${b.name}\n\n<body>\n`;
      bodies[b.name] = md;
      const places = {claude:"absent", codex:"absent", agents:"absent", projects:{}};
      Object.keys(state.skills[0].places.projects).forEach(k => places.projects[k] = "absent");
      state.skills.push({name:b.name, desc:descOf(md), origin:null, places,
                         created:Date.now()/1000, updated:Date.now()/1000});
      return {ok:true, out: L(lang, `已新建「${b.name}」(模拟)`, `Created "${b.name}" (simulated)`)};
    },
    "/api/delete": (b, lang) => {
      const i = state.skills.findIndex(s => s.name === b.name);
      if(i >= 0) state.skills.splice(i, 1);
      delete bodies[b.name];
      return {ok:true, out: L(lang, `已删除「${b.name}」,真实版里会进回收站(模拟)`,
                                    `Deleted "${b.name}" — the real version moves it to trash (simulated)`)};
    },
    "/api/set": (b, lang) => {
      state.sets[b.name] = b.skills || [];
      state.sets_raw[b.name] = b.skills || [];
      return {ok:true, out: L(lang, "组合已保存(模拟)", "Set saved (simulated)")};
    },
    "/api/set-delete": (b, lang) => {
      delete state.sets[b.name]; delete state.sets_raw[b.name];
      return {ok:true, out: L(lang, "组合已删除(模拟)", "Set deleted (simulated)")};
    },
    "/api/settings": (b, lang) => {
      Object.assign(state, b);
      return {ok:true, out: L(lang, "设置已保存(模拟)", "Settings saved (simulated)")};
    },
  };

  // 这些真的需要你机器上的文件系统或联网,模拟板做不到,也不该假装做得到
  const NEEDS_LOCAL = ["/api/open", "/api/pick-dir", "/api/scan", "/api/import", "/api/adopt",
                       "/api/adopt-bulk", "/api/relink", "/api/diff", "/api/targets/clean",
                       "/api/source/add", "/api/source/import", "/api/source/fork",
                       "/api/source/remove", "/api/source/check", "/api/source/update"];

  const realFetch = window.fetch.bind(window);
  window.fetch = function(input, opts){
    const url = String(typeof input === "string" ? input : (input && input.url) || "");
    if(!url.startsWith("/api/")) return realFetch(input, opts);

    const [path, qs] = url.split("?");
    const q = new URLSearchParams(qs || "");
    const method = ((opts && opts.method) || "GET").toUpperCase();
    const lang = (opts && opts.headers && opts.headers["X-Hub-Lang"]) === "en" ? "en" : "zh";

    if(method === "GET"){
      if(path === "/api/state") return Promise.resolve(J(state));
      if(path === "/api/usage") return Promise.resolve(J({ok:true, skills:usage}));
      if(path === "/api/skill"){
        const n = q.get("name");
        return Promise.resolve(J(n in bodies
          ? {ok:true, content:bodies[n], readonly:false}
          : {ok:false, out:"Skill not found"}));
      }
      return Promise.resolve(J({ok:false, out:"not found"}));
    }

    let body = {};
    try { body = JSON.parse((opts && opts.body) || "{}"); } catch(e){}

    if(OPS[path]) return Promise.resolve(J(OPS[path](body, lang)));
    if(NEEDS_LOCAL.includes(path)){
      if(window.demoNudge) window.demoNudge();
      return Promise.resolve(J({ok:false, out: L(lang,
        "这一步要读写你自己的电脑或者联网,模拟板做不到 —— 装个本地版就能用了。",
        "This step needs your own filesystem or the network — the demo can't do it. Install locally and it works.")}));
    }
    return Promise.resolve(J({ok:false, out:"not found"}));
  };
})();
</script>
"""

# ---- 顶部引导条 + 两个引导弹窗:跟着 app 自己的 i18n 走 ----
BAR_CSS = r"""
<style id="demo-style">
:root{--dbh:50px}
body{padding-top:var(--dbh)}
/* 侧栏只在宽屏是 sticky 满高的一列;窄屏下它变成横向导航条(webui.py 的 860px 断点),
   那时不能再压 height,否则会把导航撑成整屏高、把内容挤出视口 */
@media(min-width:861px){
  .side{top:var(--dbh) !important;height:calc(100vh - var(--dbh)) !important}
}
.demobar{position:fixed;top:0;left:0;right:0;height:var(--dbh);z-index:300;
  display:flex;align-items:center;gap:14px;padding:0 16px;
  background:var(--card);border-bottom:1px solid var(--line);
  box-shadow:0 1px 3px rgba(16,24,40,.06)}
.demobar .db-brand{display:flex;align-items:center;gap:8px;flex:none}
.demobar .db-logo{width:24px;height:24px;border-radius:7px;background:var(--accent);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:13px}
.demobar .db-brand b{font-size:14px}
.demobar .db-tag{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;
  background:var(--accent-soft);color:var(--accent-ink);white-space:nowrap}
.demobar .db-note{font-size:12.5px;color:var(--muted);flex:1;min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.demobar .db-act{display:flex;align-items:center;gap:8px;flex:none}
.demobar .db-act button,.demobar .db-act a{font:inherit;font-size:12.5px;font-weight:600;
  padding:6px 13px;border-radius:9px;border:1px solid var(--line);background:var(--card);
  color:var(--ink);cursor:pointer;text-decoration:none;white-space:nowrap}
.demobar .db-act button:hover,.demobar .db-act a:hover{border-color:var(--accent)}
.demobar .db-act .primary{background:var(--accent);border-color:var(--accent);color:#fff}
.demobar .db-act .primary:hover{opacity:.92}
@media(max-width:900px){.demobar .db-note{display:none}}
@media(max-width:620px){   /* 窄屏放不下全部,留住最关键的两个入口:说明 + 安装 */
  .demobar{gap:8px;padding:0 10px}
  .demobar .db-tag,.demobar .db-brand b,.demobar .db-act a{display:none}
  .demobar .db-act button{padding:6px 10px}
}
#demoIntro,#demoInstall{max-width:min(560px, calc(100vw - 28px))}

#demoIntro::backdrop,#demoInstall::backdrop{background:rgba(16,20,32,.45)}
#demoIntro,#demoInstall{max-width:560px}
#demoIntro h2,#demoInstall h2{margin:0 0 4px}
.demo-lede{color:var(--muted);font-size:13.5px;margin:0 0 16px}
.demo-try{list-style:none;padding:0;margin:0 0 18px}
.demo-try li{display:flex;gap:11px;align-items:flex-start;padding:9px 0;
  border-top:1px solid var(--line);font-size:13.5px}
.demo-try li:first-child{border-top:none}
.demo-try .n{width:20px;height:20px;border-radius:50%;background:var(--accent-soft);
  color:var(--accent-ink);font-size:11px;font-weight:700;flex:none;
  display:flex;align-items:center;justify-content:center;margin-top:2px}
.demo-try b{display:block}
.demo-try span.d{color:var(--muted);font-size:12.5px}
.demo-note{background:var(--infobg);color:var(--info);border-radius:var(--rs);
  padding:10px 13px;font-size:12.5px;margin:0 0 16px}
pre.demo-cmd{background:var(--panel);border:1px solid var(--line);border-radius:var(--rs);
  padding:13px 15px;font:12.5px/1.85 ui-monospace,Menlo,Consolas,monospace;
  overflow-x:auto;margin:0 0 14px;color:var(--ink)}
pre.demo-cmd .c{color:var(--faint)}
</style>
"""

BAR_HTML = r"""
<div class="demobar">
  <div class="db-brand"><span class="db-logo">✦</span><b>Skills Hub</b>
    <span class="db-tag" data-i18n="demo_tag">在线模拟</span></div>
  <div class="db-note" data-i18n="demo_note">这是模拟板:数据是编的,一切都跑在你的浏览器里,碰不到你的电脑。</div>
  <div class="db-act">
    <button data-i18n="demo_what" onclick="demoIntro.showModal()">这是什么?</button>
    <button class="primary" data-i18n="demo_install" onclick="demoInstall.showModal()">装到本地</button>
    <a href="__REPO__" target="_blank" rel="noopener">GitHub</a>
  </div>
</div>

<dialog id="demoIntro">
  <h2 data-i18n="demo_i_title">你在看一个模拟板</h2>
  <p class="demo-lede" data-i18n="demo_i_lede">这是 Skills Hub 管理台的真实界面,只不过后端换成了浏览器内存里的假数据。随便点,坏不了。</p>
  <ul class="demo-try">
    <li><span class="n">1</span><div><b data-i18n="demo_i_1h">拨一个开关</b>
      <span class="d" data-i18n="demo_i_1d">技能卡片上那排按钮。绿的 = 那个位置能用这个技能。真实版里,这一下就是建/删一条软链接。</span></div></li>
    <li><span class="n">2</span><div><b data-i18n="demo_i_2h">打开「用量分析」</b>
      <span class="d" data-i18n="demo_i_2d">这是本工具真正独有的地方:每个技能被实际触发了多少次。注意「从未触发」那一格——装了但从没用过的技能。</span></div></li>
    <li><span class="n">3</span><div><b data-i18n="demo_i_3h">试试「组合」</b>
      <span class="d" data-i18n="demo_i_3d">把常一起用的技能存成一组,一键开关整组。</span></div></li>
  </ul>
  <p class="demo-note" data-i18n="demo_i_note">数字是编出来的样例数据。真实版里,这些触发次数是从 Claude Code / Codex / OpenCode 各自的本地会话记录里数出来的——只读、不上传、不出你的机器。</p>
  <div class="row" style="justify-content:flex-end;gap:8px">
    <button data-i18n="demo_i_close" onclick="demoIntro.close()">开始点</button>
    <button class="primary" data-i18n="demo_install" onclick="demoIntro.close();demoInstall.showModal()">装到本地</button>
  </div>
</dialog>

<dialog id="demoInstall">
  <h2 data-i18n="demo_n_title">装到本地(真正能用的版本)</h2>
  <p class="demo-lede" data-i18n="demo_n_lede">模拟板只能看。要真的管理你机器上的技能,把它跑起来——只要 Python 3.9+ 和 Git,不装任何依赖。</p>
  <pre class="demo-cmd"><span class="c"># macOS / Linux / Windows 都一样</span>
git clone __REPO__.git
cd skills-hub
python3 webui.py   <span class="c"># 打开 http://127.0.0.1:7799</span></pre>
  <p class="demo-lede" data-i18n="demo_n_win">Windows 双击 start-windows.bat 也行。想要个原生窗口:pip install pywebview && python3 desktop.py。</p>
  <div class="row" style="justify-content:flex-end;gap:8px">
    <button data-i18n="demo_n_close" onclick="demoInstall.close()">关闭</button>
    <a class="primary" href="__REPO__" target="_blank" rel="noopener"
       style="font:inherit;font-size:13px;font-weight:600;padding:8px 15px;border-radius:9px;
              background:var(--accent);color:#fff;text-decoration:none">GitHub →</a>
  </div>
</dialog>

<script id="demo-ui">
(function(){
  const Z = {
    demo_tag:"在线模拟", demo_what:"这是什么?", demo_install:"装到本地",
    demo_note:"这是模拟板:数据是编的,一切都跑在你的浏览器里,碰不到你的电脑。",
    demo_i_title:"你在看一个模拟板",
    demo_i_lede:"这是 Skills Hub 管理台的真实界面,只不过后端换成了浏览器内存里的假数据。随便点,坏不了。",
    demo_i_1h:"拨一个开关",
    demo_i_1d:"技能卡片上那排按钮。绿的 = 那个位置能用这个技能。真实版里,这一下就是建/删一条软链接。",
    demo_i_2h:"打开「用量分析」",
    demo_i_2d:"这是本工具真正独有的地方:每个技能被实际触发了多少次。注意「从未触发」那一格——装了但从没用过的技能。",
    demo_i_3h:"试试「组合」",
    demo_i_3d:"把常一起用的技能存成一组,一键开关整组。",
    demo_i_note:"数字是编出来的样例数据。真实版里,这些触发次数是从 Claude Code / Codex / OpenCode 各自的本地会话记录里数出来的——只读、不上传、不出你的机器。",
    demo_i_close:"开始点",
    demo_n_title:"装到本地(真正能用的版本)",
    demo_n_lede:"模拟板只能看。要真的管理你机器上的技能,把它跑起来——只要 Python 3.9+ 和 Git,不装任何依赖。",
    demo_n_win:"Windows 双击 start-windows.bat 也行。想要个原生窗口:pip install pywebview && python3 desktop.py。",
    demo_n_close:"关闭",
    // 侧栏底部原本报的是"本机后台服务在不在跑" —— 模拟板里没有任何服务,不能这么说
    autostart_on:"模拟板 · 跑在浏览器里", autostart_off:"模拟板 · 跑在浏览器里",
  };
  const E = {
    demo_tag:"live demo", demo_what:"What is this?", demo_install:"Install locally",
    demo_note:"A simulation: the data is made up, everything runs in your browser, nothing touches your machine.",
    demo_i_title:"You're looking at a simulation",
    demo_i_lede:"This is the real Skills Hub UI with its backend swapped for made-up data in your browser's memory. Click anything — you can't break it.",
    demo_i_1h:"Flip a toggle",
    demo_i_1d:"The row of buttons on each skill card. Green = the skill is available there. In the real thing, that click creates or removes a symlink.",
    demo_i_2h:"Open Insights",
    demo_i_2d:"This is the part no other skill manager has: how often each skill actually fired. Look at the \"never triggered\" tile — skills you installed and never used.",
    demo_i_3h:"Try Sets",
    demo_i_3d:"Group the skills you always use together, then flip the whole group with one click.",
    demo_i_note:"These numbers are sample data. In the real thing they're counted from Claude Code's, Codex's and OpenCode's own local session logs — read-only, never uploaded, never leaving your machine.",
    demo_i_close:"Start clicking",
    demo_n_title:"Install locally (the version that actually works)",
    demo_n_lede:"The demo is look-only. To really manage the skills on your machine, run it — Python 3.9+ and Git, no dependencies to install.",
    demo_n_win:"On Windows you can double-click start-windows.bat instead. Want a native window? pip install pywebview && python3 desktop.py.",
    demo_n_close:"Close",
    autostart_on:"Demo · runs in your browser", autostart_off:"Demo · runs in your browser",
  };
  // 挂进 app 自己的 i18n 表:这样右上角切语言时,引导条和弹窗跟着一起切
  Object.assign(I18N.zh, Z);
  Object.assign(I18N.en, E);
  applyI18n();

  const realToggle = window.toggleLang;
  window.toggleLang = function(){ realToggle(); applyI18n(); };

  // 头一次来的人,先解释这是什么;之后不再打扰
  try{
    if(!localStorage.getItem("demo_seen")){
      demoIntro.showModal();
      localStorage.setItem("demo_seen", "1");
    }
  }catch(e){ demoIntro.showModal(); }

  // 点到"需要本地才能做"的功能时,顺势把安装弹窗推到眼前
  let nudged = false;
  window.demoNudge = function(){
    if(nudged) return;
    nudged = true;
    setTimeout(() => demoInstall.showModal(), 900);
  };
})();
</script>
"""


def main():
    page = webui.PAGE

    # 1) CSRF 占位符:模拟板没有后端,给个常量占位
    html = page.replace("__CSRF__", "demo")

    # 2) 换掉 head 里的 title,补 SEO / 社交预览
    old_title = "<title>技能库</title>"
    if old_title not in html:
        sys.exit("找不到 <title>,webui.py 的 head 结构变了,先更新本脚本")
    html = html.replace(old_title, HEAD, 1)

    # 3) 假后端必须在 app 脚本之前装好,否则 load() 会真的去打 /api/state
    anchor = '<div class="toast" id="toast"></div>'
    if anchor not in html:
        sys.exit("找不到 toast 锚点,webui.py 的 body 结构变了,先更新本脚本")
    shim = SHIM.replace("__DEMO_DATA__", json.dumps(DATA, ensure_ascii=False))
    html = html.replace(anchor, anchor + "\n" + BAR_CSS + BAR_HTML.replace("__REPO__", REPO) + shim, 1)

    # 4) 引导条的脚本要等 app 定义完 I18N/applyI18n 才能挂,挪到 app 脚本之后
    tail = "</script></body></html>"
    if html.count(tail) != 1:
        sys.exit("结尾锚点不唯一,先更新本脚本")
    m = re.search(r'<script id="demo-ui">.*?</script>', html, re.S)
    ui = m.group(0)
    html = html.replace(ui, "", 1).replace(tail, "</script>\n" + ui + "\n</body></html>", 1)

    # 5) 收尾自检:本机痕迹一个都不许进公开页面
    for bad in ("claude-501", "simplelife", "scratchpad", "/private/tmp", "9de71147"):
        if bad in html:
            sys.exit(f"产物里残留本机路径片段 {bad!r} —— 中止")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"写出 {OUT.relative_to(ROOT)}  ({len(html)/1024:.0f} KB)")
    print(f"  技能 {len(DATA['state']['skills'])} 个,组合 {len(DATA['state']['sets'])} 个,"
          f"有触发记录 {len(DATA['usage'])} 个")


if __name__ == "__main__":
    main()
