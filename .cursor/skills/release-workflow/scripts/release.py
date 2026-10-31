#!/usr/bin/env python3
"""Prepare staging release branches from local clones."""

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
        "staging": state / "staging.txt",
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
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


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
    if paths["cut"].exists() or paths["staging"].exists():
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


def push_release_branch(repo, base_ref, branch):
    base_sha = git(repo, ["rev-parse", base_ref]).stdout.strip()
    existing = remote_sha(repo, branch)
    if existing == base_sha:
        return base_sha
    if existing:
        ancestor = git(
            repo,
            ["merge-base", "--is-ancestor", existing, base_sha],
            check=False,
        )
        if ancestor.returncode != 0:
            die(
                f"origin/{branch} in {repo} is not an ancestor of {base_ref}; "
                "not force-pushing"
            )
    git(repo, ["push", "origin", f"{base_ref}:refs/heads/{branch}"])
    return base_sha


def cmd_cut(release_dir, catalog, release):
    paths = state_paths(release_dir)
    if not paths["eligible"].is_file():
        die("run eligible before cut")
    rows = read_rows(paths["eligible"])
    by_id = {app["id"]: app for app in catalog["apps"]}
    recorded = read_ids(paths["cut"])
    done = set(recorded)
    eligible = [row for row in rows if row["status"] == "eligible"]
    branch = f"release-{release['date_text']}"
    if eligible and all(row["id"] in done for row in eligible):
        for row in eligible:
            print(f"{row['id']}\talready-pushed\t{branch}")
        sys.stdout.flush()
        print("cut already complete", file=sys.stderr)
        return
    for row in eligible:
        if row["id"] in done:
            print(f"{row['id']}\talready-pushed\t{branch}")
            continue
        app = by_id.get(row["id"])
        if app is None:
            die(f"{row['id']} is in state/eligible.txt and not in the catalog")
        require_repo(app["path"])
        git(app["path"], ["fetch", "origin", "--prune"])
        base = default_ref(app["path"])
        sha = push_release_branch(app["path"], base, branch)
        recorded.append(row["id"])
        done.add(row["id"])
        write_text(paths["cut"], "".join(f"{item}\n" for item in recorded))
        print(f"{row['id']}\tpushed\t{branch}\t{sha}")


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


def push_staging_family(family, entry, branch, tags, message):
    repo = entry["path"]
    require_repo(repo)
    git(repo, ["fetch", "origin", "--prune"])
    staging = git(repo, ["rev-parse", "-q", "--verify", "refs/remotes/origin/staging"], check=False)
    if staging.returncode != 0:
        die(f"origin/staging is missing in {repo}")
    if remote_sha(repo, branch):
        die(f"origin/{branch} already exists in {repo}; not overwriting it")
    local = git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False)
    if local.returncode == 0:
        die(f"local branch {branch} already exists in {repo}")
    original = overlay_text(repo, entry["overlay"])
    updated = update_overlay(original, tags)
    if updated == original or updated == original + "\n" or original == updated + "\n":
        return "unchanged-tags"
    work = Path(tempfile.mkdtemp(prefix=f"release-{family}-"))
    pushed = False
    try:
        git(repo, ["worktree", "add", "-b", branch, str(work), "origin/staging"])
        target = work / entry["overlay"]
        if not target.is_file():
            die(f"overlay missing in {repo}: {entry['overlay']}")
        target.write_text(updated)
        git(work, ["add", "--", entry["overlay"]])
        git(work, ["commit", "-m", message])
        git(work, ["push", "-u", "origin", branch])
        pushed = True
    finally:
        git(repo, ["worktree", "remove", "--force", str(work)], check=False)
        shutil.rmtree(work, ignore_errors=True)
        git(repo, ["worktree", "prune"], check=False)
        if not pushed:
            exists = git(
                repo,
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            )
            if exists.returncode == 0:
                git(repo, ["branch", "-D", branch], check=False)
    return "pushed"


def cmd_staging(release_dir, catalog, release):
    paths = state_paths(release_dir)
    if not paths["eligible"].is_file():
        die("run eligible before staging")
    rows = read_rows(paths["eligible"])
    eligible_ids = [row["id"] for row in rows if row["status"] == "eligible"]
    cut_ids = set(read_ids(paths["cut"]))
    missing = [item for item in eligible_ids if item not in cut_ids]
    if missing:
        die("cut has not pushed: " + ", ".join(missing))
    by_id = {app["id"]: app for app in catalog["apps"]}
    done = set(read_ids(paths["staging"]))
    families = []
    for family in catalog["k8s"]:
        images = {}
        for app_id in eligible_ids:
            app = by_id[app_id]
            if app["family"] == family and app["image"]:
                images[app["image"]] = catalog["k8s"][family]["tag_prefix"] + release["date_text"]
        families.append((family, images))
    if all(family in done for family, _images in families):
        for family, images in families:
            if not images:
                print(f"{family}\tskipped-no-eligible-images")
            else:
                print(f"{family}\talready-pushed\t{release['branch']}")
        sys.stdout.flush()
        print("staging already complete", file=sys.stderr)
        return
    message = f"Set staging image tags for release-{release['date_text']}"
    for family, images in families:
        if family in done:
            print(f"{family}\talready-pushed\t{release['branch']}")
            continue
        if not images:
            print(f"{family}\tskipped-no-eligible-images")
            done.add(family)
            write_text(paths["staging"], "".join(f"{item}\n" for item in done))
            continue
        result = push_staging_family(
            family, catalog["k8s"][family], release["branch"], images, message
        )
        done.add(family)
        write_text(paths["staging"], "".join(f"{item}\n" for item in done))
        print(f"{family}\t{result}\t{release['branch']}")


def usage():
    die(
        "usage: release.sh <YYYY-MM-DD> <eligible|cut|staging>"
    )


def main(argv):
    if len(argv) != 2:
        usage()
    date_text = argv[0]
    step = argv[1]
    if step not in ("eligible", "cut", "staging"):
        usage()
    parse_date(date_text)
    release_dir = ensure_release_dir(date_text)
    release = parse_release_env(release_dir / "release.env", date_text)
    catalog = load_catalog()
    if step == "eligible":
        cmd_eligible(release_dir, catalog, release)
    elif step == "cut":
        cmd_cut(release_dir, catalog, release)
    else:
        cmd_staging(release_dir, catalog, release)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except GitError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
