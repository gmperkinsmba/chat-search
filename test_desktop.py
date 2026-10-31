"""Native origin and owned HTTP lifecycle checks; no window is opened here."""
import threading
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path

import server
from build_desktop import reviewed_assets
from desktop_policy import navigation_policy


class DesktopTests(unittest.TestCase):
    def test_ephemeral_loopback_lifecycle_and_native_assets(self):
        owned = server.make_server(port=0)
        self.assertEqual(owned.server_name, "127.0.0.1")
        worker = threading.Thread(target=owned.serve_forever, daemon=True)
        worker.start()
        try:
            conn = HTTPConnection("127.0.0.1", owned.server_port, timeout=3)
            conn.request("GET", "/?desktop=1")
            page = conn.getresponse()
            body = page.read().decode()
            self.assertEqual(page.status, 200)
            self.assertIn("Chat Search", body)
            self.assertIn("/desktop.css", body)
            conn.request("GET", "/desktop.css")
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
            bad = HTTPConnection("127.0.0.1", owned.server_port, timeout=3)
            bad.request("GET", "/", headers={"Host": "attacker.invalid"})
            self.assertEqual(bad.getresponse().status, 403)
            bad.close()
        finally:
            owned.shutdown()
            owned.server_close()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(owned.fileno(), -1)

    def test_origin_policy_rejects_remote_and_non_http_navigation(self):
        origin = "http://127.0.0.1:49125/"
        self.assertEqual(navigation_policy(origin, origin, False, False), "allow")
        self.assertEqual(navigation_policy("https://example.com", origin, False, False), "cancel")
        self.assertEqual(navigation_policy("file:///etc/passwd", origin, True, False), "cancel")

    def test_asset_manifest_rejects_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            root.mkdir()
            (root / "safe.css").write_text("body{}")
            manifest = Path(temporary) / "manifest.txt"
            manifest.write_text("safe.css\n")
            self.assertEqual(reviewed_assets(root, manifest), ("safe.css",))
            manifest.write_text("../outside.css\n")
            with self.assertRaises(SystemExit):
                reviewed_assets(root, manifest)
            target = Path(temporary) / "target.css"
            target.write_text("body{}")
            (root / "linked.css").symlink_to(target)
            manifest.write_text("linked.css\n")
            with self.assertRaises(SystemExit):
                reviewed_assets(root, manifest)


if __name__ == "__main__":
    unittest.main()
