#!/usr/bin/env python3
"""Prepare staging release branches from local clones.

The default creates local branches and does not push. Pass --push to publish them.
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


def die(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


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
        die(f"catalog not found: {path}")
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
    print(f"wrote {delete_path}", file=sys.stderr)
    print(f"wrote {log_path}", file=sys.stderr)


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


def usage():
    die("usage: release.sh <YYYY-MM-DD> <eligible|cut|staging> [--push]")


def parse_args(argv):
    push = False
    args = []
    for arg in argv:
        if arg == "--push":
            if push:
                usage()
            push = True
            continue
        if arg.startswith("-"):
            usage()
        args.append(arg)
    if len(args) != 2:
        usage()
    date_text, step = args
    if step not in ("eligible", "cut", "staging"):
        usage()
    if push and step == "eligible":
        die("--push does not apply to eligible")
    return date_text, step, push


def main(argv):
    date_text, step, push = parse_args(argv)
    parse_date(date_text)
    release_dir = ensure_release_dir(date_text)
    release = parse_release_env(release_dir / "release.env", date_text)
    catalog = load_catalog()
    if step == "eligible":
        cmd_eligible(release_dir, catalog, release)
    elif step == "cut":
        cmd_cut(release_dir, catalog, release, push)
    else:
        cmd_staging(release_dir, catalog, release, push)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except GitError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
