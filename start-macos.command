#!/bin/bash
# Skills Hub —— 双击启动本地管理台(http://127.0.0.1:7799)
exec python3 "$(cd "$(dirname "$0")" && pwd)/webui.py"
