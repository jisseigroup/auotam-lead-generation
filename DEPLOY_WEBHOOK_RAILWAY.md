# Deploy `event_webhook.py` on Railway (unsubscribe + optional SendGrid webhook)

Unsubscribe links in outbound mail point at `BASE_URL/unsubscribe?e=...` (default `https://auotam.net`). That path **must** hit this Flask app **without** only redirecting the visitor away before `/unsubscribe` is handled.

## Prerequisites

- GitHub repo containing this project (or deploy from Railway CLI).
- `requirements.txt` includes `flask`.

## 1) Create a Railway project

1. Go to [Railway](https://railway.app) and sign in.
2. **New Project** → **Deploy from GitHub repo** → select `auotam-lead-genertion` (or your fork).
3. Railway detects a generic service. Open the service → **Settings**:
   - **Root directory**: leave empty if the repo root contains `event_webhook.py` and `requirements.txt`.
   - **Start command**:
     ```bash
     pip install -r requirements.txt && python3 event_webhook.py
     ```
   - Railway injects `PORT`; `event_webhook.py` uses `os.getenv("PORT", "8080")`.

## 2) Environment variables

In the Railway service → **Variables**:

| Variable | Example | Purpose |
|----------|---------|---------|
| `BASE_URL` | `https://your-app.up.railway.app` | Must match the **public URL** users open for unsubscribe (used in email copy if you set it). |
| `SENDGRID_WEBHOOK_SECRET` | random string | Optional; if set, SendGrid HTTP posts must include header `X-Webhook-Secret: <same value>`. |
| `PYTHONUNBUFFERED` | `1` | Recommended for logs. |

**Important:** Until DNS maps `auotam.net` to this app, either:

- set `BASE_URL` to your Railway URL and regenerate/send mail with that base, **or**
- add a **custom domain** on Railway and point a hostname (e.g. `unsubscribe.auotam.net`) to it, then use that host in `BASE_URL`.

## 3) Custom domain (recommended for production)

1. Railway → your service → **Settings** → **Networking** → **Custom Domain**.
2. Add e.g. `unsubscribe.auotam.net` and follow Railway DNS instructions (CNAME).
3. Set `BASE_URL=https://unsubscribe.auotam.net`.
4. In Hostinger DNS for `auotam.net`, add the CNAME record Railway shows.

If you keep the **apex** `auotam.net` on Hostinger for your marketing site, using a **subdomain** for the webhook app avoids conflicting with the main site.

## 4) Configure SendGrid Event Webhook (optional)

If you use SendGrid inbound events:

- **HTTP POST URL**: `https://<your-public-host>/webhook/sendgrid`
- Ensure the shared secret matches `SENDGRID_WEBHOOK_SECRET` if you enabled it.

AUOTAM production mail currently targets **Amazon SES**; SES bounce/complaint handling uses SNS separately. This endpoint still satisfies **unsubscribe** hosting for CAN-SPAM-style opt-out links.

## 5) Smoke tests

After deploy:

```text
curl -sS "https://<host>/unsubscribe?e=dGVzdEBleGFtcGxlLmNvbQ"
```

You should see HTML containing “You have been unsubscribed.” (after decoding, use a real token from `encode_email_token` in `email_agent.py`).

---

## Cost Explorer IAM (scheduler host)

The machine or IAM user running `run_scheduler.py` needs Cost Explorer read access, for example:

- `AWSBillingReadOnlyAccess`, or  
- a custom policy allowing `ce:GetCostAndUsage` on `*`.

Cost Explorer API calls use region **`us-east-1`** (configured in `auotam/cost_guard.py`).
