#!/usr/bin/env python3
"""Native macOS shell for Evidence Notebook."""
from __future__ import annotations

import threading

import server
import webview
from webview.menu import Menu, MenuAction
from desktop_policy import install_policy


def run() -> None:
    owned = server.make_server(port=0)
    worker = threading.Thread(target=owned.serve_forever, name="evidence-notebook-http", daemon=True)
    origin = f"http://127.0.0.1:{owned.server_port}"
    window = None
    started = False

    def quit_app(_window=None):
        if window is not None:
            window.destroy()

    menu = [Menu("File", [MenuAction("Quit Evidence Notebook", quit_app)])]
    try:
        worker.start()
        started = True
        install_policy(origin)
        window = webview.create_window("Evidence Notebook", origin + "/?desktop=1", width=1120, height=760, min_size=(760, 560), menu=menu)
        webview.start()
    finally:
        if started:
            owned.shutdown()
        owned.server_close()
        if started:
            worker.join(timeout=3)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        import AppKit

        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Evidence Notebook could not start")
        alert.setInformativeText_("The local app failed to open. Restart Evidence Notebook and try again.")
        alert.addButtonWithTitle_("OK")
        alert.runModal()
        raise SystemExit(1)
