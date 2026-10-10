# Deploying to Cloud Run

One-time setup:

1. Create a Google Cloud project and attach billing. Cloud Run needs a card on
   file even though low traffic stays inside the free tier.
2. Install the gcloud CLI and run `gcloud auth login`.

Deploy:

    PROJECT_ID=your-project-id ./deploy/cloudrun-deploy.sh

The first build takes 5 to 10 minutes because the image includes the
Chromium libraries used for chart export. Later deploys are faster. The script
prints the service URL when it finishes.

Useful commands:

    gcloud run services logs tail noise-analysis-platform --region us-central1
    gcloud run services delete noise-analysis-platform --region us-central1

Notes:

- The service scales to zero, so the first request after a quiet period waits
  10 to 30 seconds for a cold start.
- Uploads and generated reports live in `/tmp` inside the instance and disappear
  when it scales down.
- The site is public with no login. Do not upload real participant data to it;
  `tools/make_demo_dataset.py` makes a synthetic file for testing.
