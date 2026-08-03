# CoralPay wallet balance alert

Automates: login (username + password) → Google Authenticator TOTP → read balance
on `/wallets` → alert to Slack/Telegram. Runs headless, no phone needed.

## Setup

```bash
cd coralpay-balance-alert
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env      # then edit .env with your real values
```

Fill in `.env`:
- `CORALPAY_USERNAME` / `CORALPAY_PASSWORD`
- `CORALPAY_TOTP_SECRET` — the base32 "setup/manual entry key" from when you
  enrolled Google Authenticator. If you never saved it, re-enroll 2FA in the
  portal and copy the key that time. The 6-digit codes are derived from it.
- One alert channel: `SLACK_WEBHOOK_URL`, or `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.

## First run (fix selectors)

Set `HEADFUL=1` in `.env`, then:

```bash
python balance_alert.py
```

Watch the browser. If it can't find a field, open DevTools (right-click → Inspect)
on the real page and update the matching `SEL_*` selector near the top of
`balance_alert.py`. Once it prints the balance, set `HEADFUL=0`.

## Schedule (macOS launchd, every 30 min)

Create `~/Library/LaunchAgents/com.coralpay.balance.plist` pointing at the venv
python and this script, then `launchctl load` it. (Ask and I'll generate it.)

## Security note

`.env` holds credentials + your 2FA secret — anyone with that file can generate
your OTP codes. Keep it `chmod 600`, never commit it. For stronger protection use
the macOS Keychain instead of a plaintext file.
