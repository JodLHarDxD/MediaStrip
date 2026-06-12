# Live test: load unpacked extension in real Chromium, verify the IDM-style flow:
# per-video button -> source panel -> download -> SSE progress -> Saved.
# Uses a local mock MediaStrip server so no network/real server is needed.
# Run: python test_extension.py  (needs: pip install playwright && playwright install chromium)
import http.server
import json
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

EXT = str(Path(__file__).parent.resolve())
PORT = 8765
TEST_PAGE = """
<!doctype html><html><head><title>ms-test</title></head><body>
<h1>MediaStrip test page</h1>
<video controls width="480" height="270"
  src="https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4"></video>
</body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/html"):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/extension/ping"):
            return self._send('{"ok": true, "app": "mediastrip"}', "application/json")
        if self.path.startswith("/stream/"):
            # mock SSE: progress -> done
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for ev in (
                {"type": "filename", "value": "test_video.mp4"},
                {"type": "progress", "percent": 42.0, "speed": "9.1MiB/s", "eta": "00:02"},
                {"type": "progress", "percent": 100.0, "speed": "", "eta": ""},
                {"type": "done", "filename": "test_video.mp4", "files": []},
            ):
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
            return
        self._send(TEST_PAGE)

    def do_POST(self):
        if self.path.startswith("/api/extension/download"):
            return self._send(
                '{"job_id": "t1", "kind": "direct", "watch_url": "/?job=t1"}', "application/json"
            )
        self._send("{}", "application/json")

    def log_message(self, *a):
        pass


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    results = {}
    with sync_playwright() as p:
        profile = tempfile.mkdtemp(prefix="ms_ext_test_")
        ctx = p.chromium.launch_persistent_context(
            profile,
            headless=False,
            args=[
                f"--disable-extensions-except={EXT}",
                f"--load-extension={EXT}",
                "--no-first-run",
            ],
        )
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        # point the extension at the mock server
        sw = ctx.service_workers[0] if ctx.service_workers else None
        results["service_worker_found"] = bool(sw)
        if sw:
            sw.evaluate(f"chrome.storage.sync.set({{serverUrl: 'http://127.0.0.1:{PORT}'}})")

        page.goto(f"http://127.0.0.1:{PORT}/")
        page.wait_for_timeout(2500)

        # 1. per-video button attached + visible (brief reveal on attach)
        results["vbtn_count"] = page.evaluate("document.querySelectorAll('.__ms_vbtn').length")
        results["vbtn_visible"] = page.evaluate(
            "document.querySelector('.__ms_vbtn')?.classList.contains('__ms_vshow') || false"
        )
        # button pinned inside the video's top-right corner?
        results["vbtn_anchored"] = page.evaluate(
            """(() => {
                const b = document.querySelector('.__ms_vbtn')?.getBoundingClientRect();
                const v = document.querySelector('video')?.getBoundingClientRect();
                if (!b || !v) return false;
                return b.top >= v.top && b.top <= v.top + 60 && b.right <= v.right + 2 && b.left >= v.left;
            })()"""
        )

        # 2. click -> source panel with the direct mp4 option
        page.hover("video")
        page.click(".__ms_vbtn")
        page.wait_for_timeout(800)
        results["panel_open"] = page.evaluate("!!document.querySelector('.__ms_vpanel')")
        results["panel_options"] = page.evaluate("document.querySelectorAll('.__ms_vopt').length")
        results["first_option"] = page.evaluate(
            "document.querySelector('.__ms_vopt_label')?.textContent || ''"
        )

        # 3. download -> background relays mock SSE -> progress -> Saved
        page.click(".__ms_vgo")
        page.wait_for_timeout(3000)
        results["progress_shown"] = page.evaluate(
            "document.querySelector('.__ms_vprog')?.classList.contains('__ms_von') || false"
        )
        results["status_text"] = page.evaluate(
            "document.querySelector('.__ms_vstatus')?.textContent || ''"
        )
        results["bar_width"] = page.evaluate(
            "document.querySelector('.__ms_vbar_fill')?.style.width || ''"
        )

        # 4. dismiss: x hides the button for that video
        # (panel still open; close it first)
        page.click(".__ms_vclose")
        page.hover("video")
        page.wait_for_timeout(300)
        page.click(".__ms_vbtn .__ms_vx")
        page.hover("h1")
        page.hover("video")
        page.wait_for_timeout(500)
        results["dismissed_stays_hidden"] = page.evaluate(
            "!document.querySelector('.__ms_vbtn')?.classList.contains('__ms_vshow')"
        )

        # 5. real extension reload: old script must tear down silently
        pre_err = len([e for e in errors if "context invalidated" in e.lower()])
        if sw:
            try:
                sw.evaluate("chrome.runtime.reload()")
            except Exception:
                pass  # connection drops as the extension reloads — expected
        page.wait_for_timeout(8000)
        post_err = len([e for e in errors if "context invalidated" in e.lower()])
        results["invalidated_errors_after_reload"] = post_err - pre_err
        results["vbtns_after_reload"] = page.evaluate(
            "document.querySelectorAll('.__ms_vbtn').length"
        )
        results["console_errors"] = [
            e for e in errors if "__ms" in e or "mediastrip" in e.lower() or "Extension context" in e
        ]

        ctx.close()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
