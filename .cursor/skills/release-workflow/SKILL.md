---
name: release-workflow
description: >-
  Prepares staging release branches in local clones. Fetches each catalog
  repo, cuts release-YYYY-MM-DD when origin/default differs from the latest
  origin/release-YYYY-MM-DD, then creates one local branch per k8s repo with
  version-1 image tags. drift compares that release branch with the default
  branch. refresh fast-forwards it when every new commit can come along.
  picks cherry-picks named commits onto it. The default does not push. Use
  when preparing release branches for staging, checking them against master,
  or taking named fixes onto an existing release branch. Not for story
  development, QA, prod, or merging.
---

# Staging release

Run the script. Do not commit, push, edit YAML, choose repos, merge, cherry-pick, or reset by hand. Do not call the Bitbucket API. Do not create `releases/` or `release.env` yourself. Do not write `delete-remote-branches.sh`, `branch-log.txt`, `drift-log.txt`, or `README.md` yourself, and do not run the delete script. Git uses the `origin` remote already configured in each clone.

The default is a dry run. `cut` and `staging` create local branches and do not push. `refresh` fast-forwards an existing local release branch and does not push. `picks` cherry-picks onto the local release branch and does not push. Pass `--push` only when the user explicitly asks to publish those branches.

The user message supplies one release date. Normalize it to `YYYY-MM-DD`. If the message has no date, or more than one, ask for a single `YYYY-MM-DD` and stop.

```bash
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD eligible
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD cut
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD staging
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD drift
```

Publish or update only when asked:

```bash
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD cut --push
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD staging --push
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD refresh
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD refresh --push
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD picks --sha <sha>
.cursor/skills/release-workflow/scripts/release.sh YYYY-MM-DD picks --sha <sha> --push
```

The script uses `releases/YYYY-MM-DD`. `releases/` must already exist. The script creates the date folder when it is missing and writes `release.env` with `date=YYYY-MM-DD`.

The candidate repos are [catalog.yaml](catalog.yaml). Do not remove a repo from it to change the result.

## Setup

`catalog.yaml` is not included in the repository — it contains paths and registry coordinates specific to your environment. Create it once before the first run:

```bash
cp .cursor/skills/release-workflow/catalog.example.yaml \
   .cursor/skills/release-workflow/catalog.yaml
# edit catalog.yaml — fill in your repo paths, registry, and k8s overlay locations
```

To keep the catalog outside the skill folder, set `RELEASE_CATALOG` to any absolute path:

```bash
export RELEASE_CATALOG=~/my-team/release-catalog.yaml
```

If `catalog.yaml` is missing and `RELEASE_CATALOG` is not set, the script prints the setup steps above and exits.

## Steps

1. `eligible` runs `git fetch origin` in each catalog app repo and writes `releases/YYYY-MM-DD/state/eligible.txt`. The reference is the newest `origin/release-YYYY-MM-DD` whose date is before this release date. Local branches are ignored. `git diff --quiet` compares that ref with `origin/HEAD`. A difference means `eligible`. An empty diff means `unchanged` and the current `newTag` stays. No matching remote release branch means `no-remote-release-branch`. Quote this file. Do not edit it.
2. `cut` creates local `release-YYYY-MM-DD` at the current `origin/HEAD` for rows marked `eligible`. It does not switch the checkout in the app repo, does not merge, and does not push. It records each line in `state/cut-local.txt` as `created` or `already-created`. A push records `pushed` or `already-pushed` in `state/cut.txt`. It then writes `releases/YYYY-MM-DD/README.md`, `delete-remote-branches.sh`, and `branch-log.txt`.
3. `staging` runs only after every eligible local branch exists. For each k8s repo that has an eligible image, it creates one local `release-YYYY-MM-DD` branch from `origin/staging`. That branch sets every eligible image tag in that repo's staging `kustomization.yaml`. Platform tags are `0.0.1-release-YYYY-MM-DD`. Insights tags are `0.0-1-release-YYYY-MM-DD`. Other `newTag` values stay. Repos without an `image` in the catalog are not written into the overlay. It rewrites `README.md`, the delete script, and the branch log, then prints the branch names and stops.
4. `--push` publishes the local `cut` branches, then the local `staging` branches. `staging --push` runs only after every eligible `cut --push` has succeeded. Both commands rewrite the same three files. The author opens and merges the pull requests. Read `releases/YYYY-MM-DD/README.md` for what each file is and how to use it.

5. `drift` answers whether this `release-YYYY-MM-DD` needs commits from the default branch. It is safe to repeat, and it does not need `state/` to be deleted. For each catalog app it fetches `origin` and compares the release branch with `origin/HEAD`. For each k8s repo it compares the release branch with `origin/staging`. It writes `releases/YYYY-MM-DD/state/drift.txt` and `releases/YYYY-MM-DD/drift-log.txt`. Quote `drift.txt`. Do not edit it. `in-sync` means the two refs are the same commit. `fast-forward` means the release ref is an ancestor of the base, so every new base commit can move onto the release branch. `release-ahead` means the release branch has commits the base does not. `diverged` means both sides have commits. `no-release-branch` means this date's branch is absent; run `cut` before updating it. `local-remote-diverged` means the local branch and `origin/release-YYYY-MM-DD` are not linear. `error` means that repo could not be fetched or compared. The message is on stderr and in `drift-log.txt`. The other repos are still compared. `drift-log.txt` lists `release..base` and then `base..release`. Run `drift` when the user asks whether the release branch should pick up later master commits or which fixes are missing. Do not run `refresh` or `picks` from that question alone.
6. `refresh` fast-forwards local `release-YYYY-MM-DD` for rows marked `fast-forward`. Every other status stays in place. It does not reset a branch that has its own commits. A dry run moves the local branch and records `state/refresh-local.txt`. `--push` fast-forward pushes and records `state/refresh.txt`. It refuses a push that is not a fast-forward. Run it only when the user asks to take the new default-branch commits onto the release branch. App image tags stay `0.0.1-release-YYYY-MM-DD` and `0.0-1-release-YYYY-MM-DD`, so the k8s overlay does not change. Rebuild the app image from the updated branch. A k8s release branch that contains the staging tag commit is `release-ahead` or `diverged` once `origin/staging` has moved, and `refresh` leaves it in place.
7. `picks` cherry-picks commits the user named. Pass each one as `--sha`. Do not choose SHAs. The commit must be on `origin/HEAD` for an app, or on `origin/staging` for a k8s repo, and the release branch must already exist. Commits are applied in base-branch order onto the local release branch. `already-applied` means the release branch already contains that commit. A conflict or any other cherry-pick error aborts that pick, records `conflict` in `state/picks.txt`, and stops. The branch stays at the last applied commit. `--push` fast-forward pushes after the picks for that repo succeed. A named fix on a `diverged` or `release-ahead` row is a pick. It is not a refresh.

`delete-remote-branches.sh` is one `cd <repo>` and one `git push origin :release-YYYY-MM-DD` per row marked `eligible`, in `eligible.txt` order. A k8s repo is added only when that clone already has `release-YYYY-MM-DD`. The script lists those commands and does not run them.

`branch-log.txt` runs `git log --oneline <default>...release-YYYY-MM-DD` for each eligible repo. `<default>` is the `origin/HEAD` name from `eligible.txt`. The release branch is cut at that ref, so the log is empty when they match. The file then runs `git log --oneline <previous-release>...release-YYYY-MM-DD`. For a k8s repo the left side is `origin/staging`.

A finished `cut` or `staging` dry run does not create or push branches again. A finished push does not push again. Both still rewrite `README.md`, the delete script, and the branch log. A dry run does not count as a push. Delete `releases/YYYY-MM-DD/state` before recomputing eligibility. `drift`, `refresh`, and `picks` run again whenever they are asked; they do not treat a previous run as finished. On a non-zero exit, show stderr and stop. Point the user at `README.md`.
