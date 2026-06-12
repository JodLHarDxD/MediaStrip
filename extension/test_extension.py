# Live test: load unpacked extension in real Chrome, verify catcher works.
# Run: python test_extension.py
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
<video controls src="https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4"></video>
</body></html>
"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = TEST_PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
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
                # branded Chrome 137+ ignores --load-extension without this
                "--disable-features=DisableLoadExtensionCommandLineSwitch",
            ],
        )
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"http://127.0.0.1:{PORT}/")
        page.wait_for_timeout(3000)

        # 1. injection guard set?
        results["injected_flag"] = page.evaluate("window.__mediastripInjected === true")
        # 2. launcher exists + visible (video on page -> count > 0)?
        results["launcher_exists"] = page.evaluate("!!document.querySelector('.__ms_launcher')")
        results["launcher_visible"] = page.evaluate(
            "document.querySelector('.__ms_launcher')?.classList.contains('__ms_visible') || false"
        )
        results["catch_count"] = page.evaluate(
            "document.querySelector('.__ms_count')?.textContent || '0'"
        )
        # 3. open panel, check rows render
        if results["launcher_exists"]:
            page.click(".__ms_launcher")
            page.wait_for_timeout(1500)
            results["panel_open"] = page.evaluate(
                "document.querySelector('.__ms_panel')?.classList.contains('__ms_open') || false"
            )
            results["panel_rows"] = page.evaluate(
                "document.querySelectorAll('.__ms_row').length"
            )
        # 4. Injection path + idempotency: run the exact executeScript call the
        #    onInstalled handler uses, against a tab that already has the script.
        #    Guard flag must prevent a duplicate UI.
        sw = ctx.service_workers[0] if ctx.service_workers else None
        results["service_worker_found"] = bool(sw)
        if sw:
            results["manual_inject"] = sw.evaluate(
                """async () => {
                    const tabs = await chrome.tabs.query({ url: ['http://*/*', 'https://*/*'] });
                    const out = [];
                    for (const t of tabs) {
                        try {
                            await chrome.scripting.insertCSS({ target: { tabId: t.id }, files: ['content.css'] });
                            await chrome.scripting.executeScript({ target: { tabId: t.id }, files: ['content.js'] });
                            out.push('ok:' + t.id);
                        } catch (e) {
                            out.push('fail:' + t.id + ':' + e.message);
                        }
                    }
                    return out;
                }"""
            )
            page.wait_for_timeout(1500)
            results["launchers_after_double_inject"] = page.evaluate(
                "document.querySelectorAll('.__ms_launcher').length"
            )

        # 5. REAL extension reload (what broke it for the user): old content
        #    script must tear down silently — zero 'context invalidated' errors.
        pre_err = len([e for e in errors if "context invalidated" in e.lower()])
        if sw:
            try:
                sw.evaluate("chrome.runtime.reload()")
            except Exception:
                pass  # connection drops as the extension reloads — expected
        page.wait_for_timeout(9000)  # > one 5s refresh tick + re-injection
        post_err = len([e for e in errors if "context invalidated" in e.lower()])
        results["invalidated_errors_after_reload"] = post_err - pre_err
        results["launchers_after_reload"] = page.evaluate(
            "document.querySelectorAll('.__ms_launcher').length"
        )
        # note: CLI-loaded extensions don't survive runtime.reload() in this
        # harness, so step 4 stands in for the post-reload re-injection.
        results["console_errors"] = [e for e in errors if "__ms" in e or "mediastrip" in e.lower() or "Extension context" in e]

        ctx.close()
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
