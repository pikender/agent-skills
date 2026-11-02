# agent-skills

Claude Code skills for release management and pre-push privacy checks.

## Skills

### release-workflow

Prepares staging release branches across multiple repos from a single prompt. Fetches each catalog repo, cuts `release-YYYY-MM-DD` branches where `origin/HEAD` differs from the latest release ref, and updates k8s overlay image tags. `drift` compares an existing release branch with the default branch, `refresh` fast-forwards it, and `picks` cherry-picks named commits.

Trigger: ask Claude to _prepare_, _cut_, or _run_ a staging release, or to check a release branch against master and take named fixes onto it.

See [.cursor/skills/release-workflow/README.md](.cursor/skills/release-workflow/README.md) for usage and [SKILL.md](.cursor/skills/release-workflow/SKILL.md) for the full procedure.

**Setup** — create your local catalog before the first run:

```bash
cp .cursor/skills/release-workflow/catalog.example.yaml \
   .cursor/skills/release-workflow/catalog.yaml
# fill in repo paths, registry, and k8s overlay locations
```

To keep the catalog outside the skill folder, set `RELEASE_CATALOG` to any absolute path:

```bash
export RELEASE_CATALOG=~/my-team/release-catalog.yaml
```

### privacy-check

Scans staged changes or all tracked files for sensitive information before pushing to GitHub. Runs [gitleaks](https://github.com/gitleaks/gitleaks) for 150+ community secret patterns, then layers project-specific checks derived from `catalog.yaml` and an optional `.privacy-patterns.yaml`.

Trigger: `/privacy-check` or ask Claude to _check for private/sensitive info_.

**Requires:**

```bash
brew install gitleaks
```

**Optional** — add a `.privacy-patterns.yaml` at the repo root to flag company-specific service names, internal domains, and registry hosts. This file is git-ignored.

## Requirements

- [gitleaks](https://github.com/gitleaks/gitleaks) — `brew install gitleaks`
- Git with an `origin` remote configured in each clone listed in `catalog.yaml`
