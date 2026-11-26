#!/usr/bin/env bash
# Zyppia GMB connector: creates (or updates) a second, separate server for Google Business Profile
# and the free rank tracker. It runs the same code as Zyppia Google Ads with CONNECTOR=gmb, so it has
# only the GMB tools, its own address and its own sign-in. The Google Ads server is not touched.
#
# Run it in Google Cloud Shell (https://shell.cloud.google.com), signed in as the project owner:
#   bash gmb.sh              create the GMB server, or update it to the latest version of the code
#   bash gmb.sh --rollback   go back to the GMB server's previous version
set -euo pipefail

ADS_SERVICE="${ADS_SERVICE:-zyppia-ads-connector}"
SERVICE="${SERVICE:-zyppia-gmb-connector}"
REGION="${REGION:-us-central1}"
TOKEN_SECRET="zyppia-gmb-token-key"
PLACES_SECRET="zyppia-places-api-key"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mSTOPPED: %s\033[0m\n' "$*"; exit 1; }

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
if [ -z "$PROJECT" ] || [ "$PROJECT" = "(unset)" ]; then
  PROJECT="$(gcloud projects list --filter="name:'Zyppia Ads Connector'" --format='value(projectId)' --limit=1)"
fi
[ -n "$PROJECT" ] || fail "No Google Cloud project found. Run: gcloud config set project YOUR-PROJECT-ID"
gcloud config set project "$PROJECT" >/dev/null 2>&1
PN="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
SA="serviceAccount:${PN}-compute@developer.gserviceaccount.com"
say "Project: $PROJECT"

if [ "${1:-}" = "--rollback" ]; then
  PREV="$(gcloud run revisions list --service "$SERVICE" --region "$REGION" \
          --sort-by='~metadata.creationTimestamp' --format='value(metadata.name)' --limit=2 | sed -n 2p)"
  [ -n "$PREV" ] || fail "There's no earlier GMB version to go back to."
  gcloud run services update-traffic "$SERVICE" --region "$REGION" --to-revisions "$PREV=100" --quiet
  say "DONE. The GMB connector runs the earlier version. Google Ads was not touched."
  exit 0
fi

say "1/4 Reading the Google Ads server's settings (read only, nothing is changed there) ..."
ADS_JSON="$(gcloud run services describe "$ADS_SERVICE" --region "$REGION" --format=json)" \
  || fail "Couldn't find the $ADS_SERVICE server in $REGION."
ENV_FILE="$(mktemp)"
CLIENT_SECRET="$(printf '%s' "$ADS_JSON" | python3 -c '
import json, sys
svc = json.load(sys.stdin)
c = svc["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e for e in c.get("env", [])}
out = {k: env[k]["value"] for k in ("GOOGLE_OAUTH_CLIENT_ID", "ALLOWED_EMAILS", "READ_ONLY_EMAILS") if "value" in env.get(k, {})}
with open(sys.argv[1], "w") as f:
    json.dump(out, f)
with open(sys.argv[1] + ".image", "w") as f:
    f.write(c["image"])
print(env.get("GOOGLE_OAUTH_CLIENT_SECRET", {}).get("valueFrom", {}).get("secretKeyRef", {}).get("name", ""))
' "$ENV_FILE")"
IMAGE="$(cat "$ENV_FILE.image")"
[ -n "$CLIENT_SECRET" ] || fail "The Ads server has no GOOGLE_OAUTH_CLIENT_SECRET secret."
URL="https://${SERVICE}-${PN}.${REGION}.run.app"
python3 - "$ENV_FILE" "$URL" <<'PY'
import json, sys
p, url = sys.argv[1], sys.argv[2]
env = json.load(open(p))
env.update({"CONNECTOR": "gmb", "PUBLIC_URL": url})
with open(p, "w") as f:
    for k, v in env.items():
        f.write(f"{k}: {json.dumps(v)}\n")
PY
echo "OK (same Google sign-in client and allowed emails as Google Ads)"

say "2/4 Making the GMB server's own sign-in key (safe to repeat) ..."
if ! gcloud secrets describe "$TOKEN_SECRET" >/dev/null 2>&1; then
  python3 -c "import secrets;print(secrets.token_urlsafe(48),end='')" | gcloud secrets create "$TOKEN_SECRET" --data-file=- >/dev/null
fi
gcloud secrets add-iam-policy-binding "$TOKEN_SECRET" --member="$SA" --role=roles/secretmanager.secretAccessor >/dev/null
SECRETS="GOOGLE_OAUTH_CLIENT_SECRET=${CLIENT_SECRET}:latest,TOKEN_KEY=${TOKEN_SECRET}:latest"
if gcloud secrets describe "$PLACES_SECRET" >/dev/null 2>&1; then
  SECRETS="${SECRETS},PLACES_API_KEY=${PLACES_SECRET}:latest"
else
  echo "Note: no $PLACES_SECRET secret yet, so the rank tracker stays off until you create it (DEPLOY.md, 6c)."
fi
echo "OK"

say "3/4 Starting the GMB server from the same build as Google Ads (1 to 2 minutes) ..."
gcloud run deploy "$SERVICE" --image "$IMAGE" --region "$REGION" --allow-unauthenticated \
  --env-vars-file "$ENV_FILE" --set-secrets "$SECRETS" --min-instances 0 --max-instances 2 --quiet
rm -f "$ENV_FILE" "$ENV_FILE.image"

say "4/4 Checking it answers ..."
for i in 1 2 3 4 5 6; do
  if curl -fsS "$URL/healthz" | grep -q '"ok"'; then
    say "DONE. The GMB connector is running. Google Ads was not touched."
    echo
    echo "If this is the first time, finish with two things:"
    echo " a) Add this redirect URI to your OAuth client (Google Auth Platform > Clients > your client > Authorized redirect URIs):"
    echo "      $URL/oauth/google/callback"
    echo "    https://console.cloud.google.com/auth/clients?project=$PROJECT"
    echo " b) In Claude: Settings > Connectors > Add custom connector. Name: Zyppia GMB  URL:"
    echo "      $URL/mcp"
    exit 0
  fi
  sleep 10
done
fail "The GMB server didn't answer at $URL/healthz. Google Ads is not affected. Send a screenshot of this to Claude."
