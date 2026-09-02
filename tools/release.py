"""Package and publish a PopTracker pack release for auto-update.

PopTracker reads manifest.json -> versions_url -> versions.json, takes the top
entry as the latest, offers it when its package_version is newer than the
installed one, then downloads download_url and verifies sha256. A release is
therefore three things that have to agree: a tag holding the zip, a versions.json
entry naming it, and a manifest carrying the same version.

Build only, which touches no git and no network, to inspect the zip:

    py -3.12 tools/release.py --changelog "First line" "Second line"

Full publish, which bumps the version, builds, commits, pushes and releases:

    py -3.12 tools/release.py --version 0.2.0 --changelog "..." --publish

--publish runs, in order: the checks that the release can land where PopTracker
reads from, the pack self-test, the zip and its hash, versions.json, a commit of
manifest.json and versions.json, a push, then `gh release create v<version>`
with the zip as its asset so download_url resolves. It needs git and the gh CLI
on PATH.

Manual release, without the gh CLI:

    1. py -3.12 tools/release.py --version <version> --changelog "..."
       which bumps manifest.json, builds the zip and writes versions.json
    2. git commit manifest.json versions.json -m "Release v<version>."
       git push
    3. On GitHub, create a release at a new tag v<version> targeting the branch
       versions_url reads from, and upload the built zip as its asset.
       versions.json already points download_url at that tag, so it resolves as
       soon as the asset is there.

The owner, the repository and the branch all come from the manifest's
versions_url, and the zip is named after that repository, so there is one answer
to where a release lives and no way to publish to a branch PopTracker does not
read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent

# Runtime pack content, with manifest.json at the zip root. The generator's input
# tables, the tools that ran it, the preview renders and the lint config are dev
# tooling and stay out. var_horizontal carries the horizontal variant's one
# layout, so a release without it ships a variant with no window of its own.
INCLUDE = ["manifest.json", "README.md", "LICENSE", "images", "items", "layouts",
           "locations", "maps", "scripts", "var_horizontal"]

# The pack's own gate. It cross-checks the generated files against each other and
# every pin against the map image, so it fails a release the way PopTracker would
# fail it at runtime, all at once instead of one problem per launch.
SELF_TEST = PACK / "tools" / "check_logic.py"

# Where PopTracker looks for the version list, and the one place the owner, the
# repository and the branch are read from.
VERSIONS_URL = re.compile(
    r"^https://raw\.githubusercontent\.com/([^/]+/[^/]+)/([^/]+)/versions\.json$")

# What a version may look like, since PopTracker compares these number by number
# and a tag shaped "v0.2.0" never comes out newer than what is installed.
VERSION = re.compile(r"^\d+(\.\d+)*$")

# Every image the pack draws is named in a JSON value, so this finds them all.
# The capture stops at the extension rather than the closing quote, so a path
# carrying an image modifier after it still resolves to the file itself.
IMAGE_REFERENCE = re.compile(r'"(images/[^"]*?\.png)')


def manifest_path() -> Path:
    return PACK / "manifest.json"


def manifest() -> dict:
    return json.loads(manifest_path().read_text(encoding="utf-8"))


def release_target() -> tuple[str, str]:
    """Answers the owner/repository and the branch that versions_url points at."""
    url = manifest().get("versions_url")
    if not url:
        sys.exit("error: manifest.json declares no versions_url, so auto-update has nothing to read")
    match = VERSIONS_URL.match(url)
    if not match:
        sys.exit(f"error: versions_url is {url!r}, which is not a raw.githubusercontent.com path of the "
                 "form https://raw.githubusercontent.com/<owner>/<repository>/<branch>/versions.json")
    return match.group(1), match.group(2)


def set_manifest_version(version: str) -> None:
    """Writes package_version, leaving the rest of the manifest's text alone."""
    if not VERSION.match(version):
        sys.exit(f"error: {version!r} is not a version. PopTracker compares these number by number, "
                 "so write 0.2.0 rather than v0.2.0.")
    path = manifest_path()
    text = path.read_text(encoding="utf-8")
    field = re.compile(r'("package_version"\s*:\s*")[^"]*(")')
    if field.search(text) is None:
        sys.exit("error: could not find package_version in manifest.json")
    # Unchanged when the manifest already carries this version, which is a
    # release of what is there rather than a mistake.
    bumped = field.sub(rf"\g<1>{version}\g<2>", text)
    if bumped != text:
        path.write_text(bumped, encoding="utf-8")


def manifest_version() -> str:
    return manifest()["package_version"]


def run_self_test() -> None:
    """Runs the pack's own gate and refuses to build a pack that fails it."""
    if not SELF_TEST.is_file():
        sys.exit(f"error: the pack has no {SELF_TEST.name}, which gates every release")
    # Flushed, since the gate writes its own report to the same terminal.
    print(f"Self-test  {SELF_TEST.name}", flush=True)
    result = subprocess.run([sys.executable, str(SELF_TEST)], cwd=PACK, check=False)
    if result.returncode != 0:
        sys.exit(f"error: {SELF_TEST.name} failed, so the pack is not releasable. Fix the problems it "
                 "named, or pass --no-self-test to package anyway.")


def packaged_files() -> list[tuple[Path, str]]:
    """Lists every file the zip holds, as its path and its name inside the zip."""
    packaged: list[tuple[Path, str]] = []
    for name in INCLUDE:
        path = PACK / name
        if path.is_file():
            packaged.append((path, name))
        elif path.is_dir():
            for root, directories, files in os.walk(path):
                directories[:] = [directory for directory in directories if directory != "__pycache__"]
                for file_name in sorted(files):
                    file_path = Path(root) / file_name
                    packaged.append((file_path, file_path.relative_to(PACK).as_posix()))
        else:
            sys.exit(f"error: the pack has no {name}, which the release cannot do without")
    return packaged


def missing_images(packaged: list[tuple[Path, str]]) -> list[str]:
    """Names every image the packaged JSON draws on that the zip would not hold.

    The art is generated rather than written, and the part of it taken out of the
    player's own game install is not committed, so a checkout can package a pack
    whose panels and map draw nothing. No Lua file names an image, so the JSON
    holds every reference there is.
    """
    names = {name for _, name in packaged}
    referenced: set[str] = set()
    for path, name in packaged:
        if name.endswith(".json"):
            referenced.update(IMAGE_REFERENCE.findall(path.read_text(encoding="utf-8")))
    return sorted(referenced - names)


def build_zip(version: str, repository: str, packaged: list[tuple[Path, str]]) -> Path:
    """Writes the release zip, named after the repository the release lives in."""
    dist = PACK / "dist"
    dist.mkdir(exist_ok=True)
    zip_path = dist / f"{repository.split('/')[1]}_v{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, name in packaged:
            archive.write(path, name)
    return zip_path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def update_versions(version: str, repository: str, zip_name: str, digest: str,
                    changelog: list[str]) -> Path:
    """Puts this version at the top of versions.json, where the latest lives."""
    path = PACK / "versions.json"
    data = {"versions": []}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    entry = {
        "package_version": version,
        "download_url": f"https://github.com/{repository}/releases/download/v{version}/{zip_name}",
        "sha256": digest,
        "changelog": changelog or [],
    }
    # Newest first, replacing any entry this version already has.
    data["versions"] = [known for known in data["versions"]
                        if known.get("package_version") != version]
    data["versions"].insert(0, entry)
    path.write_text(json.dumps(data, indent="\t") + "\n", encoding="utf-8")
    return path


def run(*command: str) -> None:
    print("  $", " ".join(command))
    subprocess.run(command, cwd=PACK, check=True)


def git_output(*command: str) -> str | None:
    """Answers a git command's output, or None when the command itself failed."""
    result = subprocess.run(("git", *command), cwd=PACK, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def check_publish_target(repository: str, branch: str) -> None:
    """Refuses a publish that would not land where PopTracker reads from.

    Runs before the version is bumped, so a refusal leaves the checkout as it was.
    """
    if shutil.which("git") is None or shutil.which("gh") is None:
        sys.exit("error: --publish needs both git and the gh CLI on PATH")
    current = git_output("rev-parse", "--abbrev-ref", "HEAD")
    if current is None:
        sys.exit(f"error: {PACK} is not a git checkout, so there is nothing to publish from")
    if current != branch:
        sys.exit(f"error: the checkout is on {current}, but versions_url reads versions.json from "
                 f"{branch}, so PopTracker would never see this release. Publish from {branch}, or "
                 f"point versions_url at {current}.")
    remote = git_output("remote", "get-url", "origin")
    if remote is None:
        sys.exit("error: the checkout has no origin remote, so the release has nowhere to go. "
                 f"git remote add origin https://github.com/{repository}.git")
    if repository.lower() not in remote.lower():
        sys.exit(f"error: origin is {remote}, but versions_url names {repository}, so the zip and the "
                 "version list would land in different repositories")
    release_owned = ("manifest.json", "versions.json")
    dirty = [line for line in (git_output("status", "--porcelain") or "").splitlines()
             if line[3:] not in release_owned]
    if dirty:
        sys.exit("error: the checkout holds changes the zip would ship and the tag would not:\n  "
                 + "\n  ".join(dirty)
                 + "\nCommit or stash them first, since the zip is built from the working tree.")


def publish(version: str, repository: str, zip_path: Path, changelog: list[str]) -> None:
    tag = f"v{version}"
    notes = "\n".join(changelog) if changelog else f"Release {tag}."
    pending = git_output("status", "--porcelain", "--", "manifest.json", "versions.json")
    if pending:
        run("git", "commit", "manifest.json", "versions.json", "-m", f"Release {tag}.")
    else:
        print("  manifest.json and versions.json are already committed as they stand")
    run("git", "push")
    # Creates the tag at the pushed HEAD, the release, and uploads the asset.
    run("gh", "release", "create", tag, str(zip_path),
        "--repo", repository, "--title", tag, "--notes", notes)


def main() -> int:
    parser = argparse.ArgumentParser(description="Package and publish a PopTracker pack release.")
    parser.add_argument("--version", help="set package_version in manifest.json first")
    parser.add_argument("--changelog", nargs="*", default=[],
                        help="changelog lines for this version")
    parser.add_argument("--publish", action="store_true",
                        help="commit, push, and create the GitHub release (needs git and gh)")
    parser.add_argument("--no-self-test", dest="self_test", action="store_false",
                        help="package without running tools/check_logic.py first")
    arguments = parser.parse_args()

    repository, branch = release_target()
    if arguments.publish:
        check_publish_target(repository, branch)
    if arguments.self_test:
        run_self_test()

    if arguments.version:
        set_manifest_version(arguments.version)
    version = manifest_version()

    packaged = packaged_files()
    missing = missing_images(packaged)
    if missing:
        print(f"error: {len(missing)} image(s) the pack draws are not in the checkout:")
        for name in missing:
            print(f"  {name}")
        sys.exit("Run the build steps the README lists, since a release cannot ship art it does not have.")

    zip_path = build_zip(version, repository, packaged)
    digest = sha256(zip_path)
    update_versions(version, repository, zip_path.name, digest, arguments.changelog)

    print(f"Built  {zip_path}  ({zip_path.stat().st_size // 1024} KB, {len(packaged)} files)")
    print(f"sha256 {digest}")
    print(f"Wrote  versions.json (latest = {version})")

    tag = f"v{version}"
    if arguments.publish:
        print(f"\nPublishing {tag} to {repository} on {branch}:")
        publish(version, repository, zip_path, arguments.changelog)
        print(f"\nPublished {tag}.")
        return 0

    print(f"\nDry run (no --publish). Two ways to release {tag}:")
    print("  A) re-run with --publish   (needs git and the gh CLI)")
    print("  B) manual (no gh):")
    print(f'       git commit manifest.json versions.json -m "Release {tag}."')
    print(f"       git push origin {branch}")
    print(f"       then create a GitHub release at a new tag {tag} (target {branch})")
    print(f"       and upload the built zip ({zip_path.name}) as the asset:")
    print(f"         https://github.com/{repository}/releases/new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
