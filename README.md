# Zyppia Google Ads connector

A private MCP server that lets Claude work in Zyppia's Google Ads accounts in plain conversation.
It covers client accounts under the Zyppia MCC (347-642-6639) and any account the signed-in person's
Google login was added to directly.

**Setup:** see [docs/DEPLOY.md](docs/DEPLOY.md) for a step-by-step guide.

## What it can do (phase 1: read-only)

| Tool | What it does |
| --- | --- |
| `list_accounts` | Every account you can access, with currency and whether it's via the MCC or direct |
| `performance_report` | Account, campaign, ad group, keyword, search term or ad performance for a date range |
| `run_gaql` | Any Google Ads Query Language report |
| `change_history` | Who changed what in the last 29 days |
| `find_locations` | Look up location IDs from place names |
| `keyword_ideas` | Keyword Planner ideas, volumes, competition and bid ranges (needs Basic API access) |

Changing campaigns, budgets, bids, ads and keywords comes in phase 2. Every change will go
through a preview and an explicit confirmation first.

## How it works

- **Sign-in:** Claude connects using OAuth. The server sends you to "Sign in with Google", which asks for
  your email and Google Ads access, and lets in only emails listed in `ALLOWED_EMAILS`.
- **No database:** client registrations, codes and tokens are encrypted and signed with `TOKEN_KEY`
  (Fernet), so the server stores nothing. Each person's Google refresh token is kept only inside their
  encrypted Claude token. Changing `TOKEN_KEY` signs everyone out.
- **Account routing:** `listAccessibleCustomers` plus a `customer_client` query on each accessible account
  builds the account list. Calls to an account under a manager send that manager as `login-customer-id`.
  Direct accounts use their own ID.
- **API access:** Google retired developer tokens in Sept 2026, and access levels now sit on the Google Cloud
  project. `GOOGLE_ADS_DEVELOPER_TOKEN` is optional and only sent if set.
- **Audit:** each tool call logs the user, tool and account to Cloud Logging (`zyppia.audit`).

## Develop locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Settings are in `.env.example`.
