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
4. **Data access:** click **Add or remove scopes** and tick these three:
   - `openid`
   - `.../auth/userinfo.email`
   - `.../auth/adwords` (Google Ads API). If it's not in the list, paste
     `https://www.googleapis.com/auth/adwords` into "Manually add scopes".
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
2. Copy this whole block, paste it into the black terminal, and press **Enter**:

```bash
gcloud services enable run.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
python3 -c "import secrets;print(secrets.token_urlsafe(48),end='')" | gcloud secrets create zyppia-token-key --data-file=-
read -s -p "Paste the OAuth CLIENT SECRET here (it stays hidden), then press Enter: " S; echo
printf '%s' "$S" | gcloud secrets create zyppia-oauth-client-secret --data-file=-; unset S
PN=$(gcloud projects describe "$(gcloud config get-value project)" --format='value(projectNumber)')
for s in zyppia-token-key zyppia-oauth-client-secret; do
  gcloud secrets add-iam-policy-binding $s --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" --role=roles/secretmanager.secretAccessor >/dev/null
done
echo; echo "DONE. Your connector address will be: https://zyppia-ads-connector-${PN}.us-central1.run.app"
```

3. When it asks, paste the **Client secret** from Step 1b and press **Enter**. You won't see it as you paste, which is normal.
4. At the end it prints **DONE** and your connector address. If that address doesn't match the one in Step 1b,
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

## Updates

Every time a pull request is merged into `main`, Cloud Run rebuilds and updates the server automatically.
