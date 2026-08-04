#!/usr/bin/env python3
"""
CoralPay CIP portal — automated wallet balance alert.

Logs in with username/password + Google Authenticator (TOTP), reads the balance
from /wallets, and pushes an alert to Slack or Telegram.

First run: set HEADFUL=1 in .env and run this script so you can SEE the page and
adjust the SELECTORS below to match the real DOM (right-click -> Inspect).
Once it works headful, set HEADFUL=0 and schedule it.
"""

import os
import re
import sys
from datetime import datetime, timezone

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
HEADFUL = os.environ.get("HEADFUL", "0") == "1"

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


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _slack_blocks(status: str, balance: str, detail: str) -> list:
    """Build a Block Kit alert card."""
    emoji, headline = _STATUS[status]
    fields = [{"type": "mrkdwn", "text": f"*Balance:*\n{balance}"}]
    if detail:
        fields.append({"type": "mrkdwn", "text": f"*Note:*\n{detail}"})
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} CoralPay CIP"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{headline}*"}},
        {"type": "section", "fields": fields},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f":clock3: Checked {_timestamp()}"},
        ]},
    ]


def notify(status: str, balance: str = "—", detail: str = "") -> None:
    """Send a formatted alert card to whichever channel is configured."""
    emoji, headline = _STATUS[status]
    # Plain-text fallback (used by Telegram, and by Slack notifications/previews).
    fallback = f"{emoji} CoralPay CIP — {headline}: {balance}"
    if detail:
        fallback += f" ({detail})"
    fallback += f" · {_timestamp()}"

    sent = False
    if SLACK_WEBHOOK_URL:
        r = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": fallback, "blocks": _slack_blocks(status, balance, detail)},
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
    """On failure, record where we actually ended up (safe: URL/title to logs)
    and save a screenshot + HTML for inspection."""
    try:
        print(f"[debug] final url:   {page.url}")
        print(f"[debug] page title:  {page.title()}")
        page.screenshot(path="debug.png", full_page=True)
        with open("debug.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        print("[debug] saved debug.png and debug.html")
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

    if THRESHOLD:
        try:
            limit = float(THRESHOLD)
        except ValueError:
            limit = None
        if limit is not None and amount is not None and amount < limit:
            notify("low", balance=raw, detail=f"below threshold {THRESHOLD}")
        else:
            print("[info] Above threshold; no alert sent.")
    else:
        notify("ok", balance=raw)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
