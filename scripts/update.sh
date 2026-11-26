#!/usr/bin/env bash
# Zyppia Google Ads connector: one command to update the live server.
#
# Run it in Google Cloud Shell (https://shell.cloud.google.com), signed in as the project owner:
#   bash update.sh              turn on every Google API the connector needs, rebuild from main, check it works
#   bash update.sh --apis-only  only turn on the Google APIs
#   bash update.sh --rollback   go back to the version that ran before (if an update broke something)
#
# Normally you don't need it for updates: merging a pull request into main rebuilds the server by itself.
set -euo pipefail

SERVICE="${SERVICE:-zyppia-ads-connector}"
REGION="${REGION:-us-central1}"
APIS=(
  googleads.googleapis.com          # Google Ads
  tagmanager.googleapis.com         # Tag Manager
  analyticsadmin.googleapis.com     # Google Analytics setup
  analyticsdata.googleapis.com      # Google Analytics reports
  searchconsole.googleapis.com      # Search Console
  merchantapi.googleapis.com        # Merchant Center
  run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com
)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mSTOPPED: %s\033[0m\n' "$*"; exit 1; }

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
if [ -z "$PROJECT" ] || [ "$PROJECT" = "(unset)" ]; then
  PROJECT="$(gcloud projects list --filter="name:'Zyppia Ads Connector'" --format='value(projectId)' --limit=1)"
fi
[ -n "$PROJECT" ] || fail "No Google Cloud project found. Run: gcloud config set project YOUR-PROJECT-ID"
gcloud config set project "$PROJECT" >/dev/null 2>&1
say "Project: $PROJECT"

if [ "${1:-}" = "--rollback" ]; then
  PREV="$(gcloud run revisions list --service "$SERVICE" --region "$REGION" \
          --sort-by='~metadata.creationTimestamp' --format='value(metadata.name)' --limit=2 | sed -n 2p)"
  [ -n "$PREV" ] || fail "There's no earlier version to go back to."
  say "Going back to $PREV ..."
  gcloud run services update-traffic "$SERVICE" --region "$REGION" --to-revisions "$PREV=100" --quiet
  say "DONE. The connector runs the earlier version. (The next update.sh run goes back to the newest.)"
  exit 0
fi

say "1/3 Turning on the Google APIs the connector needs (safe to repeat) ..."
gcloud services enable "${APIS[@]}"
echo "OK"
[ "${1:-}" = "--apis-only" ] && { say "DONE."; exit 0; }

say "2/3 Rebuilding the server from the latest main branch (3 to 6 minutes) ..."
TRIGGER="" ; TREGION=""
for R in "$REGION" global; do
  T="$(gcloud builds triggers list --region="$R" --format='value(name)' 2>/dev/null | grep -i -m1 -E 'zyppia|ads-connector' || true)"
  [ -z "$T" ] && T="$(gcloud builds triggers list --region="$R" --format='value(name)' 2>/dev/null | head -n1 || true)"
  if [ -n "$T" ]; then TRIGGER="$T"; TREGION="$R"; break; fi
done
if [ -n "$TRIGGER" ]; then
  BUILD="$(gcloud builds triggers run "$TRIGGER" --region="$TREGION" --branch=main --format='value(metadata.build.id)')"
  echo "Build $BUILD started (trigger $TRIGGER)."
  while :; do
    STATUS="$(gcloud builds describe "$BUILD" --region="$TREGION" --format='value(status)')"
    case "$STATUS" in
      SUCCESS) echo "Build finished."; break ;;
      FAILURE|INTERNAL_ERROR|TIMEOUT|CANCELLED|EXPIRED)
        fail "The build failed ($STATUS). The old version keeps running. Logs: https://console.cloud.google.com/cloud-build/builds;region=$TREGION/$BUILD?project=$PROJECT" ;;
      *) printf '.'; sleep 15 ;;
    esac
  done
elif [ -f Dockerfile ] && [ -d app ]; then
  echo "No Cloud Build trigger found, so deploying this folder instead."
  gcloud run deploy "$SERVICE" --source . --region "$REGION" --quiet
else
  fail "No Cloud Build trigger found. Run this script from inside the zyppia-ads-connector folder instead."
fi
gcloud run services update-traffic "$SERVICE" --region "$REGION" --to-latest --quiet >/dev/null

say "3/3 Checking the server answers ..."
URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
for i in 1 2 3 4 5 6; do
  if curl -fsS "$URL/healthz" | grep -q '"ok"'; then
    say "DONE. The connector is updated and working: $URL/mcp"
    echo "If new Google permissions were added, reconnect the connector in Claude (Settings > Connectors)."
    exit 0
  fi
  sleep 10
done
fail "The server didn't answer at $URL/healthz. Run 'bash update.sh --rollback' to go back to the earlier version."
