# AUOTAM Email Automation - Deployment Guide (macOS)

This project runs as a `launchd` user service using:
- `run_prod.sh` (startup command)
- `com.auotam.email-automation.plist` (service definition)

## 1) One-time setup

From project root:

```bash
cp .env.example .env
mkdir -p output/email
```

Edit `.env` and set real values:
- `AWS_REGION`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AUOTAM_FROM_EMAIL`
- `AUOTAM_REPLY_TO`
- optional: `SES_CONFIGURATION_SET`

Install Python dependency for live sending:

```bash
pip3 install boto3
```

## 2) Install service

```bash
cp com.auotam.email-automation.plist ~/Library/LaunchAgents/com.auotam.email-automation.plist
launchctl load ~/Library/LaunchAgents/com.auotam.email-automation.plist
launchctl start com.auotam.email-automation
```

## 3) Service control commands

### Start
```bash
launchctl start com.auotam.email-automation
```

### Stop
```bash
launchctl stop com.auotam.email-automation
```

### Restart
```bash
launchctl stop com.auotam.email-automation
launchctl start com.auotam.email-automation
```

### Reload after plist changes
```bash
launchctl unload ~/Library/LaunchAgents/com.auotam.email-automation.plist
launchctl load ~/Library/LaunchAgents/com.auotam.email-automation.plist
launchctl start com.auotam.email-automation
```

### Uninstall
```bash
launchctl stop com.auotam.email-automation || true
launchctl unload ~/Library/LaunchAgents/com.auotam.email-automation.plist || true
rm -f ~/Library/LaunchAgents/com.auotam.email-automation.plist
```

## 4) Verify it's running

```bash
launchctl list | rg auotam
```

Check logs:

```bash
tail -f output/email/launchd.out.log
tail -f output/email/launchd.err.log
```

Check send artifacts:
- `output/email/send_log.csv`
- `output/email/status.jsonl`

## 5) Dry-run test before live

```bash
python3 email_agent.py send \
  --input-csv output/sba_test/all_businesses.csv \
  --dry-run \
  --daily-cap 25 \
  --start-hour-est 0 \
  --end-hour-est 24 \
  --log-csv output/email/send_log_test.csv \
  --status-jsonl output/email/status_test.jsonl
```

## 6) Live run notes

- Scheduler enforces Mon-Fri and EST business-hour windows.
- Daily target is controlled by `AUOTAM_DAILY_TARGET` in `.env`.
- Keep reply inbox monitored for warm leads.
- Review bounce/complaint trends daily; ramp gradually if warming a new domain.

## 7) Quick troubleshooting

- **Service not listed**
  - Re-run `launchctl load ...plist` and check plist path.
- **No sends happening**
  - Confirm current EST time is within configured window.
  - Confirm `output/sba/all_businesses.csv` exists and has valid emails.
- **SES errors**
  - Confirm domain/email identity verified in SES.
  - Confirm production access (not sandbox).
  - Confirm AWS credentials/region are correct.
- **Python import error for boto3**
  - `pip3 install boto3`

