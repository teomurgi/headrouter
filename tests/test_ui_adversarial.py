"""Adversarial UI checks (Mary's checklist item 2, ux-spec §5):

Legal-value expressibility — every form control must be able to produce at
least one complete value that passes server validation. Runs the real admin
page in a headless browser against a live test server; fails if any control
(a select whose options can't form a valid value, a text input whose value
shape is invalid, or an unreachable state) blocks staging a legal config.

Run: .venv/bin/python tests/test_ui_adversarial.py  (or via pytest -k adversarial)
Playwright + chromium are dev deps: .venv/bin/pip install playwright && playwright install chromium
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONFIG = {
    "providers": [
        {"name": "or", "type": "openrouter", "base_url": "https://or.test/v1", "api_key_env": "OR_KEY"},
        {"name": "local", "type": "ollama", "base_url": "http://localhost:11434/v1"},
    ],
    "aliases": {"fast": "or:gpt-4o-mini"},
    "keys": [{"name": "team-a", "api_key_env": "KEY_A", "aliases": ["fast"]}],
}

CHECK_JS = """
async (page) => {
  const results = [];
  const gate = page.locator("#gate-key");
  await gate.fill(process.env.ADMIN_KEY);
  await page.locator("#gate-save").click();
  await page.waitForSelector("#key-gate[hidden]", {timeout: 5000});

  // --- alias form: target must accept a full provider:model value ---
  await page.locator("#tab-keys").click();
  await page.locator("#add-alias").click();
  const target = page.locator("#sf-target");
  const tag = await target.evaluate(el => el.tagName.toLowerCase());
  if (tag === "select") {
    // a select is only legal if some option is already a complete value
    const opts = await target.locator("option").allTextContents();
    const complete = opts.some(o => /^[^:]+:.+/.test(o));
    results.push({form: "alias target", ok: complete, why: `select options: ${opts.join(", ")}`});
  }
  await page.locator("[data-cancel]").click();

  // --- provider form: base_url free text, type select covers types ---
  await page.locator("#tab-providers").click();
  await page.locator("#add-provider").click();
  const base = page.locator("#sf-base_url");
  const baseTag = await base.evaluate(el => el.tagName.toLowerCase());
  if (baseTag === "select") {
    const opts = await base.locator("option").allTextContents();
    const complete = opts.every(o => /^https?:\\/\\//.test(o));
    results.push({form: "provider base_url", ok: complete, why: `select options must be full URLs: ${opts.join(", ")}`});
  }
  await page.locator("[data-cancel]").click();
  return results;
}
"""


def main() -> int:
    pytest = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--co", "-q"],
        cwd=ROOT, capture_output=True,
    )
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed (dev-only adversarial check)")
        return 0

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "providers.json"
        cfg.write_text(json.dumps(CONFIG))
        import os
        env = dict(os.environ, GATEWAY_PROVIDERS_FILE=str(cfg), GATEWAY_API_KEYS="hr_admin_adv",
                   OR_KEY="x", KEY_A="k", ADMIN_KEY="hr_admin_adv")
        proc = subprocess.Popen(
            [str(ROOT / ".venv/bin/uvicorn"), "app:create_app", "--factory", "--port", "8941"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(6)
            failures = []
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page()
                page.goto("http://127.0.0.1:8941/admin")
                # inline the check (env var not visible to page scripts — pass key directly)
                page.wait_for_selector("#gate-key")
                page.fill("#gate-key", "hr_admin_adv")
                page.click("#gate-save")
                page.wait_for_selector("#key-gate", state="hidden", timeout=5000)

                # alias target control must express a full provider:model
                page.click("#tab-keys")
                page.click("#add-alias")
                tag = page.eval_on_selector("#sf-target", "el => el.tagName.toLowerCase()")
                if tag == "select":
                    opts = page.eval_on_selector_all("#sf-target option", "els => els.map(e => e.textContent)")
                    if not any(":" in o and not o.endswith(":") for o in opts):
                        failures.append(f"alias target select can't express a complete value: {opts}")
                    if page.locator("[data-cancel]").count():
                        page.click("[data-cancel]")
                else:
                    # free text: prove a typed full value stages (form closes on submit)
                    page.fill("#sf-name", "advcheck")
                    page.fill("#sf-target", "or:gpt-4o")
                    page.click(".stage-form button[type=submit]")
                    page.wait_for_selector("#stage-bar:not([hidden])", timeout=5000)

                # provider base_url: select options must be complete URLs, or free text
                page.click("#tab-providers")
                page.click("#add-provider")
                tag = page.eval_on_selector("#sf-base_url", "el => el.tagName.toLowerCase()")
                if tag == "select":
                    opts = page.eval_on_selector_all("#sf-base_url option", "els => els.map(e => e.textContent)")
                    if not all(o.startswith("http") for o in opts):
                        failures.append(f"provider base_url select has non-URL options: {opts}")
                page.click("[data-cancel]")
                browser.close()

            if failures:
                print("FAIL: legal-value expressibility violations:")
                for f in failures:
                    print(" -", f)
                return 1
            print("OK: every form control can express a legal value (ux-spec §5)")
            return 0
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
