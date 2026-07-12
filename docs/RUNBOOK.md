# RUNBOOK · skills-hub

## 架构一页

- **产品本体**:`webui.py`(Python 标准库,本地 loopback HTTP 服务)+ 可选 `desktop.py`(pywebview 壳)+ `skillctl`(bash CLI)。用户 clone 后本地运行,无服务端。
- **落地页**:`site/`(纯静态单页)→ Cloudflare Pages 项目 `skills-liangai` → 绑定 `skills.liangai.org`(橙云)。

## 部署路径与 CI 触发

- push 到 `main` 且改动涉及 `site/**` → `.github/workflows/deploy-site.yml` 用 `wrangler pages deploy site` 发布。
- 产品代码本身没有部署——用户侧 `git pull` 即升级。

## Secrets 清单

| 名称 | 位置 | 用途 |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | 本仓库 GitHub secrets | wrangler Pages 部署鉴权(与 liangai 体系其他仓库同一 token) |

`CLOUDFLARE_ACCOUNT_ID` 非机密,workflow 里明文:`fe15c2c6bceaa6066bce8cb02965b896`。

## 冒烟与回滚

- 冒烟:`curl -I https://skills.liangai.org` 返回 200;页面能看到截图与 GitHub 链接。
- 回滚:Cloudflare Pages 面板选择上一个 deployment 设为生产,或 revert 提交重推。
- 本体回归测试:`python3 -m unittest discover -s tests`(21 例,钉死"纯管理器"边界:无审核路径、点击之外无网络、令牌门禁更新、CSRF)。

## 常见故障

- Pages 部署 403 → `CLOUDFLARE_API_TOKEN` 缺失或权限不含 Pages。
- 页面统计无数据 → `site/index.html` 里 Umami 脚本仍是注释/占位 ID,需在 analytics.liangai.org 建站后替换并解除注释。
- Windows 用户反馈开关变"副本" → 正常回退行为(无 symlink 权限时 junction→copy),见 README“边界”。
