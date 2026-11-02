# Staging release prompts

The default prompt prepares local branches and does not push. Say that you want them pushed when they should be published. [SKILL.md](SKILL.md) is the procedure the agent runs.

## Dry run

One prompt. Local `release-YYYY-MM-DD` branches are created. Remotes stay unchanged.

> prepare the release branches for staging for release on 3rd Nov

```bash
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 eligible
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 cut
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 staging
```

`cut` and `staging` print `created` and `mode: dry-run`.

## Publish in the same prompt

One prompt is enough when it asks to push. Local branches are created and published in that run.

> prepare and push the release branches for staging for release on 3rd Nov

```bash
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 eligible
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 cut --push
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 staging --push
```

`staging --push` runs only after every eligible app branch has been pushed. Output says `pushed` and `mode: push`. Open and merge the pull requests after that.

## Review, then publish

Two prompts. The first is the dry run above. After inspecting the local branches, publish them:

> push the release branches for 3rd Nov

```bash
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 cut --push
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 staging --push
```

A finished dry run does not count as a push. A finished push is a no-op.

## See what landed on the default branch

One prompt. Nothing is moved. The script fetches and writes `state/drift.txt`.

> does the 3rd Nov release branch need commits from master?

```bash
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 drift
```

`fast-forward` means every new commit on the default branch can move onto the release branch. `diverged` means both sides have commits. Quote `state/drift.txt`. The incoming commits are in `drift-log.txt`.

## Take every new commit on the default branch

One prompt after drift shows `fast-forward`. Other statuses stay in place. The local release branch moves. The remote stays until a later prompt asks to push.

> fast-forward the 3rd Nov release branch to master

```bash
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 refresh
```

Publish that fast-forward:

> push the 3rd Nov release branch update

```bash
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 refresh --push
```

Image tags stay on the release date. Rebuild the app image from the updated branch.

## Pick named fixes

One prompt. The user names the commits. The script cherry-picks those SHAs and does not choose them.

> pick abc123 onto the 3rd Nov release branch

```bash
.cursor/skills/release-workflow/scripts/release.sh 2026-11-03 picks --sha abc123
```

Add `--push` when that same prompt asks to publish the pick. A conflict stops the run. The branch stays at the last applied commit.

## Release folder

Every `cut` and every `staging` rewrites `releases/YYYY-MM-DD/README.md`. That file says what else is in the folder, why it is there, and how to use it. A finished run still rewrites it. The 2026-11-03 copy is [README.md](../../../releases/2026-11-03/README.md).

Every `cut` and every `staging` also rewrites the delete list and the branch log. A finished run still rewrites them.

`releases/YYYY-MM-DD/delete-remote-branches.sh` lists one `cd` and one delete per eligible app:

```bash
cd /path/to/repo
git push origin :release-YYYY-MM-DD
```

Rows come from `eligible` in `state/eligible.txt`. A k8s repo is included only when that clone already has `release-YYYY-MM-DD`. The skill does not run this file.

`releases/YYYY-MM-DD/branch-log.txt` records `git log --oneline <default>...release-YYYY-MM-DD` for each eligible repo. The release branch is cut at `origin/HEAD`, so that range is empty when the two refs match. The file then logs the previous `origin/release-YYYY-MM-DD` against this release branch. A k8s section uses `origin/staging` on the left.

The 2026-11-03 copies are [delete-remote-branches.sh](../../../releases/2026-11-03/delete-remote-branches.sh) and [branch-log.txt](../../../releases/2026-11-03/branch-log.txt).
