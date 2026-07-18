"""
bestteam UI automated test runner (Playwright).

Mirrors docs/ui-testing-guide.md against the CURRENT app:
  - No public registration: uses the operator-provisioned seed accounts.
  - Monitor / Wizard are org-user surfaces  -> logged in as `demo`.
  - Advanced / Memory are platform-admin-only -> logged in as `op`.
  - Advanced config is org-scoped: the org selector must be set on the
    org-scoped tabs (Workflows / Knowledge bases).

Requires, before running:
  - Backend up on :8000, started WITH `BESTTEAM_DEMO_WORKFLOWS=1` so the demo
    workflows appear in the Monitor dropdown (they are off by default).
  - Frontend up on :5173.
  - The seed accounts `demo` / `op` present (they are in the dev DB; recreate
    with `python -m ui.backend.admin create-user ...` — see the guide).

Skipped: T2-3 (needs the backend stopped mid-run) and T4 (wizard AI steps need
a real LLM key).

Run: .venv\\Scripts\\python.exe docs\\run_ui_tests.py
"""
import time, json, sys, urllib.request, urllib.error
from playwright.sync_api import sync_playwright, expect as pw_expect

BASE = "http://localhost:5173"
API  = "http://127.0.0.1:8000"

DEMO = ("demo", "demo-pass-123")  # org user (default org): Monitor, Wizard
OP   = ("op", "op-pass-123")      # platform admin (no org): Advanced, Memory
ORG_LABEL = "Default Organization"  # the default org's display name in the selector

PASSED, FAILED = [], []


def ok(name):
    PASSED.append(name)
    print(f"  [PASS] {name}")


def fail(name, reason=""):
    FAILED.append(name)
    print(f"  [FAIL] {name}" + (f" -- {reason}" if reason else ""))


def run(name, fn):
    try:
        fn()
        ok(name)
    except Exception as e:
        fail(name, str(e)[:140])


def api_login(username, password):
    """Log in via the API; returns True on success. Used only for preflight."""
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        API + "/api/auth/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return "access_token" in json.loads(r.read())
    except urllib.error.HTTPError:
        return False


def login_ui(page, account, land="/"):
    username, password = account
    page.goto(BASE + "/login")
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("button[type=submit]")
    page.wait_for_url(BASE + land, timeout=8000)


def logout(page):
    page.click("button.logout-button")
    page.wait_for_url("**/login", timeout=5000)


def open_advanced_tab(page, label):
    page.click(f".advanced-kinds button:has-text('{label}')")
    page.wait_for_selector(".advanced-list", timeout=3000)
    time.sleep(0.3)


def preflight():
    """Fail fast with a clear message if the environment isn't set up."""
    problems = []
    if not api_login(*DEMO):
        problems.append(f"cannot log in as '{DEMO[0]}' — provision it: "
                        f"python -m ui.backend.admin create-user {DEMO[0]} --org default")
    if not api_login(*OP):
        problems.append(f"cannot log in as '{OP[0]}' — provision it: "
                        f"python -m ui.backend.admin create-user {OP[0]} --platform && "
                        f"python -m ui.backend.admin promote {OP[0]}")
    if problems:
        print("Preflight failed:")
        for p in problems:
            print("  -", p)
        print("\nAlso ensure the backend was started with BESTTEAM_DEMO_WORKFLOWS=1.")
        sys.exit(2)


def test_all():
    preflight()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=150)
        ctx = browser.new_context()
        page = ctx.new_page()

        # ── T1 . Authentication (as demo) ────────────────────────────────────
        print("\n== T1 . Authentication ==")

        def t1_1():
            page.goto(BASE + "/")
            page.wait_for_url("**/login", timeout=6000)
            login_ui(page, DEMO)
            assert page.url == BASE + "/", f"Expected /, got {page.url}"
        run("T1-1 Auth guard + successful login", t1_1)

        def t1_2():
            logout(page)
            login_ui(page, DEMO)
            assert page.url == BASE + "/"
        run("T1-2 Logout and log back in", t1_2)

        def t1_3():
            page.goto(BASE + "/login")
            page.fill("#username", DEMO[0])
            page.fill("#password", "wrongpassword!")
            page.click("button[type=submit]")
            time.sleep(1.5)
            assert "/login" in page.url, f"Expected /login, got {page.url}"
            page.wait_for_selector(".banner-error", timeout=4000)
        run("T1-3 Wrong password stays on login with error", t1_3)

        login_ui(page, DEMO)  # re-login after T1-3

        def t1_4():
            ctx2 = browser.new_context()
            p2 = ctx2.new_page()
            p2.goto(BASE + "/advanced")
            p2.wait_for_url("**/login", timeout=5000)
            ctx2.close()
        run("T1-4 Unauthenticated access redirects to /login", t1_4)

        # ── T2 . Monitor Page (as demo) ──────────────────────────────────────
        print("\n== T2 . Monitor Page ==")

        def t2_1():
            js_errors = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))
            page.goto(BASE + "/")
            page.wait_for_selector("select", timeout=8000)
            options = page.locator("select option").all_inner_texts()
            assert len(options) > 0, ("Workflow dropdown is empty -- was the backend "
                                      "started with BESTTEAM_DEMO_WORKFLOWS=1?")
            bad = [e for e in js_errors if "Cannot read properties of undefined" in e]
            assert not bad, f"TypeError still present: {bad[0]}"
        run("T2-1 Workflow dropdown loads without TypeError", t2_1)

        def t2_2():
            page.goto(BASE + "/")
            page.wait_for_selector("select", timeout=8000)
            opts = page.locator("select option").all_inner_texts()
            target = "code_review" if "code_review" in opts else opts[0]
            page.select_option("select", label=target)
            page.fill("textarea", "def add(a, b): return a + b")
            page.click("button:has-text('Run')")
            page.wait_for_selector(".event.event-run_completed", timeout=30000)
            page.wait_for_selector(".result", timeout=5000)
        run("T2-2 Run workflow and see live trace + final output", t2_2)

        print("  [SKIP] T2-3 Unreachable backend banner (would stop the server)")

        # ── T3 . Advanced Page (as op) ───────────────────────────────────────
        print("\n== T3 . Advanced Page ==")
        logout(page)
        login_ui(page, OP, land="/")  # op lands on / then we navigate
        page.goto(BASE + "/advanced")
        page.wait_for_selector(".advanced-kinds", timeout=6000)

        def t3_1():
            for label in ["Workflows", "Skills", "Knowledge bases", "Tools", "Model catalog"]:
                page.click(f".advanced-kinds button:has-text('{label}')")
                page.wait_for_selector(".advanced-list", timeout=3000)
                time.sleep(0.3)
            # The removed resources must not reappear as tabs.
            labels = page.locator(".advanced-kinds button").all_inner_texts()
            assert "Agents" not in labels and "Teams" not in labels, \
                f"Removed tabs present: {labels}"
        run("T3-1 Navigate current resource tabs (no Agents/Teams)", t3_1)

        def t3_2_org_switch_clears_editor():
            # Regression: switching org must clear the editor, else Save would
            # write the previous org's item into the newly selected org.
            open_advanced_tab(page, "Skills")
            # Seed one platform skill so there's something to open.
            SEED = f"seed_{int(time.time())}"
            page.fill(".advanced-new input", SEED)
            page.click(".advanced-new button:has-text('New')")
            page.wait_for_selector(f".advanced-editor h2:has-text('{SEED}')", timeout=4000)
            page.fill(".advanced-editor textarea",
                      json.dumps({"instructions": "seed", "description": "seed"}, indent=2))
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success", timeout=6000)
            page.click(f".advanced-list button:has-text('{SEED}')")
            page.wait_for_selector(f".advanced-editor h2:has-text('{SEED}')", timeout=4000)
            # Switch org selector to the org tier; the editor must reset.
            page.select_option(".advanced-org select", label=ORG_LABEL)
            pw_expect(page.locator(".advanced-editor .hint")).to_be_visible(timeout=4000)
            pw_expect(page.locator(".advanced-editor textarea")).to_have_count(0)
            page.select_option(".advanced-org select", value="__platform__")  # restore
        run("T3-2 Switching org clears the editor (cross-tenant guard)",
            t3_2_org_switch_clears_editor)

        open_advanced_tab(page, "Skills")
        SKILL = f"skill_{int(time.time())}"
        SKILL_BODY = json.dumps({
            "instructions": "Always reply professionally. End with a polite sign-off.",
            "description": "Professional email style",
        }, indent=2)

        def t3_3():
            page.fill(".advanced-new input", SKILL)
            page.click(".advanced-new button:has-text('New')")
            page.wait_for_selector(f".advanced-editor h2:has-text('{SKILL}')", timeout=4000)
            page.fill(".advanced-editor textarea", SKILL_BODY)
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success", timeout=6000)
            page.wait_for_selector(f".advanced-list button:has-text('{SKILL}')", timeout=4000)
        run("T3-3 Create a Skill", t3_3)

        def t3_4():
            page.click(f".advanced-list button:has-text('{SKILL}')")
            raw = json.loads(page.locator(".advanced-editor textarea").input_value())
            raw["description"] = "Formal email writing style"
            page.fill(".advanced-editor textarea", json.dumps(raw, indent=2))
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success", timeout=5000)
            open_advanced_tab(page, "Knowledge bases")  # navigate away (Agents tab is gone)
            open_advanced_tab(page, "Skills")
            page.click(f".advanced-list button:has-text('{SKILL}')")
            val = json.loads(page.locator(".advanced-editor textarea").input_value())
            assert val.get("description") == "Formal email writing style", f"Got: {val.get('description')}"
        run("T3-4 Edit Skill and verify persistence", t3_4)

        def t3_5():
            page.click(f".advanced-list button:has-text('{SKILL}')")
            page.fill(".advanced-editor textarea", '{"instructions": "test"')  # missing }
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-error:has-text('Not valid JSON')", timeout=4000)
        run("T3-5 Malformed JSON rejected with 'Not valid JSON'", t3_5)

        def t3_6():
            page.click(f".advanced-list button:has-text('{SKILL}')")
            page.fill(".advanced-editor textarea", "{}")
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-error", timeout=6000)
            err = page.locator(".banner-error").inner_text()
            assert "instructions" in err.lower() or "validation" in err.lower(), f"Unexpected: {err}"
        run("T3-6 Missing 'instructions' rejected by backend", t3_6)

        def t3_7():
            page.click(f".advanced-list button:has-text('{SKILL}')")
            page.fill(".advanced-editor textarea", SKILL_BODY)  # restore valid
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success", timeout=5000)
            page.click(".advanced-editor button:has-text('Delete')")
            # After delete the editor hides (selectedId cleared); confirm the
            # item vanished from the list.
            pw_expect(page.locator(f".advanced-list button:has-text('{SKILL}')")).to_have_count(0, timeout=5000)
        run("T3-7 Delete Skill removes it from the list", t3_7)

        CATALOG_SPEC = f"fake:model_{int(time.time())}"

        def t3_8():
            open_advanced_tab(page, "Model catalog")
            page.fill(".advanced-new input", CATALOG_SPEC)
            page.click(".advanced-new button:has-text('New')")
            page.wait_for_selector(f".advanced-editor h2:has-text('{CATALOG_SPEC}')", timeout=4000)
            page.fill(".advanced-editor textarea", json.dumps({
                "display_name": "Test model",
                "description": "Smoke-test entry",
                "tier": "economy",
                "input_price_per_1k": 0,
                "output_price_per_1k": 0,
            }, indent=2))
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success", timeout=5000)
            page.wait_for_selector(f".advanced-list button:has-text('{CATALOG_SPEC}')", timeout=4000)
        run("T3-8 Create Model Catalog entry", t3_8)

        def t3_9():
            open_advanced_tab(page, "Tools")
            page.click(".advanced-list button:has-text('web_search')")
            pw_expect(page.locator(".advanced-readonly-text")).to_be_visible(timeout=4000)
            pw_expect(page.locator(".advanced-editor textarea")).to_have_count(0)
        run("T3-9 Tools tab is read-only", t3_9)

        WF = f"wf_{int(time.time())}"
        WF_BODY = json.dumps({
            "name": WF,
            "teams": [{"name": "t", "mode": "sequential", "agents": ["a"]}],
            "agents": [{"name": "a", "role": "Asst", "goal": "Help",
                        "backstory": "Friendly AI assistant.", "model": "fake:Hello! How can I help?"}],
            "workflow": {"steps": ["t"]},
        }, indent=2)

        def t3_10():
            open_advanced_tab(page, "Workflows")
            page.select_option(".advanced-org select", label=ORG_LABEL)  # workflows require an org
            page.fill(".advanced-new input", WF)
            page.click(".advanced-new button:has-text('New')")
            page.wait_for_selector(f".advanced-editor h2:has-text('{WF}')", timeout=4000)
            page.fill(".advanced-editor textarea", WF_BODY)
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success, .banner-error", timeout=8000)
            if page.locator(".banner-error").is_visible():
                raise AssertionError(f"Backend rejected save: {page.locator('.banner-error').inner_text()}")
        run("T3-10 Create a Workflow in org 'default'", t3_10)

        def t3_11():
            # The workflow lives in org 'default'; only a 'default' org user
            # sees it in Monitor. op (org-less) can't -- switch to demo.
            logout(page)
            login_ui(page, DEMO)
            page.goto(BASE + "/")
            page.wait_for_selector("select", timeout=8000)
            opts = page.locator("select option").all_inner_texts()
            assert WF in opts, f"{WF} not found in Monitor dropdown for demo"
        run("T3-11 Org workflow appears in that org user's Monitor dropdown", t3_11)

        # ── T4 . Wizard ──────────────────────────────────────────────────────
        print("\n== T4 . Wizard ==")
        print("  [SKIP] T4-1..T4-6 (AI generation requires real LLM API key)")

        # ── T5 . Edge Cases ──────────────────────────────────────────────────
        print("\n== T5 . Edge Cases ==")

        def t5_1():
            # Already logged in as demo from T3-11.
            page.goto(BASE + "/")
            page.evaluate("localStorage.removeItem('bestteam_token')")
            page.goto(BASE + "/advanced")
            page.wait_for_url("**/login", timeout=5000)
        run("T5-1 Clearing token redirects to /login", t5_1)

        def t5_2():
            # Upsert is an admin/advanced action -> op.
            login_ui(page, OP, land="/")
            page.goto(BASE + "/advanced")
            page.wait_for_selector(".advanced-kinds", timeout=5000)
            open_advanced_tab(page, "Workflows")
            page.select_option(".advanced-org select", label=ORG_LABEL)
            page.fill(".advanced-new input", WF)
            page.click(".advanced-new button:has-text('New')")
            page.wait_for_selector(f".advanced-editor h2:has-text('{WF}')", timeout=4000)
            page.fill(".advanced-editor textarea", WF_BODY)
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success, .banner-error", timeout=8000)
            if page.locator(".banner-error").is_visible():
                raise AssertionError(f"Backend rejected upsert: {page.locator('.banner-error').inner_text()}")
            count = page.locator(f".advanced-list button:has-text('{WF}')").count()
            assert count == 1, f"Expected 1 entry, got {count}"
        run("T5-2 Saving same workflow name is an upsert (not a duplicate)", t5_2)

        def t5_3():
            login_ui(page, DEMO)
            page.goto(BASE + "/")
            page.wait_for_selector("select", timeout=8000)
            page.fill("textarea", "")
            assert page.locator("button:has-text('Run')").is_disabled(), \
                "Run button should be disabled with empty input"
        run("T5-3 Run button disabled with empty input", t5_3)

        browser.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    total = len(PASSED) + len(FAILED)
    skipped = 2  # T2-3 and T4 block
    print(f"\n{'='*55}")
    print(f"Results: {len(PASSED)}/{total} passed, {len(FAILED)} failed, {skipped} skipped")
    print(f"{'='*55}")
    if FAILED:
        print("\nFailed:")
        for f in FAILED:
            print(f"  [FAIL] {f}")
    else:
        print("All tests passed!")
    return len(FAILED) == 0


if __name__ == "__main__":
    success = test_all()
    sys.exit(0 if success else 1)
