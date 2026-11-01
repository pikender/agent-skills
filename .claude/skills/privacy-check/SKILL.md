---
name: privacy-check
description: >-
  Scans staged changes and tracked files for private or sensitive information
  before pushing to GitHub. Runs gitleaks (required) for 150+ community
  secret patterns, then layers project-specific checks: internal URLs, real
  service names from catalog.yaml, registry hosts, and company emails.
  Use before any push or PR to a public repository.
---

# Privacy check before sharing on GitHub

Scan for private or sensitive information that must not appear in a public repository.

**Requires:** `gitleaks` — install once with `brew install gitleaks`

Arguments: `$ARGUMENTS`

Parse arguments:
- `--all`: scan all git-tracked files, not just staged/modified
- `--path <file-or-dir>`: restrict scan to a specific path

## Step 0 — Verify gitleaks is installed

Run:
```bash
which gitleaks
```

If not found, stop immediately and tell the user:

```
gitleaks is required but not installed.
Install it with:  brew install gitleaks
Then re-run /privacy-check.
```

Do not proceed past this step until gitleaks is available.

## Step 1 — Determine scope

If `--path` is given, scope = that path.
If `--all` is given, scope = all git-tracked files (`git ls-files`).
Otherwise (default), union `git diff --cached --name-only` and `git diff --name-only`.
If that union is empty, tell the user nothing is staged or modified and ask whether to run `--all`.

## Step 2 — Run gitleaks (community patterns)

Run gitleaks against the determined scope.

For staged/modified files (default scope):
```bash
gitleaks detect --source . --staged --report-format json --report-path /tmp/gitleaks-report.json --no-git 2>/dev/null; \
gitleaks detect --source . --report-format json --report-path /tmp/gitleaks-report.json 2>/dev/null
```

For `--all`:
```bash
gitleaks detect --source . --report-format json --report-path /tmp/gitleaks-report.json 2>/dev/null
```

For `--path <p>`:
```bash
gitleaks detect --source <p> --report-format json --report-path /tmp/gitleaks-report.json --no-git 2>/dev/null
```

gitleaks exits 1 when it finds secrets — that is expected, not an error. Read `/tmp/gitleaks-report.json` with the Read tool. Each entry has:
- `Description` — rule name (e.g. "GitHub Personal Access Token")
- `File` — path
- `StartLine` — line number
- `Secret` — the matched value (truncate to 40 chars when displaying)
- `RuleID` — machine-readable rule identifier

Collect all gitleaks findings as **Layer 1 — Community findings**.

## Step 3 — Extract project-specific sensitive terms

This project uses `catalog.yaml` (or the path in `$RELEASE_CATALOG`) to store real repo paths, registry hosts, image names, and k8s overlay paths. If that file exists, read it and extract:

- All `image:` values → real container image names / registry hosts
- All `path:` values → local filesystem paths (must never appear in committed text)
- The registry hostname from any `image:` (the part before the first `/`)
- Any `overlay:` paths that contain org-specific directory names
- All `id:` values → real internal service names

These become **project-specific forbidden patterns** for Layer 2.

Also read `.privacy-patterns.yaml` from the repo root if present:

```yaml
forbidden:
  - pattern: "my-real-service-name"
    description: "Internal service name"
  - pattern: "artifactory\\.mycompany\\.com"
    description: "Internal Artifactory host"

forbidden_email_domains:
  - mycompany.com

forbidden_host_patterns:
  - "\\.corp\\b"
  - "\\.internal\\b"
```

## Step 4 — Layer 2: project + built-in pattern scan

Read each in-scope file with the **Read tool** (skip binaries and generated files: `*.png`, `*.jpg`, `*.gif`, `*.ico`, `*.woff*`, `*.ttf`, `*.pdf`, `*.zip`, `*.tar*`, `*.gz`, `*.jar`, `*.class`, `*.pyc`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Gemfile.lock`, `Cargo.lock`, `go.sum`). Skip `.git/`, `catalog.yaml`, and `releases/` state files.

Apply these patterns:

**Internal infrastructure (built-in)**
- `.internal` hostnames: `[a-z0-9\-]+\.internal\b`
- `.corp` hostnames: `[a-z0-9\-]+\.corp\b`
- Artifactory URLs: `artifactory\.[a-z0-9\-]+\.[a-z]{2,}`
- Private container registry with port: `[a-z0-9\-\.]+:[0-9]{4,5}/[a-z0-9\-_/]+`
- Private IP in URLs: `https?://(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)`
- Registry host extracted from catalog.yaml `image:` fields

**Real service / repo names (from catalog.yaml)**
- Each `id:` value and full image name — must not appear in scripts, docs, or config that will be public

**Identity**
- Emails: flag if domain is in `forbidden_email_domains`; if none configured, flag all non-public domains (exclude @gmail.com, @yahoo.com, @outlook.com, @hotmail.com, @icloud.com, @proton.me, @github.com)

**Local paths**
- Absolute paths from catalog.yaml `path:` fields
- Generic personal paths: `/Users/[^/]+/` or `/home/[^/]+/`

**Custom patterns** from `.privacy-patterns.yaml` `forbidden:` list

## Step 5 — Deduplicate and apply judgment

Merge Layer 1 (gitleaks) and Layer 2 (project scan). Deduplicate by file+line.

For each finding apply judgment:
- **Real finding** — genuine private data, real hostname, real service name, real credential.
- **Likely safe** — clearly a placeholder (`changeme`, `your-org`, `your-token-here`, `<PASSWORD>`, `example.com`, `localhost`, `127.0.0.1`, `***`), inside `test/` / `fixtures/` with obvious fake data, or in a comment describing the expected format (e.g. `catalog.example.yaml`). Drop these silently.
- **Needs review** — uncertain. Include and flag.

## Step 6 — Report

```
## Privacy scan — <scope description>

### Layer 1 — Community (gitleaks): <N> findings
### Layer 2 — Project-specific: <M> findings
### Total: <N+M> real · <K> needs-review · <F> files scanned

| # | Source    | File | Line | Category | Detail |
|---|-----------|------|------|----------|--------|
| 1 | gitleaks  | config/prod.yaml | 12 | GitHub PAT           | ghp_xxxx...          |
| 2 | gitleaks  | .env.example     |  7 | Generic API Key      | sk-xxxx...           |
| 3 | project   | scripts/deploy.sh | 3 | Real service name   | my-internal-service  |
| 4 | project   | README.md         | 18 | Internal URL        | artifactory.corp.com |

### Setup notes
- catalog.yaml found — extracted <N> service names and <M> registry hosts
- No .privacy-patterns.yaml — add one to flag company-specific terms
  (see ~/.claude/skills/privacy-check/SKILL.md for schema)
```

Zero findings: say so clearly and state scope + gitleaks version used.

## Step 7 — Offer next steps

After the report, ask the user which findings to fix. For each confirmed finding suggest the minimal change:
- Replace real value with a generic placeholder
- Remove the line
- Move secrets to a `.env` listed in `.gitignore`

Apply each fix with the **Edit tool** only after the user confirms it. Do NOT auto-fix, commit, stage, or push anything.
