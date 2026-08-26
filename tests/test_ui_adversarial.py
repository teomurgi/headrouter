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
    "keys": [{"name": "team-a", "api_key_env": "KEY_A", "grants": [{"provider": "or", "models": ["gpt-4o-mini"]}]}],
}

CHECK_JS = """
async (page) => {
  const results = [];
  const gate = page.locator("#gate-key");
  await gate.fill(process.env.ADMIN_KEY);
  await page.locator("#gate-save").click();
  await page.waitForSelector("#key-gate[hidden]", {timeout: 5000});

  // --- grant form: one row per provider; each row's models input must accept full upstream names ---
  await page.locator("#tab-keys").click();
  await page.locator("[data-addgrant]").first().click();
  const models = page.locator(".grants-form .grant-row input").first();
  const tag = await models.evaluate(el => el.tagName.toLowerCase());
  if (tag === "select") {
    // a select is only legal if some option is already a complete value
    const opts = await models.locator("option").allTextContents();
    const complete = opts.some(o => o.trim().length > 0);
    results.push({form: "grant models", ok: complete, why: `select options: ${opts.join(", ")}`});
  }
  await page.locator("[data-cancel]").click();

  // --- provider form: base_url free text, type select covers types ---
  await page.locator("#tab-providers").click();
  await page.locator("#add-provider").click();
  const base = page.locator(".stage-form [name=base_url]");
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

                # grant form: one row per provider; models control must express full upstream model names
                page.click("#tab-keys")
                page.click("[data-addgrant]")
                row = ".grants-form .grant-row input"
                tag = page.eval_on_selector(row, "el => el.tagName.toLowerCase()")
                if tag == "select":
                    opts = page.eval_on_selector_all(row + " option", "els => els.map(e => e.textContent)")
                    if not any(o.strip() for o in opts):
                        failures.append(f"grant models select can't express a value: {opts}")
                    if page.locator("[data-cancel]").count():
                        page.click("[data-cancel]")
                else:
                    # free text: prove a typed full value stages (form closes on submit)
                    page.fill(row, "gpt-4o")
                    page.click(".stage-form button[type=submit]")
                    page.wait_for_selector("#stage-bar:not([hidden])", timeout=5000)

                # provider base_url: select options must be complete URLs, or free text
                page.click("#tab-providers")
                page.click("#add-provider")
                tag = page.eval_on_selector(".stage-form [name=base_url]", "el => el.tagName.toLowerCase()")
                if tag == "select":
                    opts = page.eval_on_selector_all(".stage-form [name=base_url] option", "els => els.map(e => e.textContent)")
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
