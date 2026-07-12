# Run Skills Hub at login / 开机自启

The manager is a tiny loopback HTTP server — keeping it resident means the page is always there when you need it. All recipes below run `webui.py --no-open` so nothing pops up at login.

## macOS (launchd)

Save as `~/Library/LaunchAgents/com.skills-hub.webui.plist` (adjust the two paths):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.skills-hub.webui</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOU/skills-hub/webui.py</string>
    <string>--no-open</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/YOU/skills-hub</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.skills-hub.webui.plist
launchctl kickstart -k gui/$UID/com.skills-hub.webui   # restart after upgrades
```

## Linux (systemd user unit)

Save as `~/.config/systemd/user/skills-hub.service`:

```ini
[Unit]
Description=Skills Hub manager

[Service]
ExecStart=/usr/bin/python3 %h/skills-hub/webui.py --no-open
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now skills-hub
```

## Windows (Task Scheduler)

```powershell
schtasks /Create /TN "Skills Hub" /SC ONLOGON /TR "pythonw C:\path\to\skills-hub\webui.py --no-open"
```

Or simply put a shortcut to `start-windows.bat` into `shell:startup`.

> Note: on Windows, links prefer real symlinks (enable *Developer Mode* in Settings → System → For developers), fall back to directory junctions, then to plain copies. Everything works either way; copies just need a re-toggle to pick up library edits.
