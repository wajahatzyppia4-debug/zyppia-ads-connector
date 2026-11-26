# Put the connector online: a step-by-step guide

Sign in as **wajahatzyppia4@gmail.com** for every step. It takes about 30 minutes.
If a screen looks different from what's described here, take a screenshot and send it to Claude.

You'll never paste a secret into a chat. Secrets only go into Google's own screens.

---

## Step 0. Merge the code

1. Open the pull request link Claude sent you on GitHub.
2. Scroll down and click the green **Merge pull request** button, then **Confirm merge**.

---

## Step 1. Finish the Google sign-in setup

All of this happens in the Google Cloud project **Zyppia Ads Connector**. Check that its name shows at the top of the page.

### 1a. Consent screen (skip any part you've already done)

1. Open <https://console.cloud.google.com/auth/overview>.
2. **Branding:** set the app name to `Zyppia Ads Connector` and enter your support email.
3. **Audience:** choose **External**, then click **Publish app** so the status says **In production**.
   *(If it stays on "Testing", everyone gets signed out every 7 days.)*
4. **Data access:** click **Add or remove scopes** and tick these:
   - `openid`
   - `.../auth/userinfo.email`
   - `.../auth/adwords` (Google Ads API). If it's not in the list, paste
     `https://www.googleapis.com/auth/adwords` into "Manually add scopes".
   - The three Tag Manager ones (see "Phase 4" below).
   Then click **Update**, then **Save**.

### 1b. OAuth client

1. Open <https://console.cloud.google.com/auth/clients> and click **Create client**.
2. **Application type:** choose **Web application**. **Name:** `Zyppia Ads Connector`.
3. Under **Authorized redirect URIs**, click **Add URI** and paste the address below.
   The number in the middle is your project number. Your Google email showed 1099251542590; if Step 2 prints a different number, use that one.

   ```
   https://zyppia-ads-connector-1099251542590.us-central1.run.app/oauth/google/callback
   ```
4. Click **Create**. A box shows a **Client ID** and a **Client secret**.
   Keep this tab open, because you need both in the next steps.

---

## Step 2. Store the secrets (one copy and paste)

1. Open <https://console.cloud.google.com/> and click the **>_** icon (Activate Cloud Shell) at the top right.
   A black terminal opens at the bottom. If it asks to authorize, click **Authorize**.
2. **Billing must be on first.** If you see a blue "Start your Free Trial with $300" banner, click **Start free** and finish it.
3. Paste this one line and press **Enter**. Wait 1 to 2 minutes until it finishes and you see a line ending in `$` again:

```bash
gcloud services enable run.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
```

4. Then copy this whole block, paste it into the black terminal, and press **Enter**:

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(48),end='')" | gcloud secrets create zyppia-token-key --data-file=-
read -s -p "Paste the OAuth CLIENT SECRET here (it stays hidden), then press Enter: " S; echo
printf '%s' "$S" | gcloud secrets create zyppia-oauth-client-secret --data-file=-; unset S
PN=$(gcloud projects describe "$(gcloud config get-value project)" --format='value(projectNumber)')
for s in zyppia-token-key zyppia-oauth-client-secret; do
  gcloud secrets add-iam-policy-binding $s --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" --role=roles/secretmanager.secretAccessor >/dev/null
done
echo; echo "DONE. Your connector address will be: https://zyppia-ads-connector-${PN}.us-central1.run.app"
```

5. When it asks, paste the **Client secret** from Step 1b and press **Enter**. You won't see it as you paste, which is normal.
6. At the end it prints **DONE** and your connector address. If that address doesn't match the one in Step 1b,
   go back to the OAuth client and fix the redirect URI.

If it says "project not set", run `gcloud config set project YOUR-PROJECT-ID`
(you'll find the ID on the Cloud console home page), then paste the block again.

---

## Step 3. Create the server (Cloud Run)

1. Open <https://console.cloud.google.com/run> and click **Deploy container** (or **Create service**).
2. Choose **Continuously deploy from a repository** and click **Set up with Cloud Build**.
   - **Repository provider:** GitHub. Click **Authenticate** and sign in to GitHub.
   - If asked, click **Install Google Cloud Build** and give it access to **zyppia-ads-connector** only.
   - Pick the repository `wajahatzyppia4-debug/zyppia-ads-connector`, then **Next**.
   - **Branch:** `^main$`. **Build type:** **Dockerfile**, source location `/Dockerfile`. Click **Save**.
3. **Service name:** `zyppia-ads-connector`  **Region:** `us-central1 (Iowa)`.
4. **Authentication:** choose **Allow public access** (or "Allow unauthenticated invocations").
   This is safe: the connector does its own Google sign-in and lets in only your allowed emails.
5. **Billing:** "Request-based". **Minimum instances:** 0. **Maximum instances:** 2.
6. Open **Containers, Volumes, Networking, Security**, then the **Variables & Secrets** tab.
   Click **Add variable** for each of these:

   | Name | Value |
   | --- | --- |
   | `PUBLIC_URL` | `https://zyppia-ads-connector-1099251542590.us-central1.run.app` (the address from Step 2) |
   | `GOOGLE_OAUTH_CLIENT_ID` | the **Client ID** from Step 1b (ends in `.apps.googleusercontent.com`) |
   | `ALLOWED_EMAILS` | `wajahatzyppia4@gmail.com` (add teammates separated by commas) |
   | `MCC_ID` | `347-642-6639` |

   Then under **Secrets exposed as environment variables**, click **Reference a secret** twice:

   | Name | Secret | Version |
   | --- | --- | --- |
   | `GOOGLE_OAUTH_CLIENT_SECRET` | `zyppia-oauth-client-secret` | latest |
   | `TOKEN_KEY` | `zyppia-token-key` | latest |

7. Click **Create**. Building takes 3 to 6 minutes.
8. When it's done, the page shows a **URL** at the top. Check that it's the same address as in Step 2.
   Open that URL with `/healthz` added to the end. You should see `{"ok":true}`.

---

## Step 4. Add it to Claude

1. Go to <https://claude.ai>, then **Settings**, then **Connectors**, then **Add custom connector**.
2. **Name:** `Zyppia Google Ads`. **URL:** your address with `/mcp` added to the end, for example:
   `https://zyppia-ads-connector-1099251542590.us-central1.run.app/mcp`
3. Click **Add**, then **Connect**. A Google sign-in opens:
   - Choose **wajahatzyppia4@gmail.com**.
   - If Google says the app isn't verified, click **Advanced**, then **Go to Zyppia Ads Connector**.
     That's expected for a private company tool.
   - Tick the **Google Ads** permission and click **Continue**.
4. In a new Claude chat, open the tools menu and make sure **Zyppia Google Ads** is switched on.

### Try it

- "List my Google Ads accounts."
- "Show campaign performance for DXB - Travel And Tourism for the last 30 days."
- "Which search terms cost the most last week in DXB - Travel And Tourism?"
- "Give me keyword ideas for 'Dubai desert safari' in the UAE." (This works once Basic access is approved.)

---

## Later: adding teammates

Add their Google emails to `ALLOWED_EMAILS` in Cloud Run: **Edit & deploy new revision**, then **Variables & Secrets**.
Each person then adds the connector in their own Claude account and signs in with their own Google login.
They see only the Google Ads accounts that their login can access.

To let someone **only view reports** (no changes), also add their email to a variable named
`READ_ONLY_EMAILS` (commas between emails).

## Later: a spending limit per client

Add a variable named `SPEND_LIMITS`, for example `399-355-6480=100,123-456-7890=50`.
Claude then refuses any change that would make that account's running campaigns' daily budgets add up to more than
that amount (in the account's own currency).

## Phase 4: turn on Google Tag Manager

Do this once, after merging the Phase 4 pull request. Sign in as **wajahatzyppia4@gmail.com**.

1. **Turn on the Tag Manager API.** Open <https://console.cloud.google.com/apis/library/tagmanager.googleapis.com>,
   check the project at the top says **Zyppia Ads Connector**, and click **Enable**.
2. **Add the Tag Manager permissions.** Open <https://console.cloud.google.com/auth/scopes>, click
   **Add or remove scopes**, and paste these three lines into **Manually add scopes**, then click
   **Add to table**, **Update**, and **Save**:

   ```
   https://www.googleapis.com/auth/tagmanager.edit.containers
   https://www.googleapis.com/auth/tagmanager.edit.containerversions
   https://www.googleapis.com/auth/tagmanager.publish
   ```
3. **Wait for the update.** Cloud Run rebuilds by itself after the merge (3 to 6 minutes).
4. **Sign in again in Claude.** Go to **Settings**, then **Connectors**, then **Zyppia Google Ads**. Click
   **Disconnect**, then **Connect**. On the Google screen, tick **every** box (Google Ads and the Tag Manager ones)
   and click **Continue**.
5. **Give your login access to each client's container.** In Tag Manager (<https://tagmanager.google.com>), each
   container needs your Google login under **Admin**, then **User Management**, with **Publish** permission.
   (If the client owns the container, ask them to add **wajahatzyppia4@gmail.com** with Publish.)

### Try it

- "List my Tag Manager containers."
- "Show the conversion actions in DXB - Travel And Tourism and tell me what's wrong."
- "Set up a 'Contact form' conversion for DXB - Travel And Tourism in GTM-XXXXXX that counts when the page URL
  contains /thank-you."

Nothing goes live on a website until you agree to publish.

## Phase 5: turn on Google Analytics, Search Console and Merchant Center

Do this once, after merging the Phase 5 pull request. Sign in as **wajahatzyppia4@gmail.com**.

1. **Turn on the new Google APIs.** Open Cloud Shell (<https://shell.cloud.google.com>), click the three-dot menu,
   **Upload**, and choose `update.sh` (it's in the project files, and in this repository under `scripts/`). Then type
   `bash update.sh` and press **Enter**. It turns on every API the connector needs, rebuilds the server, and prints
   **DONE** when it works (about 5 minutes).

   Or turn them on by hand: open each link, check the project at the top says **Zyppia Ads Connector**, click **Enable**:
   - <https://console.cloud.google.com/apis/library/analyticsadmin.googleapis.com>
   - <https://console.cloud.google.com/apis/library/analyticsdata.googleapis.com>
   - <https://console.cloud.google.com/apis/library/searchconsole.googleapis.com>
   - <https://console.cloud.google.com/apis/library/merchantapi.googleapis.com>
2. **Add the new permissions.** Open <https://console.cloud.google.com/auth/scopes>, click **Add or remove scopes**,
   paste these four lines into **Manually add scopes**, then click **Add to table**, **Update**, and **Save**:

   ```
   https://www.googleapis.com/auth/analytics.edit
   https://www.googleapis.com/auth/analytics.readonly
   https://www.googleapis.com/auth/webmasters.readonly
   https://www.googleapis.com/auth/content
   ```
3. **Sign in again in Claude.** **Settings**, **Connectors**, **Zyppia Google Ads**: **Disconnect**, then **Connect**.
   On the Google screen tick **every** box and click **Continue**.
4. **Access to client accounts.** Your Google login needs to be a user on each client's GA4 property (Editor, to set
   things up), Search Console site (any level), and Merchant Center account (Admin or Standard).
5. **Merchant Center only, the first time:** if Claude says the project isn't registered, say "register the project
   with Merchant Center account 123" and confirm.

### Try it

- "List my Google Analytics properties and tell me what's missing in DXB Travel."
- "Check the tags on https://client.com and tell me if anything is doubled."
- "Write the Tag Manager code for GTM-XXXXXX for a WordPress developer."
- "What are people searching on Google to find client.com? Which searches should we bid on?"
- "Which products in Merchant Center account 123 are disapproved, and why?"

## Phase 6: turn on Google Business Profile and the free rank tracker

Do this once, after merging the Phase 6 pull request. Sign in as **wajahatzyppia4@gmail.com**.
Nothing here costs money: the Business Profile APIs are free, and the rank tracker only uses Google's free
"IDs only" searches plus a small part of a free monthly allowance.

### 6a. Ask Google for Business Profile API access (free, one time)

Google keeps these APIs at zero use until it approves the project. Approval usually takes a few days to two weeks.

1. Open <https://support.google.com/business/contact/api_default>.
2. Choose **Application for Basic API Access**.
3. Fill it in: your email **wajahatzyppia4@gmail.com**, company **Zyppia**, website, and the project number
   **1099251542590** (Cloud console home page shows it). Say what it's for, for example:
   *"Agency tool to manage our clients' Business Profiles: reply to reviews, publish posts, update hours and read
   performance insights, for profiles where we are Manager."*
4. Google checks that your Google login manages at least one verified profile that's been live for a while and has
   a website. Submit, then wait for Google's email.

You can do 6b to 6d while you wait. Until approval, the Business Profile tools say the quota is 0, and the rank
tracker already works.

### 6b. Turn on the APIs and add the permission

1. In Cloud Shell, run `bash update.sh` (upload the new copy from `scripts/` first). It turns on Places API (New) and
   the Business Profile APIs. A note that a Business Profile API "isn't available yet" is normal before approval:
   run `bash update.sh --apis-only` again after Google's email.
2. Open <https://console.cloud.google.com/auth/scopes>, click **Add or remove scopes**, paste this line into
   **Manually add scopes**, then click **Add to table**, **Update**, and **Save**:

   ```
   https://www.googleapis.com/auth/business.manage
   ```

### 6c. Create the rank tracker's API key (one copy and paste)

Paste this whole block into Cloud Shell and press **Enter**. It creates a key that can only call Places API (New),
stores it as a secret, and gives it to the server:

```bash
gcloud services enable places.googleapis.com apikeys.googleapis.com
gcloud services api-keys create --display-name="Zyppia rank tracker" --api-target=service=places.googleapis.com
KN=$(gcloud services api-keys list --filter="displayName='Zyppia rank tracker'" --format='value(name)' --limit=1)
gcloud services api-keys get-key-string "$KN" --format='value(keyString)' | tr -d '\n' | gcloud secrets create zyppia-places-api-key --data-file=-
PN=$(gcloud projects describe "$(gcloud config get-value project)" --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding zyppia-places-api-key --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" --role=roles/secretmanager.secretAccessor >/dev/null
gcloud run services update zyppia-ads-connector --region us-central1 --update-secrets=PLACES_API_KEY=zyppia-places-api-key:latest
echo DONE
```

### 6d. A safety net so the bill stays at zero

1. Open <https://console.cloud.google.com/billing/budgets>, click **Create budget**, pick this project, set the amount
   to **1** (your currency) and keep the email alerts on. You'll get an email if anything ever costs money.
2. Grid searches are free without limit. Competitor names (up to 15 per scan) and `rank_find_business` come out of
   Google's 5,000 free calls a month, which is roughly 300+ scans a month.

### 6e. Create the separate Zyppia GMB connector

The Business Profile and rank tracker tools live in their own connector, **Zyppia GMB**, so Zyppia Google Ads stays
exactly as it is. It's a second server running the same code with its own address and sign-in.

1. In Cloud Shell, download and run the script (it only reads the Google Ads server's settings, never changes it):

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/wajahatzyppia4-debug/zyppia-ads-connector/main/scripts/gmb.sh && bash gmb.sh
   ```

2. At the end it prints a **redirect URI**. Open <https://console.cloud.google.com/auth/clients>, click your OAuth
   client, under **Authorized redirect URIs** click **Add URI**, paste it, and click **Save**. Keep the existing
   Google Ads URI there.
3. In Claude: **Settings**, **Connectors**, **Add custom connector**. **Name:** `Zyppia GMB`. **URL:** the address
   ending in `/mcp` that the script printed. Click **Add**, then **Connect**, and tick the **Business Profile**
   permission. Your login must be an **Owner or Manager** of each client's profile
   (business.google.com > the profile > **Business Profile settings** > **People and access**).

To update the GMB server later, run `bash update.sh` (it updates both servers) or `bash gmb.sh` again.
`bash gmb.sh --rollback` takes only the GMB server back a version.

### Try it

- "List my Business Profiles."
- "Audit the Business Profile of Marina Plumbing and tell me what to fix first."
- "Show unanswered reviews for Marina Plumbing and draft replies."
- "Write a post for this week's offer with a Book button."
- "How did Marina Plumbing's profile do last month? Which searches found it?"
- "Run a 7x7 rank grid for 'plumber' and 'emergency plumber' around Marina Plumbing, 1 km apart, and show it as a heatmap."
- "Find 'Al Noor Dental Clinic Deira' on Maps and run a 5x5 grid for 'dentist' (for a prospect pitch)."

## Updates

Every time a pull request is merged into `main`, Cloud Run rebuilds and updates the Google Ads server automatically.
The separate GMB server updates when you run `bash update.sh` (or `bash gmb.sh`).

To update by hand (or after a failed automatic update), run `bash update.sh` in Cloud Shell. If an update ever
breaks the connector, `bash update.sh --rollback` goes back to the version before.
