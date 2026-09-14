#!/usr/bin/env bash
# Read-only default plan; explicit apply creates/reuses narrowly scoped IAM resources.
set -euo pipefail
mode="${1:---plan}"
if [[ "$mode" != "--plan" && "$mode" != "--apply" ]]; then
  echo 'Usage: GCP_PROJECT_ID=... RUNTIME_REPO=balajik10/career-command-center-runtime ./scripts/bootstrap_gcp_wif.sh --plan|--apply' >&2
  exit 2
fi
project="${GCP_PROJECT_ID:-}"
runtime_repo="${RUNTIME_REPO:-balajik10/career-command-center-runtime}"
if [[ ! "$project" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
  echo 'SETUP_REQUIRED: supply a valid GCP_PROJECT_ID' >&2
  exit 2
fi
if [[ ! "$runtime_repo" =~ ^balajik10/career-command-center-runtime(-[a-z0-9-]+)?$ ]]; then
  echo 'Expected the exact private runtime repository under balajik10' >&2
  exit 2
fi
pool="career-radar-github"
provider="private-runtime-main"
service="career-radar-sheets"
account="$service@$project.iam.gserviceaccount.com"
condition="assertion.repository == '$runtime_repo' && assertion.ref == 'refs/heads/main'"
if [[ "$mode" == "--plan" ]]; then
  printf 'Plan: create/reuse pool %s, provider %s, and Sheets service identity %s.\n' "$pool" "$provider" "$account"
  printf 'Trust only %s at refs/heads/main. Share only the target Sheet with this service identity. No API keys or IAM deletion.\n' "$runtime_repo"
  exit 0
fi
command -v gcloud >/dev/null || { echo 'SETUP_REQUIRED: install/authenticate gcloud' >&2; exit 2; }
project_number="$(gcloud projects describe "$project" --format='value(projectNumber)')"
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com sheets.googleapis.com --project="$project" --quiet >/dev/null
if ! gcloud iam service-accounts describe "$account" --project="$project" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$service" --project="$project" --display-name='Career Radar Sheets only' --quiet >/dev/null
fi
if ! gcloud iam workload-identity-pools describe "$pool" --location=global --project="$project" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$pool" --location=global --project="$project" --display-name='Career Radar GitHub' --quiet >/dev/null
fi
if gcloud iam workload-identity-pools providers describe "$provider" --workload-identity-pool="$pool" --location=global --project="$project" >/dev/null 2>&1; then
  existing="$(gcloud iam workload-identity-pools providers describe "$provider" --workload-identity-pool="$pool" --location=global --project="$project" --format='value(attributeCondition)')"
  issuer="$(gcloud iam workload-identity-pools providers describe "$provider" --workload-identity-pool="$pool" --location=global --project="$project" --format='value(oidc.issuerUri)')"
  if [[ "$existing" != "$condition" || "$issuer" != 'https://token.actions.githubusercontent.com' ]]; then
    echo 'Existing provider trust differs; refusing to change or broaden it' >&2
    exit 2
  fi
else
  gcloud iam workload-identity-pools providers create-oidc "$provider" --project="$project" --location=global --workload-identity-pool="$pool" --display-name='Private runtime main only' --issuer-uri='https://token.actions.githubusercontent.com' --attribute-mapping='google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref' --attribute-condition="$condition" --quiet >/dev/null
fi
member="principal://iam.googleapis.com/projects/$project_number/locations/global/workloadIdentityPools/$pool/subject/repo:$runtime_repo:ref:refs/heads/main"
gcloud iam service-accounts add-iam-policy-binding "$account" --project="$project" --role='roles/iam.workloadIdentityUser' --member="$member" --quiet >/dev/null
printf 'WIF provider: projects/%s/locations/global/workloadIdentityPools/%s/providers/%s\n' "$project_number" "$pool" "$provider"
printf 'Sheets service identity: %s\n' "$account"
echo 'No project-level data access was granted. Share only the intended private Sheet with this identity.'
