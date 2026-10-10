#!/usr/bin/env bash
# Deploy to Google Cloud Run: PROJECT_ID=my-project ./deploy/cloudrun-deploy.sh
# Needs a Cloud project with billing and `gcloud auth login` done once (deploy/README.md).
# 2 vCPU / 4 GiB because merging a full season (about 10M rows) needs 1-2 GB of memory.
# The service scales to zero, and MAX_INSTANCES caps the worst-case bill.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-CHANGE-ME}"
SERVICE="${SERVICE:-noise-analysis-platform}"
REGION="${REGION:-us-central1}"

CPU=2
MEMORY=4Gi
REQUEST_TIMEOUT=3600                 # Cloud Run maximum
CONCURRENCY=2                        # one gunicorn worker x two threads
MAX_INSTANCES=2
MIN_INSTANCES=0

cd "$(dirname "$0")/.."

if [[ "$PROJECT_ID" == "CHANGE-ME" ]]; then
  echo "ERROR: set PROJECT_ID at the top of this script, or run:" >&2
  echo "       PROJECT_ID=your-project-id $0" >&2
  exit 1
fi

command -v gcloud >/dev/null || { echo "ERROR: gcloud not installed. See deploy/README.md." >&2; exit 1; }

# uploads are signed with SECRET_KEY; every instance must share one, so reuse the service's key
if [[ -z "${SECRET_KEY:-}" ]]; then
  SECRET_KEY=$(gcloud run services describe "$SERVICE" --region "$REGION" \
                 --format='value(spec.template.spec.containers[0].env.filter("name:SECRET_KEY").extract("value"))' \
                 2>/dev/null | tr -d "[]'" || true)
fi
if [[ -z "${SECRET_KEY:-}" ]]; then
  SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  echo "==> Generated a new SECRET_KEY (older uploads will have to be uploaded again)."
fi

echo "==> Project : $PROJECT_ID"
echo "==> Service : $SERVICE  ($REGION)"
echo "==> Shape   : ${CPU} vCPU / ${MEMORY} / concurrency ${CONCURRENCY} / max ${MAX_INSTANCES} instances"
echo

gcloud config set project "$PROJECT_ID" --quiet

# deploying from source needs all five APIs, including iam
echo "==> Enabling required APIs (no-op if already enabled)..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  cloudresourcemanager.googleapis.com \
  --quiet

echo "==> Deploying from source (build context is trimmed by .gcloudignore)..."
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --cpu "$CPU" \
  --memory "$MEMORY" \
  --timeout "$REQUEST_TIMEOUT" \
  --concurrency "$CONCURRENCY" \
  --max-instances "$MAX_INSTANCES" \
  --min-instances "$MIN_INSTANCES" \
  --set-env-vars "UPLOAD_FOLDER=/tmp/uploads,ARTIFACTS_DIR=/tmp/artifacts,SECRET_KEY=$SECRET_KEY" \
  --quiet

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo
echo "==> Live at: $URL"
echo "==> Logs   : gcloud run services logs tail $SERVICE --region $REGION"
