#!/usr/bin/env python3
"""
CoralPay CIP portal — automated wallet balance alert.

Logs in with username/password + Google Authenticator (TOTP), reads the balance
from /wallets, and pushes an alert to Slack.

First run: set HEADFUL=1 in .env and run this script so you can SEE the page and
adjust the SELECTORS below to match the real DOM (right-click -> Inspect).
Once it works headful, set HEADFUL=0 and schedule it.
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
THRESHOLD = os.environ.get("BALANCE_THRESHOLD", "").strip()
WARNING_THRESHOLD = os.environ.get("BALANCE_WARNING_THRESHOLD", "").strip()
PAGERDUTY_ROUTING_KEY = os.environ.get("PAGERDUTY_ROUTING_KEY", "").strip()
HEADFUL = os.environ.get("HEADFUL", "0") == "1"

# Stable dedup key so repeated events map to the same PagerDuty incident.
PD_KEY_LOW = "coralpay-balance-low"
# Persistent latch across runs: remembers whether we've already paged for the
# CURRENT low episode, so we page once and don't re-page until the balance recovers.
# STATE_URL = an npoint.io bin URL (https://api.npoint.io/<id>) for ephemeral hosts
# that can't keep a local file (e.g. GitHub Actions). Falls back to STATE_FILE for dev.
STATE_URL = os.environ.get("STATE_URL", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
# Auto-resolve the ticket this many seconds after paging (keeps MTTR clean).
# The latch stays set, so it won't re-page until the balance actually recovers.
AUTO_RESOLVE_SECONDS = int(os.environ.get("AUTO_RESOLVE_SECONDS", "300"))
# While the portal is unreachable, re-notify at this cadence instead of every run
# (spammy) or not at all (looks like the monitor died). Recovery still auto-clears.
UNREACHABLE_REMINDER_SECONDS = int(os.environ.get("UNREACHABLE_REMINDER_SECONDS", "1800"))

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


class PortalUnreachable(Exception):
    """Raised when a page navigation times out — as opposed to a real script bug or
    a rejected login. Kept distinct so this can get its own quiet, edge-triggered
    alert instead of a per-run error card. `stage` distinguishes the LOGIN page
    (a genuine full-portal-down signal) from /wallets specifically (observed to be
    merely slow, not down, during a recurring nightly window)."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage  # "login" or "wallets"


# status -> (emoji, headline) for the alert card
_STATUS = {
    "ok": (":large_green_circle:", "Wallet balance"),
    "warning": (":large_yellow_circle:", "Balance approaching low threshold"),
    "low": (":red_circle:", "LOW wallet balance"),
    "error": (":rotating_light:", "Balance check FAILED"),
    "suspect": (":grey_question:", "Balance read needs manual check"),
    "challenge": (":warning:", "CoralPay System Challenge — escalate to CoralPay support"),
    "unreachable": (":large_orange_diamond:", "CoralPay portal unreachable"),
}


WAT = timezone(timedelta(hours=1))  # West Africa Time (UTC+1, no DST)


def _timestamp() -> str:
    return datetime.now(WAT).strftime("%Y-%m-%d %H:%M WAT")


def _body_lines(balance: str, threshold: str, detail: str) -> list:
    """The Current / Threshold / Site lines shown in the alert."""
    lines = [f"Current: {balance}"]
    if threshold:
        lines.append(f"Threshold: {threshold}")
    lines.append("Site: *CoralPay*")
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
    """Read the 'already paged' latch. If STATE_URL is set (an npoint.io bin),
    read it over HTTP — needed because GitHub Actions is ephemeral. Falls back
    to a local file for dev. Any read failure -> assume not paged; the PagerDuty
    dedup_key then prevents a duplicate incident, so this is safe."""
    if STATE_URL:
        try:
            r = requests.get(STATE_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else {"paged": False}
        except Exception as e:  # noqa: BLE001
            print(f"[warn] could not read remote state ({e}); assuming not paged")
            return {"paged": False}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"paged": False}


def save_state(state: dict) -> None:
    """Persist the latch. POSTs the full JSON to the npoint.io bin when STATE_URL
    is set; otherwise writes the local file."""
    if STATE_URL:
        try:
            r = requests.post(STATE_URL, json=state, timeout=20)
            r.raise_for_status()
            print(f"[info] remote state updated: {state}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] could not write remote state ({e}); latch may not persist")
        return
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def notify(status: str, balance: str = "—", threshold: str = "", detail: str = "") -> None:
    """Send a formatted alert card to whichever channel is configured."""
    emoji, headline = _STATUS[status]
    # Slack rejects blocks whose text exceeds ~3000 chars (a full browser log/traceback
    # would 400). Keep the note short so error alerts actually send.
    detail = (detail or "").strip().replace("\n", " ")
    if len(detail) > 300:
        detail = detail[:300] + " …(truncated)"
    # Plain-text fallback for Slack notifications/previews.
    fallback = f"{emoji} {headline}\n" + "\n".join(_body_lines(balance, threshold, detail))
    fallback += f"\nChecked {_timestamp()}"

    if SLACK_WEBHOOK_URL:
        r = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": fallback, "blocks": _slack_blocks(status, balance, threshold, detail)},
            timeout=20,
        )
        r.raise_for_status()
    else:
        print("[warn] No Slack webhook configured; message was:\n" + fallback)


def parse_amount(raw: str):
    """Pull a numeric amount out of a string like 'NGN 1,234,567.89'."""
    m = re.search(r"[-+]?[\d,]*\.?\d+", raw.replace(",", ""))
    return float(m.group()) if m else None


# Common phrases shown by anti-bot/WAF interstitials (Cloudflare, Akamai, DataDome,
# PerimeterX, etc.) instead of the real page. Headless Chromium gets flagged by these
# far more often than a real browser — which fits "loads fine manually, times out
# headless" exactly. Checked against the visible body text when a wait times out.
_BOT_CHALLENGE_MARKERS = (
    "checking your browser", "cloudflare", "captcha", "are you human",
    "verify you are human", "just a moment", "access denied", "attention required",
    "unusual traffic", "security check", "please wait",
)


def _dump_debug(page, label: str = "debug") -> None:
    """On failure, log where we ended up and WHAT was actually on the page. No
    screenshot/HTML file capture: on ephemeral compute (GitHub Actions) those files don't
    persist, so everything here goes to stdout, which the Actions log does
    capture. Goal is to distinguish 'selector is fine, just slow' from 'this isn't
    the login page at all' (bot-detection challenge, cookie banner, maintenance
    page, redirect) — since the site loads fine manually, a wrong-page theory is
    the leading suspect for a headless-only failure."""
    try:
        print(f"[{label}] final url:   {page.url}")
        print(f"[{label}] page title:  {page.title()}")

        # Every <input> actually present — if the real form uses different
        # name/id/type attributes than our selector expects, this proves it.
        try:
            inputs = page.eval_on_selector_all(
                "input",
                "els => els.map(e => ({name: e.name, id: e.id, type: e.type, "
                "placeholder: e.placeholder, visible: !!(e.offsetWidth || e.offsetHeight)}))",
            )
            print(f"[{label}] <input> elements on page: {inputs}")
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] could not enumerate inputs: {e}")

        # Iframes can hide the real form behind a challenge/consent overlay.
        try:
            iframes = page.eval_on_selector_all("iframe", "els => els.map(e => e.src)")
            if iframes:
                print(f"[{label}] iframes on page: {iframes}")
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] could not enumerate iframes: {e}")

        # Visible body text — both to eyeball what's actually rendered, and to
        # check it against known bot-challenge phrasing.
        try:
            body_text = page.inner_text("body")
            print(f"[{label}] body text (first 500 chars): {body_text[:500]!r}")
            hits = [m for m in _BOT_CHALLENGE_MARKERS if m in body_text.lower()]
            if hits:
                print(f"[{label}] POSSIBLE BOT-CHALLENGE / INTERSTITIAL PAGE — "
                      f"matched marker(s): {hits}")
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] could not read body text: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"[{label}] could not read page state: {e}")


def fetch_balance() -> tuple:
    """Returns (raw_balance_text, system_challenge_detected)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not HEADFUL)
        ctx = browser.new_context()
        page = ctx.new_page()
        # Surface browser-console errors and any HTTP error responses (403/429/5xx
        # are the classic signature of a WAF/anti-bot block on the login request) —
        # captured in the Actions log, useful when the site loads fine manually
        # but headless Chromium gets a different response.
        page.on("console", lambda msg: print(f"[console:{msg.type}] {msg.text}"))
        page.on("response", lambda resp: print(f"[net] {resp.status} {resp.url}")
                if resp.status >= 400 else None)
        try:
            # 1) Login page
            # domcontentloaded, not networkidle: the portal has background network
            # activity (polling/analytics/chat widget) that may never go fully idle,
            # which can hang "networkidle" until it times out. The actual gates that
            # matter (login fields, OTP field, balance element) are waited for below.
            # The username field is also observed (like /wallets) to sometimes render
            # client-side well after domcontentloaded fires, even though the portal
            # loads fine manually. So: a generous timeout + one retry (re-navigating)
            # before treating it as unreachable, rather than failing fast on a
            # merely-slow render.
            login_timeout_ms = 60000
            for attempt in (1, 2):
                try:
                    page.goto(f"{URL}/", wait_until="domcontentloaded", timeout=login_timeout_ms)
                    page.wait_for_selector(SEL_USERNAME, timeout=login_timeout_ms)
                    break
                except PWTimeout as e:
                    _dump_debug(page, label=f"debug:login-attempt-{attempt}")
                    if attempt == 2:
                        raise PortalUnreachable("login", str(e)) from e
                    print(f"[warn] login page slow to load (>{login_timeout_ms/1000:.0f}s); "
                          f"retrying once")
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

            # 3) Wallets page (see domcontentloaded note above). This specific page has
            # been observed to be slow (not down) during a recurring nightly window —
            # the portal itself loads fine manually, it's just this page's backend data
            # that can take a while. So: a generous timeout + one retry before treating
            # it as unreachable, rather than failing fast on a merely-slow load.
            page.wait_for_load_state("domcontentloaded")
            wallets_goto_timeout_ms = 60000
            for attempt in (1, 2):
                try:
                    page.goto(f"{URL}/wallets", wait_until="domcontentloaded",
                               timeout=wallets_goto_timeout_ms)
                    break
                except PWTimeout as e:
                    if attempt == 2:
                        raise PortalUnreachable("wallets", str(e)) from e
                    print(f"[warn] /wallets slow to load (>{wallets_goto_timeout_ms/1000:.0f}s); "
                          f"retrying once")
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

            # If it's still reading exactly zero after the retry above, check whether
            # CoralPay's own UI is showing its "System challenge" error banner — that
            # means the portal itself failed to load the balance (a site-side issue,
            # not something we can retry past), and it should be escalated to CoralPay.
            system_challenge = False
            if parse_amount(raw) == 0:
                try:
                    system_challenge = page.get_by_text(
                        re.compile("system challenge", re.IGNORECASE)
                    ).count() > 0
                except Exception:  # noqa: BLE001
                    system_challenge = False

            return raw, system_challenge
        except Exception:
            _dump_debug(page)
            raise
        finally:
            browser.close()


def main() -> int:
    try:
        raw, system_challenge = fetch_balance()
    except PortalUnreachable as e:
        # A goto/navigation timeout — as opposed to a real bug. Throttled heartbeat:
        # alert when this starts, then a "still down" reminder every
        # UNREACHABLE_REMINDER_SECONDS while it continues (NOT every run —
        # that's spammy — but also not total silence, which is indistinguishable
        # from the monitor having died during a multi-hour episode). Self-clears the
        # moment a run reaches the portal again, via the normal balance card.
        state = load_state()
        now = datetime.now(timezone.utc)
        print(f"[warn] Portal unreachable at '{e.stage}' stage (navigation timeout): {e}")

        is_first = not state.get("unreachable_alerted", False)
        since = state.get("unreachable_since")
        since_dt = datetime.fromisoformat(since) if since else now
        last_notified = state.get("unreachable_last_notified")
        last_notified_dt = datetime.fromisoformat(last_notified) if last_notified else None
        due_for_reminder = (
            last_notified_dt is not None
            and (now - last_notified_dt).total_seconds() >= UNREACHABLE_REMINDER_SECONDS
        )

        if is_first or due_for_reminder:
            ongoing = ""
            if not is_first:
                mins = int((now - since_dt).total_seconds() // 60)
                ongoing = f" Ongoing ~{mins}m; still auto-retrying every run."
            if e.stage == "wallets":
                # Confirmed (manually) that the portal itself loads fine; only this
                # specific page has been observed to be slow, not down, during a
                # recurring nightly window — likely backend/data latency on their end.
                detail = (f"CoralPay's /wallets page took too long to load, even after a "
                          f"retry. The portal loads fine manually — likely backend latency "
                          f"on CoralPay's end. Reminders every ~{UNREACHABLE_REMINDER_SECONDS // 60}m "
                          f"while ongoing; auto-clears on recovery.{ongoing} {URL}/wallets")
            else:
                # The LOGIN page itself timed out — unlike the known /wallets slowness,
                # this suggests the whole portal may be down, not just one page.
                detail = (f"Could not even reach the CoralPay LOGIN page (navigation "
                          f"timeout). Unlike the known slow /wallets page, this suggests "
                          f"the portal may be down. Reminders every "
                          f"~{UNREACHABLE_REMINDER_SECONDS // 60}m while ongoing; auto-clears "
                          f"on recovery.{ongoing} {URL}/")
            notify("unreachable", detail=detail)
            save_state({
                **state,
                "unreachable_alerted": True,
                "unreachable_since": since_dt.isoformat(),
                "unreachable_last_notified": now.isoformat(),
            })
            print(f"[warn] {'First' if is_first else 'Reminder'} alert sent; "
                  f"next reminder in ~{UNREACHABLE_REMINDER_SECONDS // 60}m if still down.")
        else:
            print("[warn] Already alerted; not due for a reminder yet, staying quiet.")
        return 0
    except Exception as e:  # noqa: BLE001
        notify("error", detail=str(e))
        print(f"[error] {e}", file=sys.stderr)
        return 1

    # Reached the portal successfully — clear any "unreachable" flag from a prior
    # outage now that it's confirmed back up. Loaded once here and reused below.
    state = load_state()
    if state.get("unreachable_alerted", False):
        state["unreachable_alerted"] = False
        state.pop("unreachable_since", None)
        state.pop("unreachable_last_notified", None)
        save_state(state)
        print("[info] Portal reachable again; cleared 'unreachable' latch.")

    amount = parse_amount(raw)
    print(f"[info] Balance read: {raw!r} -> {amount}")

    # Always send the balance. If thresholds are set, flag yellow when approaching low,
    # and red when below low. Warning threshold (amber) triggers if balance < warning
    # but still > low; this is informational only (no PagerDuty page).
    limit = None
    threshold_display = ""
    warning_limit = None
    warning_threshold_display = ""
    if THRESHOLD:
        try:
            limit = float(THRESHOLD)
            threshold_display = f"₦{limit:,.2f}"
        except ValueError:
            print(f"[warn] BALANCE_THRESHOLD {THRESHOLD!r} is not a number; ignoring.")
    if WARNING_THRESHOLD:
        try:
            warning_limit = float(WARNING_THRESHOLD)
            warning_threshold_display = f"₦{warning_limit:,.2f}"
        except ValueError:
            print(f"[warn] BALANCE_WARNING_THRESHOLD {WARNING_THRESHOLD!r} is not a number; ignoring.")

    # An exact ₦0.00 (or an unparseable value) is almost certainly a bad read, not a
    # real balance: the wallet can go negative, so it would essentially never land
    # precisely on zero. Rather than treat it as a genuine low (and page) or a healthy
    # value (and resolve), flag it for a human to confirm. We do NOT touch the
    # PagerDuty latch here, so a glitchy read can neither page nor resolve.
    if amount is None or amount == 0:
        if system_challenge:
            # CoralPay's own UI showed its "System challenge" error — the portal
            # itself failed to load the balance. This is a site-side issue on
            # CoralPay's end, not something a retry on our side can fix.
            notify(
                "challenge",
                balance=raw,
                threshold=threshold_display,
                detail=(f"CoralPay's portal is showing a 'System challenge' error and "
                        f"could not load the balance. This is an issue on CoralPay's "
                        f"end — please escalate to CoralPay support for resolution. "
                        f"Portal: {URL}/wallets"),
            )
            print("[warn] CoralPay System Challenge detected; escalation alert sent, latch untouched.")
        else:
            reason = "reads exactly ₦0.00" if amount == 0 else "could not be read"
            notify(
                "suspect",
                balance=raw,
                threshold=threshold_display,
                detail=(f"Balance {reason} after retry. This is likely a page-load glitch. "
                        f"Please confirm manually: {URL}/wallets"),
            )
            print(f"[warn] Balance {reason}; sent manual-check alert, latch untouched.")
        return 0

    is_low = limit is not None and amount is not None and amount < limit
    is_warning = (warning_limit is not None and amount is not None and amount < warning_limit
                  and not is_low)

    # Edge-triggered PagerDuty: page once when we cross into low, then stay latched
    # until the balance recovers (funded). The latch survives across runs via STATE_FILE.
    # (state was already loaded above, right after a successful fetch_balance().)
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
            # Merge (not replace) so we don't clobber the unreachable-latch key.
            save_state({**state, "paged": True})
            print("[info] Low balance: opened PagerDuty incident and latched.")
            if AUTO_RESOLVE_SECONDS > 0:
                print(f"[info] Waiting {AUTO_RESOLVE_SECONDS}s, then auto-resolving the ticket.")
                time.sleep(AUTO_RESOLVE_SECONDS)
                pagerduty("resolve", PD_KEY_LOW)
                print("[info] Auto-resolved ticket; latch stays set until balance recovers.")
        else:
            print("[info] Low balance: already paged this episode; not re-paging.")
    elif is_warning:
        notify("warning", balance=raw, threshold=threshold_display)
        print(f"[info] Warning: balance approaching low threshold ({warning_threshold_display}).")
    else:
        notify("ok", balance=raw)
        if already_paged:
            pagerduty("resolve", PD_KEY_LOW)   # idempotent if already auto-resolved
            save_state({**state, "paged": False})
            print("[info] Balance recovered: resolved PagerDuty incident and reset latch.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
