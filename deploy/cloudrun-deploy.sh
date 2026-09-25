#!/usr/bin/env bash
#
# Deploy the noise-analysis platform to Google Cloud Run.
#
# Run from anywhere:  ./deploy/cloudrun-deploy.sh
#
# Prerequisites you must have completed once (see deploy/CLOUD-RUN-SETUP.md):
#   1. a Google Cloud project exists and billing is attached
#   2. `gcloud` is installed and `gcloud auth login` has been run
#   3. PROJECT_ID below matches your project
#
# Sizing rationale
# ----------------
# A full-season merge (18 files, ~10.7M rows) needs ~1-2 GB of pandas working
# set and takes ~72 s of CPU, so 512 MB / 0.1 vCPU tiers cannot run it. 2 vCPU
# and 4 GiB clear it with headroom.
#
# Cost control: the service scales to zero, so nothing is billed while idle.
# Free tier is 180,000 vCPU-seconds and 360,000 GiB-seconds per month; at this
# shape that is ~25 hours of *active request time* per month. MAX_INSTANCES caps
# the worst case so a traffic spike cannot produce a surprise bill.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-CHANGE-ME}"
SERVICE="${SERVICE:-noise-analysis-platform}"
REGION="${REGION:-us-central1}"      # Tier-1 region, free-tier eligible

CPU=2
MEMORY=4Gi
REQUEST_TIMEOUT=3600                 # Cloud Run max; gunicorn's own is 600 s
CONCURRENCY=2                        # matches gunicorn: 1 worker x 2 threads
MAX_INSTANCES=2
MIN_INSTANCES=0                      # scale to zero => no idle cost

cd "$(dirname "$0")/.."

if [[ "$PROJECT_ID" == "CHANGE-ME" ]]; then
  echo "ERROR: set PROJECT_ID at the top of this script, or run:" >&2
  echo "       PROJECT_ID=your-project-id $0" >&2
  exit 1
fi

command -v gcloud >/dev/null || { echo "ERROR: gcloud not installed. See deploy/CLOUD-RUN-SETUP.md step 3." >&2; exit 1; }

# Upload references are signed with SECRET_KEY and checked on every request. A
# per-instance random key would reject a reference minted by another instance,
# so the key is set once for the service and reused across revisions.
if [[ -z "${SECRET_KEY:-}" ]]; then
  SECRET_KEY=$(gcloud run services describe "$SERVICE" --region "$REGION" \
                 --format='value(spec.template.spec.containers[0].env.filter("name:SECRET_KEY").extract("value"))' \
                 2>/dev/null | tr -d "[]'" || true)
fi
if [[ -z "${SECRET_KEY:-}" ]]; then
  SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  echo "==> Generated a new SECRET_KEY for this service (uploads from older revisions will need re-uploading)."
fi

echo "==> Project : $PROJECT_ID"
echo "==> Service : $SERVICE  ($REGION)"
echo "==> Shape   : ${CPU} vCPU / ${MEMORY} / concurrency ${CONCURRENCY} / max ${MAX_INSTANCES} instances"
echo

gcloud config set project "$PROJECT_ID" --quiet

# `run deploy --source` needs all five: Cloud Build compiles the image,
# Artifact Registry stores it, and IAM is required to mint the build and
# runtime service accounts on a project that has never used them. Omitting
# iam.googleapis.com fails with SERVICE_DISABLED partway through the deploy.
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
