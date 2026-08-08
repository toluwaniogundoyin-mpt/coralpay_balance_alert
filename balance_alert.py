#!/usr/bin/env python3
"""
CoralPay CIP portal — automated wallet balance alert.

Logs in with username/password + Google Authenticator (TOTP), reads the balance
from /wallets, and pushes an alert to Slack or Telegram.

"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pyotp
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

URL = os.environ["CORALPAY_URL"].rstrip("/")
USERNAME = os.environ["CORALPAY_USERNAME"]
PASSWORD = os.environ["CORALPAY_PASSWORD"]
TOTP_SECRET = os.environ["CORALPAY_TOTP_SECRET"].replace(" ", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
THRESHOLD = os.environ.get("BALANCE_THRESHOLD", "").strip()
PAGERDUTY_ROUTING_KEY = os.environ.get("PAGERDUTY_ROUTING_KEY", "").strip()
HEADFUL = os.environ.get("HEADFUL", "0") == "1"

# Stable dedup key so repeated events map to the same PagerDuty incident.
PD_KEY_LOW = "coralpay-balance-low"
# Persistent latch across runs: remembers whether we've already paged for the
# CURRENT low episode, so we page once and don't re-page until the balance recovers.
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
# Auto-resolve the ticket this many seconds after paging (keeps MTTR clean).
# The latch stays set, so it won't re-page until the balance actually recovers.
AUTO_RESOLVE_SECONDS = int(os.environ.get("AUTO_RESOLVE_SECONDS", "300"))

# ---------------------------------------------------------------------------
# SELECTORS — the ONLY part likely to need tweaking for the real site.
# Use CSS selectors. Run headful the first time and inspect the elements.
# The values below are best-effort guesses with common fallbacks.
# ---------------------------------------------------------------------------
SEL_USERNAME = "input[name='username'], input[type='email'], #username"
SEL_PASSWORD = "input[name='password'], input[type='password'], #password"
SEL_LOGIN_BTN = "button[type='submit'], button:has-text('Login'), button:has-text('Sign in')"
SEL_OTP = "input[name='otp'], input[name='code'], input[type='tel'], input[autocomplete='one-time-code']"
SEL_OTP_BTN = "button[type='submit'], button:has-text('Verify'), button:has-text('Submit')"
# On /wallets — the <h6> whose text reads "Balance : ₦...". Anchored on the
# text because the CSS classes (text-white, ps-3) are generic Bootstrap utils.
SEL_BALANCE = "h6:has-text('Balance')"


# status -> (emoji, headline) for the alert card
_STATUS = {
    "ok": (":large_green_circle:", "Wallet balance"),
    "low": (":red_circle:", "LOW wallet balance"),
    "error": (":rotating_light:", "Balance check FAILED"),
}


WAT = timezone(timedelta(hours=1))  # West Africa Time (UTC+1, no DST)


def _timestamp() -> str:
    return datetime.now(WAT).strftime("%Y-%m-%d %H:%M WAT")


def _body_lines(balance: str, threshold: str, detail: str) -> list:
    """The Current / Threshold / Site lines shown in the alert."""
    lines = [f"Current: {balance}"]
    if threshold:
        lines.append(f"Threshold: {threshold}")
    lines.append("Site: CoralPay")
    if detail:
        lines.append(f"Note: {detail}")
    return lines


def _slack_blocks(status: str, balance: str, threshold: str, detail: str) -> list:
    """Build a Block Kit alert card."""
    emoji, headline = _STATUS[status]
    body = "\n".join(_body_lines(balance, threshold, detail))
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {headline}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f":clock3: Checked {_timestamp()}"},
        ]},
    ]


def pagerduty(action: str, dedup_key: str, summary: str = "", severity: str = "warning",
              details: dict = None) -> None:
    """Send a PagerDuty Events API v2 event (trigger / resolve).

    'trigger' opens (or updates) an incident keyed by dedup_key; 'resolve'
    closes the incident with that same key. No-op if no routing key is set.
    """
    if not PAGERDUTY_ROUTING_KEY:
        return
    event = {
        "routing_key": PAGERDUTY_ROUTING_KEY,
        "event_action": action,
        "dedup_key": dedup_key,
    }
    if action == "trigger":
        payload = {
            "summary": summary[:1024],          # PD caps summary length
            "severity": severity,               # critical | error | warning | info
            "source": "cipportal.coralpay.com",
            "component": "wallet-balance",
        }
        if details:
            payload["custom_details"] = details  # renders as a key/value panel in PD
        event["payload"] = payload
    r = requests.post("https://events.pagerduty.com/v2/enqueue", json=event, timeout=20)
    r.raise_for_status()
    print(f"[info] PagerDuty {action} ({dedup_key})")


def load_state() -> dict:
    """Read the persisted latch. Missing/corrupt file -> not yet paged."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"paged": False}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def notify(status: str, balance: str = "—", threshold: str = "", detail: str = "") -> None:
    """Send a formatted alert card to whichever channel is configured."""
    emoji, headline = _STATUS[status]
    # Plain-text fallback (used by Telegram, and by Slack notifications/previews).
    fallback = f"{emoji} {headline}\n" + "\n".join(_body_lines(balance, threshold, detail))
    fallback += f"\nChecked {_timestamp()}"

    sent = False
    if SLACK_WEBHOOK_URL:
        r = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": fallback, "blocks": _slack_blocks(status, balance, threshold, detail)},
            timeout=20,
        )
        r.raise_for_status()
        sent = True
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": fallback},
            timeout=20,
        )
        r.raise_for_status()
        sent = True
    if not sent:
        print("[warn] No alert channel configured; message was:\n" + fallback)


def parse_amount(raw: str):
    """Pull a numeric amount out of a string like 'NGN 1,234,567.89'."""
    m = re.search(r"[-+]?[\d,]*\.?\d+", raw.replace(",", ""))
    return float(m.group()) if m else None


def _dump_debug(page) -> None:
    """On failure, save debug artifacts with EVERYTHING redacted except the first
    balance. Redaction is done by BLANKING the actual DOM text (not via CSS, which
    a site's own !important rules can override) — so no page styling can defeat it.
    The same redacted DOM is used for both the screenshot and the HTML dump. If
    redaction can't be applied, the artifacts are skipped rather than leaked."""
    try:
        print(f"[debug] final url:   {page.url}")
        print(f"[debug] page title:  {page.title()}")

        # Mark the first balance so redaction leaves just that one readable.
        try:
            bal = page.locator(SEL_BALANCE).first
            if bal.count() > 0:
                bal.evaluate("el => el.setAttribute('data-keep', '1')")
        except Exception as e:  # noqa: BLE001
            print(f"[debug] no balance element to keep visible: {e}")

        # Blank every text node + input/placeholder EXCEPT inside the kept balance.
        # Returns the redacted outerHTML so the screenshot and HTML match exactly.
        try:
            html = page.evaluate(
                """() => {
                    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    const ns = []; while (w.nextNode()) ns.push(w.currentNode);
                    ns.forEach(n => {
                        if (n.parentElement && n.parentElement.closest('[data-keep]')) return;
                        if (n.nodeValue && n.nodeValue.trim()) n.nodeValue = '[REDACTED]';
                    });
                    document.querySelectorAll('input,textarea,select').forEach(i => {
                        if (i.closest('[data-keep]')) return;
                        try { i.value = ''; } catch (e) {}
                        i.setAttribute('value', '');
                        if (i.hasAttribute('placeholder')) i.setAttribute('placeholder', '');
                    });
                    return document.documentElement.outerHTML;
                }"""
            )
        except Exception as e:  # noqa: BLE001
            # Could not redact -> do NOT capture anything, to avoid leaking data.
            print(f"[debug] redaction failed; skipping ALL artifacts to avoid leak: {e}")
            return

        # Screenshot the already-redacted DOM.
        try:
            page.screenshot(path="debug.png", full_page=True)
            print("[debug] saved redacted debug.png")
        except Exception as e:  # noqa: BLE001
            print(f"[debug] could not save screenshot: {e}")

        try:
            with open("debug.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("[debug] saved redacted debug.html")
        except Exception as e:  # noqa: BLE001
            print(f"[debug] could not write html: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"[debug] could not capture debug artifacts: {e}")


def fetch_balance() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not HEADFUL)
        ctx = browser.new_context()
        page = ctx.new_page()
        try:
            # 1) Login page
            page.goto(f"{URL}/", wait_until="networkidle")
            page.fill(SEL_USERNAME, USERNAME)
            page.fill(SEL_PASSWORD, PASSWORD)
            page.click(SEL_LOGIN_BTN)

            # 2) TOTP / Google Authenticator step
            try:
                page.wait_for_selector(SEL_OTP, timeout=15000)
                code = pyotp.TOTP(TOTP_SECRET).now()
                page.fill(SEL_OTP, code)
                page.click(SEL_OTP_BTN)
            except PWTimeout:
                # No OTP field appeared — maybe already past 2FA, continue.
                pass

            # 3) Wallets page
            page.wait_for_load_state("networkidle")
            page.goto(f"{URL}/wallets", wait_until="networkidle")
            page.wait_for_selector(SEL_BALANCE, timeout=20000)

            # The balance loads via AJAX AFTER the element renders, so it briefly
            # shows a placeholder (₦0.00 / empty). Wait until it shows a real,
            # non-zero amount. If the account is genuinely 0 this times out and we
            # read it as-is — so a true zero still works, just a few seconds slower.
            try:
                page.wait_for_function(
                    """() => {
                        const el = [...document.querySelectorAll('h6')]
                            .find(e => /balance/i.test(e.textContent));
                        if (!el) return false;
                        const n = parseFloat(el.textContent.replace(/[^0-9.]/g, ''));
                        return !isNaN(n) && n > 0;
                    }""",
                    timeout=15000,
                )
            except PWTimeout:
                print("[warn] balance still 0/empty after wait; reading as-is")

            text = page.locator(SEL_BALANCE).first.inner_text()
            # Normalise: drop &nbsp;, collapse whitespace, strip the "Balance :" label.
            text = text.replace("\xa0", " ")
            text = re.sub(r"\s+", " ", text).strip()
            raw = re.sub(r"(?i)^balance\s*:?\s*", "", text).strip()
            return raw
        except Exception:
            _dump_debug(page)
            raise
        finally:
            browser.close()


def main() -> int:
    try:
        raw = fetch_balance()
    except Exception as e:  # noqa: BLE001
        notify("error", detail=str(e))
        print(f"[error] {e}", file=sys.stderr)
        return 1

    amount = parse_amount(raw)
    print(f"[info] Balance read: {raw!r} -> {amount}")

    # Always send the balance. If a threshold is set, flag it red when below.
    limit = None
    threshold_display = ""
    if THRESHOLD:
        try:
            limit = float(THRESHOLD)
            threshold_display = f"₦{limit:,.2f}"
        except ValueError:
            print(f"[warn] BALANCE_THRESHOLD {THRESHOLD!r} is not a number; ignoring.")

    is_low = limit is not None and amount is not None and amount < limit

    # Edge-triggered PagerDuty: page once when we cross into low, then stay latched
    # until the balance recovers (funded). The latch survives across runs via STATE_FILE.
    state = load_state()
    already_paged = state.get("paged", False)

    if is_low:
        notify("low", balance=raw, threshold=threshold_display)
        if not already_paged:
            summary = f"CoralPay wallet balance LOW — Current: {raw}, Threshold: {threshold_display}, Site: CoralPay"
            details = {
                "current_balance": raw,
                "threshold": threshold_display or "not set",
                "site": "CoralPay",
                "wallet_url": f"{URL}/wallets",
                "checked_at": _timestamp(),
            }
            pagerduty("trigger", PD_KEY_LOW, summary, "critical", details=details)
            # Latch BEFORE the wait so we never re-page even if the run is interrupted.
            save_state({"paged": True})
            print("[info] Low balance: opened PagerDuty incident and latched.")
            if AUTO_RESOLVE_SECONDS > 0:
                print(f"[info] Waiting {AUTO_RESOLVE_SECONDS}s, then auto-resolving the ticket.")
                time.sleep(AUTO_RESOLVE_SECONDS)
                pagerduty("resolve", PD_KEY_LOW)
                print("[info] Auto-resolved ticket; latch stays set until balance recovers.")
        else:
            print("[info] Low balance: already paged this episode; not re-paging.")
    else:
        notify("ok", balance=raw, threshold=threshold_display)
        if already_paged:
            pagerduty("resolve", PD_KEY_LOW)   # idempotent if already auto-resolved
            save_state({"paged": False})
            print("[info] Balance recovered: resolved PagerDuty incident and reset latch.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
