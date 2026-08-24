# Cloud Run — what's done, and the 4 things only you can do

## Already done for you

- `Dockerfile` now binds `${PORT:-7860}` — works on Cloud Run *and* still on
  HuggingFace Spaces, so you are not locked to either.
- `.gcloudignore` trims the upload from **3.5 GB → 0.9 MB** (28 files). Verified
  that `Dockerfile`, `requirements.txt`, `backend/` and `frontend/` all survive
  and the 879 MB venv, 2 GB of raw logger data and 399 MB of artifacts do not.
- `deploy/cloudrun-deploy.sh` — enables the APIs and deploys with the right
  shape (2 vCPU, 4 GiB, 60-min timeout, scale-to-zero, instance cap).
- **gcloud CLI installed** (v578.0.0, already on your PATH).

## The 4 things I cannot do

Each one needs either a browser sign-in or your credit card.

---

### STEP 1 — Create a Google Cloud project

Go to **https://console.cloud.google.com/projectcreate**

| Field | What to put |
|---|---|
| Project name | `noise-analysis-platform` |
| Project ID | auto-fills — **copy this exact string**, you need it in Step 4 |
| Location | leave as "No organization" |

Click **CREATE**. Wait ~30 seconds.

> The Project **ID** is not the same as the name. It usually looks like
> `noise-analysis-platform-473921`. That ID is what Step 4 needs.

---

### STEP 2 — Attach billing (card required, but you will not be charged)

Go to **https://console.cloud.google.com/billing**

1. Click **ADD BILLING ACCOUNT** (or **MANAGE BILLING ACCOUNTS → CREATE ACCOUNT**)
2. Select your country, accept the terms
3. Enter your card details
4. Back on the Billing page: **My Projects** tab → find `noise-analysis-platform`
   → **⋮** → **Change billing** → select the account you just made → **SET ACCOUNT**

**Why a card is needed even though it's free:** Google requires one to enable the
Cloud Run API at all. The always-free tier is 2 M requests, 180,000 vCPU-seconds
and 360,000 GiB-seconds per month. At the shape we deploy, that is roughly **25
hours of active request time per month** — you will not get near it with 1–3
occasional users, because the service bills nothing while idle.

**Set a safety net anyway:** https://console.cloud.google.com/billing/budgets
→ **CREATE BUDGET** → name `noise-cap`, a small amount in your own currency (the
equivalent of ~$1), tick *"alert at 50 / 90 / 100%"*. This emails you; it does
not auto-stop. The `--max-instances 2` in the deploy script is the real cap.

---

### STEP 3 — Sign in to gcloud

In your terminal:

```
gcloud auth login
```

A browser window opens → pick your Google account → **Allow**. Done.

---

### STEP 4 — Deploy

Replace `<YOUR-PROJECT-ID>` with the ID you copied in Step 1:

```
cd /Users/chiku/PROJECTS/noise-analysis-platform
PROJECT_ID=<YOUR-PROJECT-ID> ./deploy/cloudrun-deploy.sh
```

First run takes **5–10 minutes** (it builds the ~1.5 GB image with the Chromium
libraries). Later deploys are 1–2 minutes because layers are cached.

When it finishes it prints:

```
==> Live at: https://noise-analysis-platform-XXXXXXXX-uc.a.run.app
```

That URL is your LinkedIn link.

---

## Afterwards

| Task | Command |
|---|---|
| Redeploy after code changes | `PROJECT_ID=<id> ./deploy/cloudrun-deploy.sh` |
| Watch logs | `gcloud run services logs tail noise-analysis-platform --region us-central1` |
| Check spend | https://console.cloud.google.com/billing |
| Take it offline | `gcloud run services delete noise-analysis-platform --region us-central1` |

## Things worth knowing

- **Cold start ~10–30 s.** The service scales to zero, so the first request after
  an idle period waits for the container to boot. Normal for free hosting.
- **The filesystem is in-memory.** Uploads and generated reports live in `/tmp`
  and count against the 4 GiB. They vanish when the instance scales down, which
  is fine — the app treats artifacts as per-session anyway.
- **Do not upload real participant data to it.** Use
  `tools/make_demo_dataset.py` to generate a synthetic file. The platform is
  public and unauthenticated.
- **Region is `us-central1`**, chosen because it is free-tier eligible. Closer
  regions exist but not all are Tier 1.
