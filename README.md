# Zyppia Google Ads connector

A private MCP server that lets Claude work in Zyppia's Google Ads accounts in plain conversation.
It covers client accounts under the Zyppia MCC (347-642-6639) and any account the signed-in person's
Google login was added to directly.

**Setup:** see [docs/DEPLOY.md](docs/DEPLOY.md) for a step-by-step guide.

## What it can do

### Reading (phase 1)

| Tool | What it does |
| --- | --- |
| `list_accounts` | Every account you can access, with currency and whether it's via the MCC or direct |
| `performance_report` | Account, campaign, ad group, keyword, search term or ad performance for a date range (rows include the IDs the change tools need) |
| `run_gaql` | Any Google Ads Query Language report |
| `change_history` | Who changed what in the last 29 days |
| `find_locations` | Look up location IDs from place names |
| `keyword_ideas` | Keyword Planner ideas, volumes, competition and bid ranges (needs Basic API access) |

### Changing (phase 2)

Every change tool only **previews**: Google checks the change (`validate_only`) and nothing is modified.
Claude shows the person what will change, and only after a yes calls `apply_change` with the preview's
confirm token.

| Tool | What it does |
| --- | --- |
| `set_status` | Pause or enable campaigns, ad groups, ads, keywords |
| `remove_items` | Remove them for good (campaigns and ad groups need their exact names typed) |
| `update_budget` | Change a campaign's daily budget |
| `update_bids` | Change ad group or keyword max CPC bids |
| `set_bidding_strategy` | Manual CPC, Maximize clicks, Maximize conversions (target CPA), Maximize conversion value (target ROAS) |
| `add_keywords` / `add_negative_keywords` | Add keywords to an ad group, or negatives to a campaign or ad group |
| `create_search_campaign` | Budget, bidding, locations, languages, and optionally an ad group with keywords and an ad, in one step. Always created **paused** |
| `create_ad_group` / `create_responsive_search_ad` | Add ad groups and responsive search ads |
| `update_campaign_settings` / `update_campaign_targeting` | Name, end date, networks; locations and languages |
| `advanced_change` | Any other Google Ads API operation (assets, schedules, labels, and so on) |
| `apply_change` | Make a previewed change |

### Phase 3

| Tool | What it does |
| --- | --- |
| `account_health_check` | Audits an account and returns a 0-100 score plus ranked issues: tracking, budget-limited campaigns, wasted search terms, low Quality Score, disapproved or weak ads, ad groups with no ads, missing extensions, Display network on Search. Each issue names the change tool that fixes it |
| `website_to_campaign_plan` | Reads a client's website (public addresses only) and gets Keyword Planner ideas for it, so Claude can draft a full campaign |
| `add_sitelinks` / `add_callouts` / `add_call_button` / `add_structured_snippet` | Extensions for a campaign or the whole account |
| `set_ad_schedule` | When ads run, with optional bid changes by time |
| `set_bid_adjustments` | Bid more or less by device or targeted location |
| `undo_change` | Reverse an applied change within 7 days (changed values go back, created items are removed) |
| `apply_changes` | Apply several previews after one yes, e.g. the same fix across many accounts |

### Phase 4: conversion tracking and Google Tag Manager

| Tool | What it does |
| --- | --- |
| `list_conversion_actions` | Every conversion action with type, category, primary/secondary, counting, value, lookback, attribution, conversions in the last 30 days, its Tag Manager conversion ID and label, and likely problems (no conversions, leads counted many times, duplicate primaries) |
| `create_conversion_action` | Website, phone call (from ads, from website, number taps) or imported (offline clicks/calls) conversion actions |
| `update_conversion_action` | Fix name, status (or HIDDEN), category, primary/secondary, counting, value, currency, lookback, call length, attribution. Can be undone |
| `remove_conversion_action` | Remove for good (exact names typed, extra confirmation) |
| `gtm_list_containers` | Every Tag Manager account and container the login can access (GTM-... IDs, domains) |
| `gtm_workspace` | Tags, triggers, variables, built-in variables, unpublished changes, live version, and problems (no Conversion Linker, tags that never fire, missing labels) |
| `gtm_get` | Read any Tag Manager item in full |
| `gtm_tag` / `gtm_trigger` / `gtm_variable` | Create, change or delete tags (Google Ads conversion, Conversion Linker, Google tag, remarketing, custom HTML, any type), triggers (page views, custom events, clicks, link clicks, forms, ...) and variables (dataLayer, URL, cookie, DOM, JavaScript, ...). Built-in variables a trigger needs (Click URL, Form ID, ...) are turned on automatically |
| `gtm_publish` | Save a version of the workspace and put it live (extra confirmation; undo republishes the version that was live before) |
| `gtm_advanced_change` | Any other Tag Manager API call (workspaces, folders, templates, environments, ...), extra confirmation |
| `setup_conversion_tracking` | One step: create the Google Ads conversion action (or use an existing one) **and** the GTM trigger, Google Ads conversion tag with the right ID and label, Conversion Linker and Google tag if missing, optionally publish. One preview, one yes, one undo |

Tag Manager has no "check only" mode, so its previews are checked by the connector (container, workspace, names,
trigger and variable references) and the exact API calls are sealed into the confirm token. Changes go into a
workspace and reach the website only when it's published, which always needs extra confirmation. Publishing,
deleting, custom HTML/JavaScript and raw API calls are flagged. If the Google Ads part of a combined change works
and a Tag Manager step fails, the result says "partly done" and the undo token reverses what was made.

### Phase 5: Google Analytics, website tags, Search Console, Merchant Center

| Tool | What it does |
| --- | --- |
| `ga4_list_properties` / `ga4_property` | GA4 accounts and properties; one property's web streams (G-... IDs), key events, Google Ads links and what's missing |
| `ga4_report` | Any GA4 metrics by any dimensions for a date range |
| `ga4_check_event` | Live: which events arrived in the last 30 minutes (did the test conversion arrive?) |
| `ga4_create_property` | New property plus its web data stream (undo deletes it; GA4 keeps it in the trash 35 days) |
| `ga4_key_event` | Mark an event as a key event (conversion) or remove one |
| `ga4_link_google_ads` | Link or unlink a GA4 property and a Google Ads account |
| `ga4_advanced_change` | Any other GA4 Admin API call (extra confirmation) |
| `tag_install_code` | The exact Tag Manager / Google tag code, where to paste it (WordPress, Shopify, Wix, ...) and a message for the developer |
| `check_site_tags` | Reads a live site: which GTM-, G- and AW- tags are there, doubled tags (also code that duplicates a tag inside the site's GTM container), missing IDs, old Universal Analytics, consent mode, platform |
| `gsc_list_sites` / `gsc_search_queries` | Real Google searches bringing visitors (by query, page, country, device, date), compared with the period before, plus keyword opportunities |
| `gsc_inspect_url` / `gsc_sitemaps` | Is a page on Google; submitted sitemaps and errors |
| `merchant_list_accounts` / `merchant_products` | Merchant Center accounts; products with approval status, problems and Google's fixes |
| `merchant_report` | Product clicks, impressions and conversions (or any Merchant report query) |
| `merchant_product_override` | Change a product's title, price, availability, labels, pause its ads... through a "Zyppia overrides" supplemental data source, so the shop's feed is untouched. Undo removes the override |
| `merchant_register_project` / `merchant_advanced_change` | One-time Merchant API registration; any other Merchant API call (extra confirmation) |

These APIs also have no "check only" mode, so (like Tag Manager) previews are checked by the connector and the exact
API calls are sealed into the confirm token. Search Console is read-only.

`scripts/update.sh` (run in Cloud Shell) turns on every Google API the connector needs, rebuilds the server from
`main`, and checks it answers. `--rollback` goes back to the previous version.

Safety rules:

- Confirm tokens expire after 15 minutes, work once, and only for the person who made the preview.
- These need an extra, explicit yes (`confirm_large_change`): budget or bid increases over
  `MAX_INCREASE_PCT` (default 50%), turning anything on, removals, removing every location, and `advanced_change`.
- People in `READ_ONLY_EMAILS` can use reports but can't change anything.
- `SPEND_LIMITS` (for example `399-355-6480=100`) caps an account's total daily budget across running campaigns.
  Changes that would go over are refused.
- Removals can't be undone, so the undo token doesn't cover them.
- Every preview and applied change is logged to Cloud Logging (`zyppia.audit`) with the user and the details.

## How it works

- **Sign-in:** Claude connects using OAuth. The server sends you to "Sign in with Google", which asks for
  your email, Google Ads access and Tag Manager access (edit containers, save versions, publish), and lets in only emails listed in `ALLOWED_EMAILS`.
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
