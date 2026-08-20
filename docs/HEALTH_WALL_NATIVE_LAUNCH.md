# Native Health Wall launch

RadioTEDU OnAir can use its existing native WebView shell as a dedicated,
read-only Health Wall. This launches no Chrome or Edge window and does not open
the operator wall.

```powershell
RadioTEDU-OnAir.exe --health-wall
```

The Health Wall is intentionally fixed to the dedicated local page
`http://127.0.0.1:8100/static/health-wall/index.html`. That page reads only the
sanitized, loopback-only `GET /api/monitor/snapshot` endpoint. It accepts no remote host: launching
with `CLEANROOM_SHELL_MODE=health-wall` or `--health-wall` fails unless
`CLEANROOM_HOST` is a loopback address. It uses a separate WebView profile,
blocks every navigation except that fixed URI, suppresses popups/context menus,
disables developer tools and browser accelerators, and has no operator
controls. If Windows opens the wall before the backend or WebView runtime is
ready, the native shell retries with bounded backoff until it becomes
available. The wall opens as a centered, normal Windows window at roughly 86%
of the monitor work area. It has a title bar and standard minimize, maximize,
move, and resize behavior; it is not forced fullscreen or always-on-top.
Ordinary window close requests, including Alt+F4, are still rejected in Health
Wall mode so a display user cannot permanently dismiss the wall.

When the installer Health Wall startup option is selected, it creates a
per-user Scheduled Task named `RadioTEDU OnAir Health Wall`. The task has a
30-second logon delay, ignores duplicate launch requests, and restarts an
unexpectedly exited wall. The installer creates it in the original interactive
user's context and removes it during uninstall. The native shell also holds a
global Windows mutex, so another user session cannot open a competing wall.

For planned maintenance, an administrator must first disable the Scheduled
Task and then stop its running task/process; re-enable the task after support
work. Do not host this UI from a Windows service or Session 0. The backend and
the no-password monitor endpoint must remain directly loopback-bound: do not
publish them through a reverse proxy, tunnel, or port-forward, because that
would defeat the local-only Health Wall boundary. The backend/service host
remains responsible for automatic backend startup; the Health Wall only
observes local readiness.
