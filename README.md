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

### Phase 5: Performance Max, Display and remarketing

| Tool | What it does |
| --- | --- |
| `create_pmax_campaign` | A Performance Max campaign (Search, YouTube, Display, Gmail, Discover, Maps) with budget, conversion bidding, locations, languages and a first asset group: headlines, long headlines, descriptions, business name, images, logos, YouTube videos, search themes and an audience signal. With `merchant_center_id` it also advertises the Merchant Center products. Always created **paused** |
| `list_asset_groups` | Every asset group with ad strength, each asset and its ID, signals, product listing groups, recent performance and what to improve |
| `create_asset_group` / `update_asset_group` | Add an asset group; rename, pause, change the final URL, add or remove assets |
| `set_pmax_signals` | Add or remove search themes, replace or remove the audience signal |
| `set_pmax_listing_groups` | Choose which products an asset group advertises: all, only some brands / item IDs / product types / custom labels, or all except some |
| `create_display_campaign` | A Display campaign with an ad group, targeting (remarketing lists, interests, custom audiences, keywords, placements, topics), excluded placements and a responsive display ad. Always created **paused** |
| `create_display_ad_group` / `create_responsive_display_ad` / `update_display_targeting` | More Display ad groups, ads and targeting changes, including optimized targeting |
| `list_audiences` | Remarketing lists with their size for Display and Search, whether ads can use them yet, and where they're used |
| `find_audience_segments` | Find interest, in-market, affinity, topic and custom audience IDs by name |
| `create_remarketing_list` / `update_remarketing_list` | Website-visitor lists from page rules (visited X but not Y), how long people stay, prefill with the last 30 days |
| `create_remarketing_campaign` | A Display campaign that only shows ads to people on the given remarketing lists |
| `update_audience_targeting` | Add audiences to Search or Display campaigns and ad groups as observation (with a bid adjustment), targeting, or exclusion |

Images are public `https://` addresses of JPEG, PNG or GIF files (5 MB at most), or IDs of image assets already in
the account. The connector downloads each one (public addresses only), checks its shape and size, and lets Google
check the whole change. The confirm token keeps only each image's address and fingerprint: `apply_change` downloads
it again and refuses if the file changed since the preview.

Things to set up by hand: a Merchant Center account linked to the Google Ads account (for Shopping products in
Performance Max), the Google tag on the website (so remarketing lists fill up), and working conversion tracking
(Performance Max and conversion bidding need it). Remarketing lists only work once they have 100+ active people for
Display and Performance Max, or 1,000+ for Search.

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
