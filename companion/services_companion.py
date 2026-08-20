from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


APP_TITLE = "RadioTEDU Services"


def app_dir() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def load_config() -> dict:
    path = app_dir() / "connections.json"
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def service_state(name: str) -> str:
    result = subprocess.run(["sc.exe", "query", name], capture_output=True, text=True, timeout=8)
    if result.returncode != 0:
        return "not installed"
    for line in result.stdout.splitlines():
        if "STATE" in line:
            return "running" if "RUNNING" in line else "stopped"
    return "unknown"


def service_action(name: str, action: str) -> tuple[bool, str]:
    if action == "restart":
        subprocess.run(["sc.exe", "stop", name], capture_output=True, timeout=15)
        time.sleep(2)
        action = "start"
    result = subprocess.run(["sc.exe", action, name], capture_output=True, text=True, timeout=20)
    return result.returncode == 0 or "already" in result.stdout.lower(), (result.stdout or result.stderr).strip()


def health(urls: list[str]) -> tuple[bool, str]:
    if not urls:
        return True, "no health URL"
    errors = []
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=4) as response:
                if 200 <= response.status < 400:
                    return True, f"healthy ({response.status})"
                errors.append(f"HTTP {response.status}")
        except Exception as exc:
            errors.append(type(exc).__name__)
    return False, "offline: " + ", ".join(errors)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        self.title(APP_TITLE)
        self.geometry("780x480")
        self.minsize(680, 410)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.rows: dict[str, dict] = {}
        self._build()
        self.refresh()

    def _build(self) -> None:
        ttk.Label(self, text=APP_TITLE, font=("Segoe UI", 20, "bold")).pack(anchor="w", padx=20, pady=(18, 2))
        ttk.Label(self, text="JukeLocal, Voting and AI host only — no streaming engine.").pack(anchor="w", padx=20, pady=(0, 14))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=20)
        for component in self.cfg["components"]:
            frame = ttk.LabelFrame(body, text=component["label"])
            frame.pack(fill="x", pady=5)
            state = ttk.Label(frame, text="checking…", width=28)
            state.pack(side="left", padx=12, pady=11)
            for action in ("start", "restart", "stop"):
                ttk.Button(frame, text=action.title(), command=lambda c=component, a=action: self.act(c, a)).pack(side="left", padx=3)
            url = component.get("ui_url")
            if url:
                ttk.Button(frame, text="Open", command=lambda value=url: webbrowser.open(value)).pack(side="left", padx=8)
            self.rows[component["id"]] = {"state": state, "component": component}
        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=20, pady=14)
        ttk.Button(footer, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(footer, text="Start all", command=lambda: self.all_action("start")).pack(side="left", padx=8)
        ttk.Button(footer, text="Connections", command=lambda: os.startfile(app_dir() / "connections.json")).pack(side="left")
        self.status = ttk.Label(footer, text="")
        self.status.pack(side="right")

    def _background(self, task) -> None:
        threading.Thread(target=task, daemon=True).start()

    def refresh(self) -> None:
        self.status.config(text="Checking…")
        def work() -> None:
            results = []
            for row in self.rows.values():
                c = row["component"]
                state = service_state(c["service_name"])
                ok, detail = health(c.get("health_urls", []))
                results.append((row, state, ok, detail))
            def render() -> None:
                for row, state, ok, detail in results:
                    row["state"].config(text=f"{state} · {detail}", foreground="#177245" if state == "running" and ok else "#a33")
                self.status.config(text="Updated")
            self.after(0, render)
        self._background(work)

    def act(self, component: dict, action: str) -> None:
        self.status.config(text=f"{action.title()} {component['label']}…")
        def work() -> None:
            ok, detail = service_action(component["service_name"], action)
            self.after(0, self.refresh)
            if not ok:
                self.after(0, lambda: messagebox.showerror(APP_TITLE, detail or "Service action failed. Run as administrator."))
        self._background(work)

    def all_action(self, action: str) -> None:
        for row in self.rows.values():
            self.act(row["component"], action)


def main() -> int:
    if os.name != "nt":
        raise SystemExit("RadioTEDU Services is a Windows application.")
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
