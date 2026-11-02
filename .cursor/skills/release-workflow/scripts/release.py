#!/usr/bin/env python3
"""Prepare staging release branches from local clones.

The default creates local branches and does not push. Pass --push to publish them.
drift compares an existing release branch with its base. refresh fast-forwards
that branch when the base contains it. picks cherry-picks named commits.
"""

import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RELEASE_REF_RE = re.compile(
    r"^refs/remotes/origin/release-(\d{4}-\d{2}-\d{2})$"
)
NAME_RE = re.compile(r'^(\s*)-\s+name:\s+"?([^"\s]+)"?\s*$')
TAG_RE = re.compile(r'^(\s*)newTag:\s+(?:"([^"]*)"|(\S+))\s*$')
ENV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent


class GitError(Exception):
    pass


class Fatal(Exception):
    pass


def die(message):
    raise Fatal(message)


def git_env():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(repo, args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=git_env(),
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise GitError(f"git {' '.join(args)} failed in {repo}: {detail}")
    return result


def catalog_path():
    override = os.environ.get("RELEASE_CATALOG")
    if override:
        return Path(override)
    return SKILL_DIR / "catalog.yaml"


def _catalog_missing_hint(path):
    example = SKILL_DIR / "catalog.example.yaml"
    lines = [
        f"catalog not found: {path}",
        "",
        "To set up:",
        f"  cp {example} {path}",
        "  # edit catalog.yaml — fill in your repo paths and registry",
        "",
        "Or point to an existing file:",
        "  export RELEASE_CATALOG=/path/to/your/catalog.yaml",
    ]
    return "\n".join(lines)


def parse_catalog(text):
    k8s = {}
    apps = []
    section = None
    family = None
    app = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line == "k8s:":
            section = "k8s"
            family = None
            continue
        if line == "apps:":
            section = "apps"
            app = None
            continue
        if section == "k8s":
            header = re.match(r"^  ([a-z]+):\s*$", line)
            if header:
                family = header.group(1)
                k8s[family] = {}
                continue
            field = re.match(r"^    (path|overlay|tag_prefix):\s+(\S+)\s*$", line)
            if field and family:
                k8s[family][field.group(1)] = field.group(2)
                continue
        if section == "apps":
            header = re.match(r"^  - id:\s+(\S+)\s*$", line)
            if header:
                app = {"id": header.group(1), "image": ""}
                apps.append(app)
                continue
            field = re.match(r"^    (path|family|image):\s+(\S+)\s*$", line)
            if field and app is not None:
                app[field.group(1)] = field.group(2)
                continue
        die(f"cannot parse catalog line: {raw}")
    for name, entry in k8s.items():
        for key in ("path", "overlay", "tag_prefix"):
            if key not in entry:
                die(f"catalog k8s.{name} is missing {key}")
    seen = set()
    for app in apps:
        if app["id"] in seen:
            die(f"duplicate catalog id {app['id']}")
        seen.add(app["id"])
        for key in ("path", "family"):
            if key not in app:
                die(f"catalog app {app['id']} is missing {key}")
        if app["family"] not in k8s:
            die(f"catalog app {app['id']} has unknown family {app['family']}")
    if not apps:
        die("catalog has no apps")
    return {"k8s": k8s, "apps": apps}


def load_catalog():
    path = catalog_path()
    if not path.is_file():
        die(_catalog_missing_hint(path))
    return parse_catalog(path.read_text())


def parse_date(text):
    try:
        day = datetime.datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        die("date must be YYYY-MM-DD")
    return day


def releases_root():
    override = os.environ.get("RELEASE_ROOT")
    if override:
        return Path(override)
    return SKILL_DIR.parents[2] / "releases"


def ensure_release_dir(date_text):
    root = releases_root()
    if not root.is_dir():
        die(f"releases directory not found: {root}")
    release_dir = root / date_text
    release_dir.mkdir(exist_ok=True)
    env_path = release_dir / "release.env"
    if not env_path.is_file():
        env_path.write_text(f"date={date_text}\n")
    return release_dir


def parse_release_env(path, date_text):
    if not path.is_file():
        die(f"release.env not found: {path}")
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_RE.match(line)
        if not match:
            die(f"invalid release.env line: {raw}")
        key, value = match.group(1), match.group(2).strip()
        if key in values:
            die(f"duplicate release.env key: {key}")
        values[key] = value
    extra = set(values) - {"date"}
    if extra:
        die(f"unknown release.env keys: {', '.join(sorted(extra))}")
    date = parse_date(values.get("date", ""))
    if date.isoformat() != date_text:
        die(f"release.env date is {date.isoformat()}, expected {date_text}")
    return {"date": date, "date_text": date.isoformat(), "branch": f"release-{date.isoformat()}"}


def parse_day(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def default_ref(repo):
    result = git(repo, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], check=False)
    if result.returncode != 0:
        die(f"origin/HEAD is not set in {repo}")
    ref = result.stdout.strip()
    prefix = "refs/remotes/origin/"
    if not ref.startswith(prefix):
        die(f"unexpected origin/HEAD in {repo}: {ref}")
    return "origin/" + ref[len(prefix):]


def latest_release_ref(repo, release_date):
    result = git(repo, ["for-each-ref", "--format=%(refname)", "refs/remotes/origin"])
    best_day = None
    best_ref = None
    for ref in result.stdout.splitlines():
        match = RELEASE_REF_RE.match(ref)
        if not match:
            continue
        day = parse_day(match.group(1))
        if day is None or day >= release_date:
            continue
        if best_day is None or day > best_day:
            best_day = day
            best_ref = f"origin/release-{match.group(1)}"
    return best_ref


def classify(repo, release_date):
    git(repo, ["fetch", "origin", "--prune"])
    base = default_ref(repo)
    reference = latest_release_ref(repo, release_date)
    if reference is None:
        return "no-remote-release-branch", "-", base
    diff = git(repo, ["diff", "--quiet", reference, base], check=False)
    if diff.returncode == 0:
        return "unchanged", reference, base
    if diff.returncode == 1:
        return "eligible", reference, base
    detail = (diff.stderr or diff.stdout or "").strip()
    raise GitError(f"git diff failed in {repo}: {detail}")


def state_paths(release_dir):
    state = release_dir / "state"
    return {
        "dir": state,
        "eligible": state / "eligible.txt",
        "cut": state / "cut.txt",
        "cut_local": state / "cut-local.txt",
        "staging": state / "staging.txt",
        "staging_local": state / "staging-local.txt",
    }


def read_rows(path):
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            die(f"invalid state row: {line}")
        rows.append(
            {
                "id": parts[0],
                "status": parts[1],
                "reference": parts[2],
                "default": parts[3],
            }
        )
    return rows


def read_ids(path):
    if not path.is_file():
        return []
    ids = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        ids.append(line.split("\t", 1)[0])
    return ids


def upsert_status(path, name, line):
    lines = []
    if path.is_file():
        lines = [item.strip() for item in path.read_text().splitlines() if item.strip()]
    replaced = False
    out = []
    for existing in lines:
        if existing.split("\t", 1)[0] == name:
            out.append(line)
            replaced = True
        else:
            out.append(existing)
    if not replaced:
        out.append(line)
    write_text(path, "".join(f"{item}\n" for item in out))
    return [item.split("\t", 1)[0] for item in out]


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def require_repo(path):
    if not (Path(path) / ".git").exists() and not Path(path).is_dir():
        die(f"repo path not found: {path}")
    probe = git(path, ["rev-parse", "--is-inside-work-tree"], check=False)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        die(f"not a git repo: {path}")


def cmd_eligible(release_dir, catalog, release):
    paths = state_paths(release_dir)
    if any(
        paths[name].exists()
        for name in ("cut", "cut_local", "staging", "staging_local")
    ):
        die("delete the release state directory before recomputing eligibility")
    if paths["eligible"].exists():
        sys.stdout.write(paths["eligible"].read_text())
        sys.stdout.flush()
        print("eligible already complete", file=sys.stderr)
        return
    lines = []
    for app in catalog["apps"]:
        require_repo(app["path"])
        status, reference, base = classify(app["path"], release["date"])
        lines.append(f"{app['id']}\t{status}\t{reference}\t{base}")
    text = "\n".join(lines) + "\n"
    write_text(paths["eligible"], text)
    sys.stdout.write(text)


def remote_sha(repo, branch):
    result = git(
        repo,
        ["rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def local_sha(repo, branch):
    result = git(
        repo,
        ["rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def refuse_non_fast_forward(repo, existing, target_sha, branch, base_ref):
    if not existing or existing == target_sha:
        return
    ancestor = git(
        repo,
        ["merge-base", "--is-ancestor", existing, target_sha],
        check=False,
    )
    if ancestor.returncode != 0:
        die(
            f"origin/{branch} in {repo} is not an ancestor of {base_ref}; "
            "not force-pushing"
        )


def create_local_release_branch(repo, base_ref, branch):
    base_sha = git(repo, ["rev-parse", base_ref]).stdout.strip()
    refuse_non_fast_forward(repo, remote_sha(repo, branch), base_sha, branch, base_ref)
    existing = local_sha(repo, branch)
    if existing == base_sha:
        return base_sha
    if existing:
        die(f"local branch {branch} in {repo} is {existing}, expected {base_sha}")
    git(repo, ["branch", "--no-track", branch, base_sha])
    return base_sha


def push_release_branch(repo, base_ref, branch):
    sha = create_local_release_branch(repo, base_ref, branch)
    existing = remote_sha(repo, branch)
    if existing == sha:
        return sha
    refuse_non_fast_forward(repo, existing, sha, branch, base_ref)
    git(repo, ["push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"])
    return sha


def cut_satisfied(app_id, push, local_ids, pushed_ids):
    if app_id in pushed_ids:
        return True
    return not push and app_id in local_ids


def branch_ref(repo, branch):
    if local_sha(repo, branch):
        return branch
    if remote_sha(repo, branch):
        return f"origin/{branch}"
    return None


def oneline_log(repo, left, right):
    result = git(repo, ["log", "--oneline", f"{left}...{right}"], check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise GitError(f"git log failed in {repo}: {detail}")
    text = result.stdout.strip()
    if text:
        return text
    sha = git(repo, ["rev-parse", right]).stdout.strip()
    return f"(no commits; {left} and {right} are both {sha})"


def release_targets(catalog, rows, branch):
    by_id = {app["id"]: app for app in catalog["apps"]}
    targets = []
    seen = set()
    for row in rows:
        if row["status"] != "eligible":
            continue
        app = by_id.get(row["id"])
        if app is None:
            die(f"{row['id']} is in state/eligible.txt and not in the catalog")
        path = app["path"]
        if path in seen:
            continue
        seen.add(path)
        targets.append(
            {
                "id": row["id"],
                "path": path,
                "left": row["default"],
                "previous": row["reference"],
                "kind": "app",
            }
        )
    for family, entry in catalog["k8s"].items():
        path = entry["path"]
        if path in seen or branch_ref(path, branch) is None:
            continue
        seen.add(path)
        targets.append(
            {
                "id": family,
                "path": path,
                "left": "origin/staging",
                "previous": "",
                "kind": "k8s",
            }
        )
    return targets


def write_release_notes(release_dir, catalog, rows, branch):
    targets = release_targets(catalog, rows, branch)
    delete_chunks = []
    log_parts = [
        branch,
        "Repos marked eligible in state/eligible.txt.",
        "Each app release branch is cut at origin/HEAD, so the default branch and this release branch match when the first log is empty.",
        "The second log is the previous origin/release branch against this release branch.",
        "",
    ]
    for target in targets:
        path = target["path"]
        require_repo(path)
        delete_chunks.append(f"cd {path}\ngit push origin :{branch}\n")
        ref = branch_ref(path, branch)
        log_parts.append(target["id"])
        log_parts.append(path)
        log_parts.append("")
        if target["kind"] == "k8s":
            log_parts.append("Created from origin/staging.")
            log_parts.append("")
        if ref is None:
            log_parts.append(f"git log --oneline {target['left']}...{branch}")
            log_parts.append(f"({branch} is not in this clone)")
            log_parts.append("")
            continue
        log_parts.append(f"git log --oneline {target['left']}...{ref}")
        log_parts.append(oneline_log(path, target["left"], ref))
        log_parts.append("")
        previous = target["previous"]
        if previous and previous != "-":
            log_parts.append(f"git log --oneline {previous}...{ref}")
            log_parts.append(oneline_log(path, previous, ref))
            log_parts.append("")
    delete_path = release_dir / "delete-remote-branches.sh"
    log_path = release_dir / "branch-log.txt"
    delete_text = "\n".join(delete_chunks)
    if delete_text and not delete_text.endswith("\n"):
        delete_text += "\n"
    write_text(delete_path, delete_text)
    write_text(log_path, "\n".join(log_parts).rstrip() + "\n")
    guide_path = release_dir / "README.md"
    write_text(guide_path, folder_guide(release_dir, branch))
    print(f"wrote {delete_path}", file=sys.stderr)
    print(f"wrote {log_path}", file=sys.stderr)
    print(f"wrote {guide_path}", file=sys.stderr)


def state_excerpt(release_dir, name):
    path = release_dir / "state" / name
    if not path.is_file():
        return f"`state/{name}` is not in this folder yet."
    body = path.read_text().rstrip()
    if not body:
        return f"`state/{name}` is empty."
    return f"`state/{name}`:\n\n```\n{body}\n```"


def folder_guide(release_dir, branch):
    date_text = branch[len("release-"):]
    script = ".cursor/skills/release-workflow/scripts/release.sh"
    return f"""# {branch}

This folder is the record of one staging release. `cut` and `staging` rewrite this file, `delete-remote-branches.sh`, and `branch-log.txt`. `drift`, `refresh`, and `picks` rewrite this file and `drift-log.txt`. The release script does not run the delete script.

## release.env

`date={date_text}`. The script creates this file. Leave it in place.

## state/eligible.txt

One row per catalog app: id, status, the previous `origin/release-*`, and `origin/HEAD`. `eligible` means that app has a `{branch}` branch. `unchanged` means it does not. `no-remote-release-branch` means no older remote release branch was found.

`eligible` will not recompute this file while `state/cut-local.txt`, `state/cut.txt`, `state/staging-local.txt`, or `state/staging.txt` exists. Delete `state/` before a new eligibility pass. That also drops the already-created record, so the next `cut` prints `created` again.

{state_excerpt(release_dir, "eligible.txt")}

## state/cut-local.txt

Dry-run record for app repos. `created` plus a commit is the first local branch. `already-created` means a later `cut` found that line and left the local branch in place.

{state_excerpt(release_dir, "cut-local.txt")}

## state/cut.txt

Push record. `pushed` or `already-pushed`. Written only by `cut --push`.

{state_excerpt(release_dir, "cut.txt")}

## state/staging-local.txt and state/staging.txt

Same pattern for each k8s family. `skipped-no-eligible-images` means that overlay was left unchanged. `staging-local.txt` is the dry run. `staging.txt` is the push.

{state_excerpt(release_dir, "staging-local.txt")}

{state_excerpt(release_dir, "staging.txt")}

## state/drift.txt

One row per catalog app, then each k8s family. Columns are kind, id, status, commits only on the release branch, commits only on the base, the release ref, the base ref, and the `origin/{branch}` sha or `-`.

The app base is `origin/HEAD`. The k8s base is `origin/staging`. `in-sync` means those refs are the same commit. `fast-forward` means the release ref is an ancestor of the base, so the new base commits can move onto it. `release-ahead` means the release ref has commits the base does not. `diverged` means both sides have commits. `no-release-branch` means `{branch}` is absent. `local-remote-diverged` means the local branch and `origin/{branch}` are not linear. `error` means that repo could not be fetched or compared. The message is in `drift-log.txt`. The other repos are still compared.

`drift`, `refresh`, and `picks` rewrite this file. Deleting `state/` is not required. `eligible` does not read it.

{state_excerpt(release_dir, "drift.txt")}

## drift-log.txt

`git log --oneline <release>..<base>` lists commits on the base that are not on the release branch. The next command lists commits only on the release branch. `refresh` writes this file from the comparison before it moves branches. The next `drift` replaces it with the current comparison.

App image tags stay on the release date. Moving an app branch does not edit the overlay. Rebuild the image from the updated branch.

## state/refresh-local.txt and state/refresh.txt

`refresh` fast-forwards local `{branch}` for rows marked `fast-forward`. `fast-forwarded` records the old and new commits. `already-current` means the local branch already matched the base. `skipped` leaves every other status in place. `refresh.txt` is the push.

{state_excerpt(release_dir, "refresh-local.txt")}

{state_excerpt(release_dir, "refresh.txt")}

## state/picks.txt

One named commit per row: kind, id, sha, `applied`, `already-applied`, or `conflict`, and the subject. `picks` stops on the first conflict and leaves the branch at the last applied commit.

{state_excerpt(release_dir, "picks.txt")}

## delete-remote-branches.sh

Deletes `origin/{branch}`. One `cd` and one `git push origin :{branch}` per repo marked `eligible`, then a k8s repo when that clone already has `{branch}`.

Read `branch-log.txt` before running it. The release script never runs this file. Run it yourself when those remote branches should be removed:

```bash
sh releases/{date_text}/delete-remote-branches.sh
```

## branch-log.txt

`git log --oneline <default>...{branch}` for each eligible repo. `<default>` is the `origin/HEAD` name from `state/eligible.txt`. The release branch is cut at that ref, so the log is empty when the two refs match. The next command is the previous `origin/release-*` against `{branch}`, which lists the commits that made the repo eligible. A k8s section uses `origin/staging` on the left.

## Run again

Refresh this folder without recomputing eligibility:

```bash
{script} {date_text} cut
{script} {date_text} staging
```

Publish the local branches:

```bash
{script} {date_text} cut --push
{script} {date_text} staging --push
```

`staging --push` runs only after every eligible app branch is listed in `state/cut.txt`.

Compare `{branch}` with the default branch:

```bash
{script} {date_text} drift
```

Fast-forward rows take the new base commits. Other statuses stay in place:

```bash
{script} {date_text} refresh
{script} {date_text} refresh --push
```

Cherry-pick commits named with `--sha`:

```bash
{script} {date_text} picks --sha <sha>
{script} {date_text} picks --sha <sha> --push
```
"""


def cmd_cut(release_dir, catalog, release, push):
    paths = state_paths(release_dir)
    if not paths["eligible"].is_file():
        die("run eligible before cut")
    print("mode: push" if push else "mode: dry-run", file=sys.stderr)
    rows = read_rows(paths["eligible"])
    by_id = {app["id"]: app for app in catalog["apps"]}
    local_ids = read_ids(paths["cut_local"])
    pushed_ids = read_ids(paths["cut"])
    eligible = [row for row in rows if row["status"] == "eligible"]
    branch = f"release-{release['date_text']}"
    if eligible and all(
        cut_satisfied(row["id"], push, local_ids, pushed_ids) for row in eligible
    ):
        for row in eligible:
            label = "already-pushed" if row["id"] in pushed_ids else "already-created"
            print(f"{row['id']}\t{label}\t{branch}")
            record = paths["cut"] if label == "already-pushed" else paths["cut_local"]
            upsert_status(record, row["id"], f"{row['id']}\t{label}\t{branch}")
        sys.stdout.flush()
        print("cut already complete", file=sys.stderr)
        write_release_notes(release_dir, catalog, rows, branch)
        return
    for row in eligible:
        if cut_satisfied(row["id"], push, local_ids, pushed_ids):
            label = "already-pushed" if row["id"] in pushed_ids else "already-created"
            print(f"{row['id']}\t{label}\t{branch}")
            record = paths["cut"] if label == "already-pushed" else paths["cut_local"]
            upsert_status(record, row["id"], f"{row['id']}\t{label}\t{branch}")
            continue
        app = by_id.get(row["id"])
        if app is None:
            die(f"{row['id']} is in state/eligible.txt and not in the catalog")
        require_repo(app["path"])
        git(app["path"], ["fetch", "origin", "--prune"])
        base = default_ref(app["path"])
        if push:
            sha = push_release_branch(app["path"], base, branch)
            upsert_status(paths["cut_local"], row["id"], f"{row['id']}\tcreated\t{branch}\t{sha}")
            pushed_ids = upsert_status(paths["cut"], row["id"], f"{row['id']}\tpushed\t{branch}\t{sha}")
            print(f"{row['id']}\tpushed\t{branch}\t{sha}")
        else:
            sha = create_local_release_branch(app["path"], base, branch)
            local_ids = upsert_status(
                paths["cut_local"], row["id"], f"{row['id']}\tcreated\t{branch}\t{sha}"
            )
            print(f"{row['id']}\tcreated\t{branch}\t{sha}")
    write_release_notes(release_dir, catalog, rows, branch)


def update_overlay(text, tags):
    lines = text.splitlines(keepends=True)
    if text and not text.endswith("\n"):
        lines[-1] = lines[-1] + "\n"
    found = set()
    out = []
    index = 0
    while index < len(lines):
        match = NAME_RE.match(lines[index].rstrip("\n"))
        image = match.group(2) if match else None
        if image in tags:
            if index + 1 >= len(lines):
                die(f"overlay image {image} has no newTag")
            tag_match = TAG_RE.match(lines[index + 1].rstrip("\n"))
            if not tag_match:
                die(f"overlay image {image} has no newTag on the next line")
            indent = tag_match.group(1)
            quoted = tag_match.group(2) is not None
            tag = tags[image]
            if quoted:
                updated = f'{indent}newTag: "{tag}"\n'
            else:
                updated = f"{indent}newTag: {tag}\n"
            out.append(lines[index] if lines[index].endswith("\n") else lines[index] + "\n")
            out.append(updated)
            found.add(image)
            index += 2
            continue
        out.append(lines[index])
        index += 1
    missing = sorted(set(tags) - found)
    if missing:
        die("images missing from overlay: " + ", ".join(missing))
    return "".join(out)


def overlay_text(repo, overlay):
    result = git(repo, ["show", f"origin/staging:{overlay}"])
    return result.stdout


def same_overlay(left, right):
    return left == right or left == right + "\n" or right == left + "\n"


def branch_overlay(repo, branch, overlay):
    result = git(repo, ["show", f"{branch}:{overlay}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def prepare_staging_family(family, entry, branch, tags, message, push):
    repo = entry["path"]
    require_repo(repo)
    git(repo, ["fetch", "origin", "--prune"])
    staging = git(repo, ["rev-parse", "-q", "--verify", "refs/remotes/origin/staging"], check=False)
    if staging.returncode != 0:
        die(f"origin/staging is missing in {repo}")
    if remote_sha(repo, branch):
        die(f"origin/{branch} already exists in {repo}; not overwriting it")
    original = overlay_text(repo, entry["overlay"])
    updated = update_overlay(original, tags)
    if same_overlay(original, updated):
        return "unchanged-tags"
    local = git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False)
    created_branch = False
    work = None
    committed = False
    try:
        if local.returncode == 0:
            current = branch_overlay(repo, branch, entry["overlay"])
            if current is None or not same_overlay(current, updated):
                die(f"local branch {branch} in {repo} does not match the staging tag update")
            committed = True
        else:
            work = Path(tempfile.mkdtemp(prefix=f"release-{family}-"))
            git(repo, ["worktree", "add", "-b", branch, str(work), "origin/staging"])
            created_branch = True
            target = work / entry["overlay"]
            if not target.is_file():
                die(f"overlay missing in {repo}: {entry['overlay']}")
            target.write_text(updated)
            git(work, ["add", "--", entry["overlay"]])
            git(work, ["commit", "-m", message])
            committed = True
        if push:
            git(repo, ["push", "-u", "origin", branch])
            return "pushed"
        return "created"
    finally:
        if work is not None:
            git(repo, ["worktree", "remove", "--force", str(work)], check=False)
            shutil.rmtree(work, ignore_errors=True)
            git(repo, ["worktree", "prune"], check=False)
        if created_branch and not committed:
            exists = git(
                repo,
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            )
            if exists.returncode == 0:
                git(repo, ["branch", "-D", branch], check=False)


def record_family(path, done, family, line):
    upsert_status(path, family, line)
    done.add(family)
    return done


def staging_satisfied(family, push, local_done, pushed_done):
    if family in pushed_done:
        return True
    return not push and family in local_done


def cmd_staging(release_dir, catalog, release, push):
    paths = state_paths(release_dir)
    if not paths["eligible"].is_file():
        die("run eligible before staging")
    print("mode: push" if push else "mode: dry-run", file=sys.stderr)
    rows = read_rows(paths["eligible"])
    eligible_ids = [row["id"] for row in rows if row["status"] == "eligible"]
    cut_local = set(read_ids(paths["cut_local"]))
    cut_pushed = set(read_ids(paths["cut"]))
    if push:
        missing = [item for item in eligible_ids if item not in cut_pushed]
        if missing:
            die("cut has not pushed: " + ", ".join(missing))
    else:
        missing = [
            item for item in eligible_ids if item not in cut_local and item not in cut_pushed
        ]
        if missing:
            die("cut has not created local branches: " + ", ".join(missing))
    by_id = {app["id"]: app for app in catalog["apps"]}
    local_done = set(read_ids(paths["staging_local"]))
    pushed_done = set(read_ids(paths["staging"]))
    families = []
    for family in catalog["k8s"]:
        images = {}
        for app_id in eligible_ids:
            app = by_id[app_id]
            if app["family"] == family and app["image"]:
                images[app["image"]] = catalog["k8s"][family]["tag_prefix"] + release["date_text"]
        families.append((family, images))
    if all(
        staging_satisfied(family, push, local_done, pushed_done) for family, _images in families
    ):
        for family, images in families:
            if not images:
                print(f"{family}\tskipped-no-eligible-images")
                record_family(
                    paths["staging_local"],
                    local_done,
                    family,
                    f"{family}\tskipped-no-eligible-images",
                )
                if push:
                    record_family(
                        paths["staging"],
                        pushed_done,
                        family,
                        f"{family}\tskipped-no-eligible-images",
                    )
            else:
                label = "already-pushed" if family in pushed_done else "already-created"
                print(f"{family}\t{label}\t{release['branch']}")
                record = paths["staging"] if label == "already-pushed" else paths["staging_local"]
                done = pushed_done if label == "already-pushed" else local_done
                record_family(record, done, family, f"{family}\t{label}\t{release['branch']}")
        sys.stdout.flush()
        print("staging already complete", file=sys.stderr)
        write_release_notes(release_dir, catalog, rows, release["branch"])
        return
    message = f"Set staging image tags for release-{release['date_text']}"
    for family, images in families:
        if not images:
            print(f"{family}\tskipped-no-eligible-images")
            if family not in local_done:
                local_done = record_family(
                    paths["staging_local"],
                    local_done,
                    family,
                    f"{family}\tskipped-no-eligible-images",
                )
            if push and family not in pushed_done:
                pushed_done = record_family(
                    paths["staging"],
                    pushed_done,
                    family,
                    f"{family}\tskipped-no-eligible-images",
                )
            continue
        if staging_satisfied(family, push, local_done, pushed_done):
            label = "already-pushed" if family in pushed_done else "already-created"
            print(f"{family}\t{label}\t{release['branch']}")
            record = paths["staging"] if label == "already-pushed" else paths["staging_local"]
            done = pushed_done if label == "already-pushed" else local_done
            record_family(record, done, family, f"{family}\t{label}\t{release['branch']}")
            continue
        result = prepare_staging_family(
            family, catalog["k8s"][family], release["branch"], images, message, push
        )
        local_done = record_family(
            paths["staging_local"],
            local_done,
            family,
            f"{family}\t{result}\t{release['branch']}",
        )
        if push:
            pushed_done = record_family(
                paths["staging"],
                pushed_done,
                family,
                f"{family}\t{result}\t{release['branch']}",
            )
        print(f"{family}\t{result}\t{release['branch']}")
    write_release_notes(release_dir, catalog, rows, release["branch"])


def is_ancestor(repo, ancestor, descendant):
    if ancestor == descendant:
        return True
    result = git(
        repo,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = (result.stderr or result.stdout or "").strip()
    raise GitError(f"git merge-base --is-ancestor failed in {repo}: {detail}")


def side_counts(repo, left_ref, right_ref):
    result = git(
        repo,
        ["rev-list", "--left-right", "--count", f"{left_ref}...{right_ref}"],
    )
    parts = result.stdout.split()
    if len(parts) != 2:
        die(f"unexpected rev-list count in {repo}: {result.stdout.strip()}")
    return int(parts[0]), int(parts[1])


def status_from_counts(release_only, incoming):
    if release_only == 0 and incoming == 0:
        return "in-sync"
    if release_only == 0:
        return "fast-forward"
    if incoming == 0:
        return "release-ahead"
    return "diverged"


def range_log(repo, left, right):
    result = git(repo, ["log", "--oneline", f"{left}..{right}"], check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise GitError(f"git log failed in {repo}: {detail}")
    text = result.stdout.strip()
    if text:
        return text
    return "(none)"


def require_base(repo, base_ref):
    if base_ref == "origin/staging":
        probe = git(
            repo,
            ["rev-parse", "-q", "--verify", "refs/remotes/origin/staging"],
            check=False,
        )
        if probe.returncode != 0:
            die(f"origin/staging is missing in {repo}")
        return
    git(repo, ["rev-parse", "--verify", base_ref])


def drift_row(kind, name, repo, branch, base_ref):
    require_base(repo, base_ref)
    local = local_sha(repo, branch)
    remote = remote_sha(repo, branch)
    origin = remote if remote else "-"
    row = {
        "kind": kind,
        "id": name,
        "path": str(repo),
        "status": "no-release-branch",
        "release_only": "-",
        "incoming": "-",
        "release_ref": "-",
        "base_ref": base_ref,
        "origin_sha": origin,
        "log_lines": [f"{branch} is not in this clone"],
    }
    if local is None and remote is None:
        return row
    if local and remote and local != remote:
        if not is_ancestor(repo, local, remote) and not is_ancestor(repo, remote, local):
            row["status"] = "local-remote-diverged"
            row["release_ref"] = branch
            row["log_lines"] = [
                f"local {branch} and origin/{branch} have diverged",
                f"git log --oneline {branch}...origin/{branch}",
                oneline_log(repo, branch, f"origin/{branch}"),
            ]
            return row
    if remote:
        release_ref = f"origin/{branch}"
        tip = remote
    else:
        release_ref = branch
        tip = local
    release_only, incoming = side_counts(repo, release_ref, base_ref)
    row["status"] = status_from_counts(release_only, incoming)
    row["release_only"] = str(release_only)
    row["incoming"] = str(incoming)
    row["release_ref"] = release_ref
    row["log_lines"] = [
        f"git log --oneline {release_ref}..{base_ref}",
        range_log(repo, release_ref, base_ref),
        "",
        f"git log --oneline {base_ref}..{release_ref}",
        range_log(repo, base_ref, release_ref),
    ]
    if tip is None:
        die(f"missing release tip for {branch} in {repo}")
    return row


def format_drift_row(row):
    return "\t".join(
        [
            row["kind"],
            row["id"],
            row["status"],
            str(row["release_only"]),
            str(row["incoming"]),
            row["release_ref"],
            row["base_ref"],
            row["origin_sha"],
        ]
    )


def drift_error_row(kind, name, repo, base_ref, message):
    detail = str(message).strip() or "unknown error"
    print(f"error\t{kind}\t{name}\t{' '.join(detail.split())}", file=sys.stderr)
    return {
        "kind": kind,
        "id": name,
        "path": str(repo),
        "status": "error",
        "release_only": "-",
        "incoming": "-",
        "release_ref": "-",
        "base_ref": base_ref or "-",
        "origin_sha": "-",
        "log_lines": [detail],
    }


def collect_drift_repo(kind, name, path, branch, base_ref):
    try:
        require_repo(path)
        git(path, ["fetch", "origin", "--prune"])
        resolved_base = default_ref(path) if base_ref is None else base_ref
        return drift_row(kind, name, path, branch, resolved_base)
    except (GitError, Fatal) as exc:
        return drift_error_row(kind, name, path, base_ref, exc)


def collect_drift(catalog, branch):
    rows = []
    for app in catalog["apps"]:
        rows.append(collect_drift_repo("app", app["id"], app["path"], branch, None))
    for family, entry in catalog["k8s"].items():
        rows.append(
            collect_drift_repo("k8s", family, entry["path"], branch, "origin/staging")
        )
    return rows


def write_drift_files(release_dir, branch, rows, header):
    text = "\n".join(format_drift_row(row) for row in rows) + "\n"
    write_text(release_dir / "state" / "drift.txt", text)
    parts = [branch, header, ""]
    for row in rows:
        parts.append(f"{row['kind']}\t{row['id']}")
        parts.append(row["path"])
        parts.append(row["status"])
        parts.append("")
        parts.extend(row["log_lines"])
        parts.append("")
    write_text(release_dir / "drift-log.txt", "\n".join(parts).rstrip() + "\n")
    return text


def rewrite_notes(release_dir, catalog, release):
    eligible = release_dir / "state" / "eligible.txt"
    branch = release["branch"]
    if eligible.is_file():
        write_release_notes(release_dir, catalog, read_rows(eligible), branch)
        return
    write_text(release_dir / "README.md", folder_guide(release_dir, branch))


def cmd_drift(release_dir, catalog, release):
    branch = release["branch"]
    rows = collect_drift(catalog, branch)
    text = write_drift_files(
        release_dir,
        branch,
        rows,
        "Comparison of the release branch with its base.",
    )
    rewrite_notes(release_dir, catalog, release)
    sys.stdout.write(text)


def checked_out_branch(repo):
    result = git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    if name == "HEAD":
        return None
    return name


def worktree_clean(repo):
    result = git(repo, ["status", "--porcelain"], check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise GitError(f"git status failed in {repo}: {detail}")
    return result.stdout.strip() == ""


def move_local_release(repo, branch, base_ref):
    base_sha = git(repo, ["rev-parse", "--verify", base_ref]).stdout.strip()
    local = local_sha(repo, branch)
    remote = remote_sha(repo, branch)
    if local is None and remote is None:
        die(f"{branch} is missing in {repo}")
    if local is None:
        if not is_ancestor(repo, remote, base_sha):
            die(f"origin/{branch} in {repo} is not an ancestor of {base_ref}")
        git(repo, ["branch", "--no-track", branch, base_sha])
        return remote, base_sha, "fast-forwarded"
    if local == base_sha:
        return local, base_sha, "already-current"
    if not is_ancestor(repo, local, base_sha):
        return local, base_sha, "local-not-contained"
    current = checked_out_branch(repo)
    if current == branch:
        if not worktree_clean(repo):
            die(f"{branch} is checked out in {repo} with local changes; not fast-forwarding")
        git(repo, ["merge", "--ff-only", base_sha])
        return local, base_sha, "fast-forwarded"
    git(repo, ["update-ref", f"refs/heads/{branch}", base_sha, local])
    return local, base_sha, "fast-forwarded"


def push_release_ff(repo, branch):
    local = local_sha(repo, branch)
    remote = remote_sha(repo, branch)
    if local is None:
        die(f"local branch {branch} is missing in {repo}")
    if remote == local:
        return "already-pushed"
    refuse_non_fast_forward(repo, remote, local, branch, branch)
    git(repo, ["push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"])
    return "pushed"


def publish_matching_local(row, branch, local_lines, push_lines):
    repo = row["path"]
    local = local_sha(repo, branch)
    remote = remote_sha(repo, branch)
    base_sha = git(repo, ["rev-parse", "--verify", row["base_ref"]]).stdout.strip()
    if local != base_sha or remote == local:
        return False
    if remote and not is_ancestor(repo, remote, local):
        return False
    label = push_release_ff(repo, branch)
    local_lines.append(f"{row['kind']}\t{row['id']}\talready-current\t{branch}\t{base_sha}")
    line = f"{row['kind']}\t{row['id']}\t{label}\t{branch}\t{base_sha}"
    print(line)
    push_lines.append(line)
    return True


def cmd_refresh(release_dir, catalog, release, push):
    print("mode: push" if push else "mode: dry-run", file=sys.stderr)
    branch = release["branch"]
    before = collect_drift(catalog, branch)
    write_drift_files(
        release_dir,
        branch,
        before,
        "Comparison before refresh moves release branches.",
    )
    local_lines = []
    push_lines = []
    for row in before:
        if row["status"] != "fast-forward":
            if push and row["status"] == "in-sync" and publish_matching_local(
                row, branch, local_lines, push_lines
            ):
                continue
            line = f"{row['kind']}\t{row['id']}\tskipped\t{row['status']}"
            print(line)
            local_lines.append(line)
            continue
        old, new, action = move_local_release(row["path"], branch, row["base_ref"])
        if action == "local-not-contained":
            line = f"{row['kind']}\t{row['id']}\tskipped\tlocal-not-contained"
            print(line)
            local_lines.append(line)
            continue
        if action == "fast-forwarded":
            local_lines.append(
                f"{row['kind']}\t{row['id']}\tfast-forwarded\t{branch}\t{old}\t{new}"
            )
        else:
            local_lines.append(
                f"{row['kind']}\t{row['id']}\talready-current\t{branch}\t{new}"
            )
        if push:
            label = push_release_ff(row["path"], branch)
            line = f"{row['kind']}\t{row['id']}\t{label}\t{branch}\t{new}"
            print(line)
            push_lines.append(line)
        else:
            print(local_lines[-1])
    write_text(
        release_dir / "state" / "refresh-local.txt",
        "".join(f"{line}\n" for line in local_lines),
    )
    if push:
        write_text(
            release_dir / "state" / "refresh.txt",
            "".join(f"{line}\n" for line in push_lines),
        )
    after = collect_drift(catalog, branch)
    write_text(
        release_dir / "state" / "drift.txt",
        "\n".join(format_drift_row(row) for row in after) + "\n",
    )
    rewrite_notes(release_dir, catalog, release)


def resolve_commit(repo, sha):
    result = git(repo, ["rev-parse", "--verify", f"{sha}^{{commit}}"], check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "ambiguous" in detail.lower():
            die(f"ambiguous commit {sha} in {repo}")
        return None
    return result.stdout.strip()


def commit_subject(repo, sha):
    subject = git(repo, ["log", "-1", "--format=%s", sha]).stdout.strip()
    return subject.replace("\t", " ")


def locate_picks(catalog, shas):
    located = []
    seen = set()
    for raw in shas:
        matches = []
        for app in catalog["apps"]:
            resolved = resolve_commit(app["path"], raw)
            if resolved:
                matches.append(("app", app["id"], app["path"], resolved))
        for family, entry in catalog["k8s"].items():
            resolved = resolve_commit(entry["path"], raw)
            if resolved:
                matches.append(("k8s", family, entry["path"], resolved))
        if not matches:
            die(f"commit not found in any catalog repo: {raw}")
        hashes = {item[3] for item in matches}
        if len(hashes) != 1:
            die(f"commit {raw} resolves to different objects in multiple repos")
        paths = {item[2] for item in matches}
        if len(paths) != 1:
            names = ", ".join(sorted(item[1] for item in matches))
            die(f"commit {raw} is present in multiple catalog repos: {names}")
        kind, name, path, resolved = matches[0]
        if resolved in seen:
            continue
        seen.add(resolved)
        located.append((kind, name, path, resolved))
    return located


def group_picks(located):
    groups = []
    index = {}
    for kind, name, path, sha in located:
        key = str(path)
        if key not in index:
            index[key] = len(groups)
            groups.append({"kind": kind, "id": name, "path": path, "shas": []})
        groups[index[key]]["shas"].append(sha)
    return groups


def order_on_base(repo, base_ref, shas):
    order = git(repo, ["rev-list", "--reverse", base_ref]).stdout.split()
    position = {sha: index for index, sha in enumerate(order)}
    missing = [sha for sha in shas if sha not in position]
    if missing:
        die(f"commits not on {base_ref} in {repo}: {', '.join(missing)}")
    return sorted(shas, key=lambda sha: position[sha])


def ensure_local_release(repo, branch):
    if local_sha(repo, branch):
        return
    remote = remote_sha(repo, branch)
    if remote is None:
        die(f"{branch} is missing in {repo}; run cut before picks")
    git(repo, ["branch", "--no-track", branch, remote])


def cherry_pick_message(result):
    return ((result.stderr or "") + "\n" + (result.stdout or "")).strip()


def cherry_pick_empty(detail):
    text = detail.lower()
    return "now empty" in text or "empty commit" in text


def apply_picks(repo, branch, shas):
    records = []
    ensure_local_release(repo, branch)
    work = Path(tempfile.mkdtemp(prefix="release-pick-"))
    own_worktree = False
    try:
        added = git(repo, ["worktree", "add", str(work), branch], check=False)
        if added.returncode == 0:
            own_worktree = True
            pick_dir = work
        else:
            detail = cherry_pick_message(added)
            shutil.rmtree(work, ignore_errors=True)
            work = None
            if checked_out_branch(repo) != branch:
                die(f"cannot check out {branch} in {repo}: {detail}")
            if not worktree_clean(repo):
                die(f"{branch} is checked out in {repo} with local changes; not cherry-picking")
            pick_dir = repo
        for sha in shas:
            subject = commit_subject(repo, sha)
            if is_ancestor(repo, sha, branch):
                records.append((sha, "already-applied", subject, ""))
                continue
            result = git(pick_dir, ["cherry-pick", sha], check=False)
            if result.returncode != 0:
                detail = cherry_pick_message(result)
                git(pick_dir, ["cherry-pick", "--abort"], check=False)
                if cherry_pick_empty(detail):
                    records.append((sha, "already-applied", subject, ""))
                    continue
                git(pick_dir, ["reset", "--hard", "HEAD"], check=False)
                records.append((sha, "conflict", subject, detail))
                return records
            records.append((sha, "applied", subject, ""))
        return records
    finally:
        if own_worktree and work is not None:
            git(repo, ["worktree", "remove", "--force", str(work)], check=False)
            shutil.rmtree(work, ignore_errors=True)
            git(repo, ["worktree", "prune"], check=False)


def write_pick_lines(path, lines):
    write_text(path, "".join(f"{line}\n" for line in lines))


def cmd_picks(release_dir, catalog, release, push, shas):
    print("mode: push" if push else "mode: dry-run", file=sys.stderr)
    branch = release["branch"]
    for app in catalog["apps"]:
        require_repo(app["path"])
        git(app["path"], ["fetch", "origin", "--prune"])
    for entry in catalog["k8s"].values():
        require_repo(entry["path"])
        git(entry["path"], ["fetch", "origin", "--prune"])
    groups = group_picks(locate_picks(catalog, shas))
    pick_path = release_dir / "state" / "picks.txt"
    lines = []
    for group in groups:
        repo = group["path"]
        if group["kind"] == "k8s":
            base_ref = "origin/staging"
        else:
            base_ref = default_ref(repo)
        require_base(repo, base_ref)
        ensure_local_release(repo, branch)
        pending = []
        for sha in group["shas"]:
            subject = commit_subject(repo, sha)
            if is_ancestor(repo, sha, branch):
                line = f"{group['kind']}\t{group['id']}\t{sha}\talready-applied\t{subject}"
                print(line)
                lines.append(line)
                write_pick_lines(pick_path, lines)
                continue
            if not is_ancestor(repo, sha, base_ref):
                die(f"{sha} is not on {base_ref} in {repo}")
            pending.append(sha)
        ordered = order_on_base(repo, base_ref, pending) if pending else []
        records = apply_picks(repo, branch, ordered) if ordered else []
        failed = None
        for sha, status, subject, detail in records:
            line = f"{group['kind']}\t{group['id']}\t{sha}\t{status}\t{subject}"
            print(line)
            lines.append(line)
            write_pick_lines(pick_path, lines)
            if status == "conflict":
                failed = (sha, detail)
                break
        if failed is not None:
            rows = collect_drift(catalog, branch)
            write_drift_files(
                release_dir,
                branch,
                rows,
                "Comparison after picks stopped on a conflict.",
            )
            rewrite_notes(release_dir, catalog, release)
            die(f"cherry-pick {failed[0]} failed in {repo}: {failed[1]}")
        if push:
            label = push_release_ff(repo, branch)
            print(f"{group['kind']}\t{group['id']}\t{label}\t{branch}")
    rows = collect_drift(catalog, branch)
    write_drift_files(
        release_dir,
        branch,
        rows,
        "Comparison after picks.",
    )
    rewrite_notes(release_dir, catalog, release)


def usage():
    die(
        "usage: release.sh <YYYY-MM-DD> <eligible|cut|staging|drift|refresh|picks> "
        "[--push] [--sha <commit>]"
    )


def parse_args(argv):
    push = False
    shas = []
    args = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--push":
            if push:
                usage()
            push = True
            index += 1
            continue
        if arg == "--sha":
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                usage()
            shas.append(argv[index + 1])
            index += 2
            continue
        if arg.startswith("-"):
            usage()
        args.append(arg)
        index += 1
    if len(args) != 2:
        usage()
    date_text, step = args
    if step not in ("eligible", "cut", "staging", "drift", "refresh", "picks"):
        usage()
    if shas and step != "picks":
        die("--sha applies to picks")
    if step == "picks" and not shas:
        die("picks requires at least one --sha")
    if push and step in ("eligible", "drift"):
        die(f"--push does not apply to {step}")
    return date_text, step, push, shas


def main(argv):
    date_text, step, push, shas = parse_args(argv)
    parse_date(date_text)
    release_dir = ensure_release_dir(date_text)
    release = parse_release_env(release_dir / "release.env", date_text)
    catalog = load_catalog()
    if step == "eligible":
        cmd_eligible(release_dir, catalog, release)
    elif step == "cut":
        cmd_cut(release_dir, catalog, release, push)
    elif step == "staging":
        cmd_staging(release_dir, catalog, release, push)
    elif step == "drift":
        cmd_drift(release_dir, catalog, release)
    elif step == "refresh":
        cmd_refresh(release_dir, catalog, release, push)
    else:
        cmd_picks(release_dir, catalog, release, push, shas)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (GitError, Fatal) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
