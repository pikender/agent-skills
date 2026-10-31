---
name: release-workflow
description: >-
  Prepares staging release branches in local clones. Fetches each catalog
  repo, cuts release-YYYY-MM-DD when origin/default differs from the latest
  origin/release-YYYY-MM-DD, then pushes one branch per k8s repo with
  version-1 image tags. Use when preparing release branches for staging.
  Not for story development, QA, prod, hotfix, or merging.
---

# Staging release

Run the script. Do not commit, push, edit YAML, choose repos, or merge. Do not call the Bitbucket API. Do not create `releases/` or `release.env` yourself. Git uses the `origin` remote already configured in each clone.

The user message supplies one release date. Normalize it to `YYYY-MM-DD`. If the message has no date, or more than one, ask for a single `YYYY-MM-DD` and stop.

```bash
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD eligible
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD cut
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD staging
```

The script uses `releases/YYYY-MM-DD`. `releases/` must already exist. The script creates the date folder when it is missing and writes `release.env` with `date=YYYY-MM-DD`.

The candidate repos are [catalog.yaml](catalog.yaml). Do not remove a repo from it to change the result.

## Steps

1. `eligible` runs `git fetch origin` in each catalog app repo and writes `releases/YYYY-MM-DD/state/eligible.txt`. The reference is the newest `origin/release-YYYY-MM-DD` whose date is before this release date. Local branches are ignored. `git diff --quiet` compares that ref with `origin/HEAD`. A difference means `eligible`. An empty diff means `unchanged` and the current `newTag` stays. No matching remote release branch means `no-remote-release-branch`. Quote this file. Do not edit it.
2. `cut` pushes `release-YYYY-MM-DD` at the current `origin/HEAD` for rows marked `eligible`. It does not switch the checkout in the app repo and does not merge.
3. `staging` runs only after every eligible push has succeeded. For each k8s repo that has an eligible image, it pushes one `release-YYYY-MM-DD` branch from `origin/staging`. That branch sets every eligible image tag in that repo's staging `kustomization.yaml`. Platform tags are `0.0.1-release-YYYY-MM-DD`. Insights tags are `0.0-1-release-YYYY-MM-DD`. Other `newTag` values stay. Repos without an `image` in the catalog are not written into the overlay. Print the branch names and stop. The author opens and merges the pull requests.

A finished step is a no-op. Delete `releases/YYYY-MM-DD/state` before recomputing eligibility. On a non-zero exit, show stderr and stop.
