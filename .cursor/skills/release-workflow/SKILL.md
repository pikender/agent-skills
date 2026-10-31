---
name: release-workflow
description: >-
  Prepares staging release branches in local clones. Fetches each catalog
  repo, cuts release-YYYY-MM-DD when origin/default differs from the latest
  origin/release-YYYY-MM-DD, then creates one local branch per k8s repo with
  version-1 image tags. The default does not push. Use when preparing release
  branches for staging. Not for story development, QA, prod, hotfix, or merging.
---

# Staging release

Run the script. Do not commit, push, edit YAML, choose repos, or merge by hand. Do not call the Bitbucket API. Do not create `releases/` or `release.env` yourself. Do not write `delete-remote-branches.sh` or `branch-log.txt` yourself, and do not run the delete script. Git uses the `origin` remote already configured in each clone.

The default is a dry run. `cut` and `staging` create local branches and do not push. Pass `--push` only when the user explicitly asks to publish those branches.

The user message supplies one release date. Normalize it to `YYYY-MM-DD`. If the message has no date, or more than one, ask for a single `YYYY-MM-DD` and stop.

```bash
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD eligible
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD cut
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD staging
```

Publish only when asked:

```bash
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD cut --push
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD staging --push
```

The script uses `releases/YYYY-MM-DD`. `releases/` must already exist. The script creates the date folder when it is missing and writes `release.env` with `date=YYYY-MM-DD`.

The candidate repos are [catalog.yaml](catalog.yaml). Do not remove a repo from it to change the result.

## Steps

1. `eligible` runs `git fetch origin` in each catalog app repo and writes `releases/YYYY-MM-DD/state/eligible.txt`. The reference is the newest `origin/release-YYYY-MM-DD` whose date is before this release date. Local branches are ignored. `git diff --quiet` compares that ref with `origin/HEAD`. A difference means `eligible`. An empty diff means `unchanged` and the current `newTag` stays. No matching remote release branch means `no-remote-release-branch`. Quote this file. Do not edit it.
2. `cut` creates local `release-YYYY-MM-DD` at the current `origin/HEAD` for rows marked `eligible`. It does not switch the checkout in the app repo, does not merge, and does not push. It records each line in `state/cut-local.txt` as `created` or `already-created`. A push records `pushed` or `already-pushed` in `state/cut.txt`. It then writes `releases/YYYY-MM-DD/delete-remote-branches.sh` and `releases/YYYY-MM-DD/branch-log.txt`.
3. `staging` runs only after every eligible local branch exists. For each k8s repo that has an eligible image, it creates one local `release-YYYY-MM-DD` branch from `origin/staging`. That branch sets every eligible image tag in that repo's staging `kustomization.yaml`. Platform tags are `0.0.1-release-YYYY-MM-DD`. Insights tags are `0.0-1-release-YYYY-MM-DD`. Other `newTag` values stay. Repos without an `image` in the catalog are not written into the overlay. It rewrites the delete script and the branch log, then prints the branch names and stops.
4. `--push` publishes the local `cut` branches, then the local `staging` branches. `staging --push` runs only after every eligible `cut --push` has succeeded. Both commands rewrite the same two files. The author opens and merges the pull requests.

`delete-remote-branches.sh` is one `cd <repo>` and one `git push origin :release-YYYY-MM-DD` per row marked `eligible`, in `eligible.txt` order. A k8s repo is added only when that clone already has `release-YYYY-MM-DD`. The script lists those commands and does not run them.

`branch-log.txt` runs `git log --oneline <default>...release-YYYY-MM-DD` for each eligible repo. `<default>` is the `origin/HEAD` name from `eligible.txt`. The release branch is cut at that ref, so the log is empty when they match. The file then runs `git log --oneline <previous-release>...release-YYYY-MM-DD`. For a k8s repo the left side is `origin/staging`.

A finished dry run does not create or push branches again. A finished push does not push again. Both still rewrite the delete script and the branch log. A dry run does not count as a push. Delete `releases/YYYY-MM-DD/state` before recomputing eligibility. On a non-zero exit, show stderr and stop. Point the user at `delete-remote-branches.sh` and `branch-log.txt`.
