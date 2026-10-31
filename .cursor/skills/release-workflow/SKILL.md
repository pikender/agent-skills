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

Run `.cursor/skills/release-workflow/scripts/release.sh`. Do not commit, push, edit YAML, choose repos, or merge. Do not call the Bitbucket API. Git uses the `origin` remote already configured in each clone.

```bash
.cursor/skills/release-workflow/scripts/release.sh <release-dir> eligible
.cursor/skills/release-workflow/scripts/release.sh <release-dir> cut
.cursor/skills/release-workflow/scripts/release.sh <release-dir> staging
```

`<release-dir>/release.env` is the only input you write:

```
story=WMARCH-1293-PK-brotli-security-fix
date=2026-10-31
```

- `story` is `JIRA_ID-AUTHOR_INITIALS-short-summary`. It names the branch cut from `staging` in `platform-k8s` and `insights-k8s`.
- `date` is the release date. Eligible app repos get `release-YYYY-MM-DD`.

The candidate repos are [catalog.yaml](catalog.yaml). Do not remove a repo from it to change the result.

## Steps

1. `eligible` runs `git fetch origin` in each catalog app repo and writes `<release-dir>/state/eligible.txt`. The reference is the newest `origin/release-YYYY-MM-DD` whose date is before `date`. Local branches are ignored. `git diff --quiet` compares that ref with `origin/HEAD`. A difference means `eligible`. An empty diff means `unchanged` and the current `newTag` stays. No matching remote release branch means `no-remote-release-branch`. Quote this file. Do not edit it.
2. `cut` pushes `release-YYYY-MM-DD` at the current `origin/HEAD` for rows marked `eligible`. It does not switch the checkout in the app repo and does not merge.
3. `staging` runs only after every eligible push has succeeded. For each family that has an eligible image, it pushes one `story` branch from `origin/staging`. Platform tags are `0.0.1-release-YYYY-MM-DD`. Insights tags are `0.0-1-release-YYYY-MM-DD`. Other `newTag` values stay. Repos without an `image` in the catalog are not written into the overlay. Print the branch names and stop. The author opens and merges the pull requests.

A finished step is a no-op. Delete `<release-dir>/state` before recomputing eligibility. On a non-zero exit, show stderr and stop.
