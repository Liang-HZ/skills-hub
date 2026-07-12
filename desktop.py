#!/usr/bin/env python3
"""可选的桌面窗口壳:把管理台包成一个本地原生窗口(用系统自带 WebView,体积很小)。

依赖:  pip install pywebview
运行:  python3 desktop.py

不装 pywebview 也完全不影响使用——直接 python3 webui.py 用浏览器打开即可。
"""
import threading

import webui


def main():
    try:
        import webview
    except ImportError:
        raise SystemExit("需要先安装 pywebview:  pip install pywebview\n"
                         "(不想装也可以直接 python3 webui.py 用浏览器打开)")
    srv = webui.serve(port=webui.PORT, open_browser=False)
    if srv is None:          # 端口被占用:管理台已在运行,直接连上去
        url = f"http://127.0.0.1:{webui.PORT}"
    else:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
    webview.create_window("技能库 · Skills Hub", url, width=1180, height=780)
    webview.start()


if __name__ == "__main__":
    main()
