"""Measure a submission's two static axes: S (source size) and D (dependency weight).

    python3 harness/static_axes.py --track-dir ./track-a [--tools-image TAG]
                                   [--check-registry] [--json out.json]
    python3 harness/static_axes.py --reference      # rewrites harness/reference_axes.json

S is the number of code lines scc counts (no blank lines, no comments) after each
file has been through its language's canonical formatter. It covers every file in
the track directory except registry packages first published before 2026-10-01
by people outside the team, which count only in D.

How this script applies that rule:

  * The submission is never modified. It is copied, the copy is formatted, the copy
    is counted. scc and the formatters run in the tools image built from
    harness/tools/Dockerfile, with no network and a read-only root filesystem.
  * Each formatter runs with its defaults on one file at a time. Configuration in
    the submission (rustfmt.toml, .clang-format, .prettierrc, ruff.toml,
    fourmolu.yaml, .editorconfig, ignore files) has no effect. A file its formatter
    rejects is counted as written, and the notes say which. Languages with no
    formatter in the image are counted as written.
  * Documentation and data formats are not code (PROSE_LANGUAGES, DATA_LANGUAGES):
    PERF.md, README.md, manifests, lockfiles and committed data cost nothing.
    Every file that is not counted is listed under not_counted with its size, so
    code hidden in one stands out.
  * A vendored package is left out only when its own metadata shows it came from a
    registry (find_vendored_packages): an npm package in node_modules, a crate
    that `cargo vendor` wrote with a registry checksum, a module in a Go vendor/
    directory, a Python package installed with its .dist-info, a Hackage package
    unpacked under a vendor directory. A crate vendored from git, a module
    replaced by a local directory, or code with no package metadata counts,
    wherever it lives. Every excluded package is listed under excluded_paths.
  * D is computed in dependencies.py from the manifests in the track directory.

--check-registry adds the organiser's spot-check: the first-publish date of every
direct dependency and every excluded package, flagging any first published on or
after 2026-10-01. That needs the network; the measurement itself never does.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

if sys.version_info < (3, 11):
    sys.exit("static_axes.py needs Python 3.11 or newer (it reads TOML with tomllib); try python3.13")

import tomllib  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dependencies  # noqa: E402
from dependencies import GIT, PATH, REGISTRY, URL, Dependency, inside, read_json, read_toml, rel  # noqa: E402

ROOT = os.path.dirname(HERE)
TOOLS_IMAGE = "speeddemon/static-tools:2026-10-28"
BUILD_COMMAND = f"docker build -t {TOOLS_IMAGE} harness/tools"
REFERENCE_FILES = {"a": "a_ingest.py", "b": "b_index.py", "c": "c_join.py", "d": "d_traverse.py"}
REFERENCE_AXES = os.path.join(HERE, "reference_axes.json")

# scc languages whose lines are not code. Their files are listed under not_counted.
PROSE_LANGUAGES = {
    "Markdown", "MDX", "Plain Text", "License", "ReStructuredText", "AsciiDoc", "Org", "Textile",
    "Creole", "Rich Text Format", "SPDX", "TeX", "LaTeX", "Typst", "TaskPaper",
}
DATA_LANGUAGES = {
    "JSON", "JSON5", "JSONC", "JSONL", "YAML", "TOML", "XML", "CSV", "INI", "Properties File", "SVG",
    "HEX", "Intel HEX", "MSBuild", "Cabal", "nuspec", "gitignore", "ignore", "Docker ignore",
    "CloudFormation (JSON)", "CloudFormation (YAML)", "Bitbucket Pipeline", "Systemd", "Xcode Config",
    "Godot Scene", "Game Maker Project", "Avro",
}
NOT_CODE = PROSE_LANGUAGES | DATA_LANGUAGES
# Lockfiles are written by the package manager. They are data whatever scc makes of them.
LOCKFILE_NAMES = {
    "Cargo.lock", "go.sum", "package-lock.json", ".package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "bun.lock", "bun.lockb", "deno.lock", "poetry.lock", "uv.lock", "pdm.lock",
    "Pipfile.lock", "packages.lock.json", "Package.resolved", "gradle.lockfile", "cabal.project.freeze",
    "stack.yaml.lock", "flake.lock", "Gemfile.lock", "composer.lock", "mix.lock", "pubspec.lock", "Podfile.lock",
}

# The formatter for each scc language, and the command that runs it. The file
# arrives on stdin, its path is "$1", and the formatted file goes to stdout.
FORMATTER_FOR_LANGUAGE = {
    "Rust": "rustfmt",
    "Go": "gofmt",
    "C": "clang-format", "C Header": "clang-format", "C++": "clang-format", "C++ Header": "clang-format",
    "Python": "ruff",
    "Java": "google-java-format",
    "JavaScript": "prettier", "JSX": "prettier", "TypeScript": "prettier", "TypeScript Typings": "prettier",
    "Haskell": "fourmolu",
}
CONFIG_DIR = "/usr/local/share/static-tools"
FORMATTER_COMMANDS = {
    "rustfmt": f"rustfmt --config-path {CONFIG_DIR}/rustfmt.toml --edition {{edition}}",
    "gofmt": "gofmt",
    "clang-format": 'clang-format --style=LLVM --assume-filename="$1"',
    "ruff": 'ruff format --isolated --no-cache --stdin-filename "$1" -',
    "google-java-format": "google-java-format -",
    # --with-node-modules: otherwise prettier echoes any file under node_modules unchanged.
    "prettier": 'prettier --no-config --no-editorconfig --ignore-path /dev/null --with-node-modules '
                '--stdin-filepath "$1"',
    "fourmolu": f'fourmolu --quiet --config {CONFIG_DIR}/fourmolu.yaml --stdin-input-file "$1"',
}
RUST_EDITIONS = {"2015", "2018", "2021", "2024"}
DEFAULT_RUST_EDITION = "2021"   # for a .rs file with no Cargo.toml above it
# clang-format reads these and no flag turns that off. They are not code, so they
# are dropped from the copy before formatting and the count does not change.
FORMATTER_CONTROL_FILES = {".clang-format-ignore"}
FORMAT_TIMEOUT_S = 120
# gofmt and fourmolu never wrap a long line, and gofmt keeps several statements
# on one line when they were written that way. Lines this long after formatting,
# and Go lines holding more than one statement, are flagged for the organiser.
LONG_LINE_CHARS = 200
# Directories that usually hold vendored code. Counted code in one of them (code
# that is not an identified registry package) gets a note.
VENDOR_DIR_NAMES = {"vendor", "vendored", "third_party", "third-party", "thirdparty", "external", "node_modules"}

# Runs one formatter over a list of files inside the tools container. A file is
# replaced only when the formatter exits 0 and prints something (or the file was
# empty to begin with), so a failing formatter can never lose code.
FORMAT_LOOP = r"""
list=$1 status=$2 command=$3
n=0
while IFS= read -r file; do
  n=$((n + 1))
  timeout -k 5 "$TIMEOUT" sh -c "$command" sh "$file" <"$file" >/tmp/formatted 2>"$status.$n.err"
  code=$?
  if [ "$code" -eq 0 ] && [ ! -s /tmp/formatted ] && [ -s "$file" ]; then
    code="0, but it printed nothing"
  fi
  if [ "$code" = 0 ]; then
    cat /tmp/formatted >"$file"
  fi
  printf '%s\t%s\n' "$code" "$file" >>"$status"
done <"$list"
"""

SCC_ARGS = ["scc", "--no-config", "--no-gitignore", "--no-ignore", "--no-scc-ignore", "--no-gitmodule",
            "--no-cocomo", "--no-size", "--no-complexity", "--by-file", "--format", "json", "/work/src"]


class ToolError(Exception):
    """Docker, the tools image or the work directory is not usable."""


# ------------------------------------------------------------------ vendored registry packages

class Package:
    """A package a package manager vendored into the track. registry says whether it
    came from a registry; only those are left out of S. roots are the directories
    (or files) it occupies, relative to the track. It owns either every file under
    its roots, or exactly the files its metadata lists (files), or, for Go, the
    files directly in its package directories (package_dirs)."""

    def __init__(self, ecosystem, name, version, roots, evidence, files=None, package_dirs=None, registry=True):
        self.ecosystem, self.name, self.version = ecosystem, name, version
        self.roots, self.evidence = sorted(roots), evidence
        self.files, self.package_dirs = files, package_dirs
        self.registry = registry

    def label(self):
        return f"{self.name} {self.version}".strip()


def find_vendored_packages(track_dir):
    """Every package a package manager vendored into the track, from its own metadata."""
    packages = []
    for dirpath, dirnames, filenames in os.walk(track_dir):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        here = rel(dirpath, track_dir)
        parents = here.split("/")[:-1] if here != "." else []
        if os.path.basename(dirpath) == "node_modules":
            packages += npm_packages(dirpath, track_dir)
        if ".cargo-checksum.json" in filenames and "Cargo.toml" in filenames:
            packages += cargo_package(dirpath, track_dir)
        if os.path.basename(dirpath) == "vendor" and "modules.txt" in filenames:
            packages += go_modules(dirpath, track_dir)
        for d in dirnames:
            if d.endswith(".dist-info"):
                packages += python_distribution(os.path.join(dirpath, d), track_dir)
        if any(p in VENDOR_DIR_NAMES for p in parents):
            packages += hackage_package(dirpath, filenames, track_dir)
    return packages


def npm_packages(node_modules, track_dir):
    """node_modules/<name> and node_modules/@scope/<name> with a package.json naming them."""
    found = []
    for entry in sorted(os.listdir(node_modules)):
        path = os.path.join(node_modules, entry)
        if entry.startswith("@") and os.path.isdir(path) and not os.path.islink(path):
            candidates = [os.path.join(path, e) for e in sorted(os.listdir(path))]
        else:
            candidates = [path]
        for candidate in candidates:
            manifest = os.path.join(candidate, "package.json")
            if os.path.islink(candidate) or not os.path.isfile(manifest):
                continue
            try:
                data = read_json(manifest)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("name"), str) and isinstance(data.get("version"), str):
                found.append(Package("npm", data["name"], data["version"], [rel(candidate, track_dir)],
                                     rel(manifest, track_dir)))
    return found


def cargo_package(crate_dir, track_dir):
    """A crate written by `cargo vendor`. Its .cargo-checksum.json records the registry's
    checksum of the .crate file; a crate vendored from git has "package": null."""
    try:
        checksums = read_json(os.path.join(crate_dir, ".cargo-checksum.json"))
        package = read_toml(os.path.join(crate_dir, "Cargo.toml")).get("package", {})
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return []
    if not isinstance(checksums, dict) or "name" not in package:
        return []
    root = rel(crate_dir, track_dir)
    files = {f"{root}/{name}" for name in checksums.get("files", {})} | {f"{root}/.cargo-checksum.json"}
    return [Package("cargo", package["name"], str(package.get("version", "")), [root],
                    f"{root}/.cargo-checksum.json", files=files, registry=bool(checksums.get("package")))]


def go_modules(vendor_dir, track_dir):
    """The modules in vendor/modules.txt, which `go mod vendor` writes. A module replaced
    by a local directory (=> ./path) is the team's code, not a registry module."""
    modules, current = [], None
    with open(os.path.join(vendor_dir, "modules.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("# "):
                words, current = line[2:].split(), None
                if not words:
                    continue
                name, version = words[0], words[1] if len(words) > 1 and words[1] != "=>" else ""
                registry = True
                if "=>" in words:
                    target = words[words.index("=>") + 1:]
                    if not target or target[0].startswith((".", "/")):
                        registry = False      # replaced by a directory: the team's own code
                    else:
                        name, version = target[0], target[1] if len(target) > 1 else version
                current = {"path": words[0], "name": name, "version": version, "dirs": set(), "registry": registry}
                modules.append(current)
            elif line and not line.startswith("#") and current is not None:
                current["dirs"].add(rel(os.path.join(vendor_dir, *line.split("/")), track_dir))
    evidence = rel(os.path.join(vendor_dir, "modules.txt"), track_dir)
    return [Package("go", m["name"], m["version"], [rel(os.path.join(vendor_dir, *m["path"].split("/")), track_dir)],
                    evidence, package_dirs=m["dirs"], registry=m["registry"])
            for m in modules if m["dirs"]]


def python_distribution(dist_info, track_dir):
    """A package installed by pip (pip install --target vendor/ ...): its .dist-info
    names it, and its RECORD lists exactly the files it installed."""
    metadata, record = os.path.join(dist_info, "METADATA"), os.path.join(dist_info, "RECORD")
    if not (os.path.isfile(metadata) and os.path.isfile(record)):
        return []
    fields = {}
    with open(metadata, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                break
            key, _, value = line.partition(":")
            fields.setdefault(key.strip().lower(), value.strip())
    if not fields.get("name"):
        return []
    site = os.path.dirname(dist_info)
    files, roots = set(), set()
    with open(record, encoding="utf-8", errors="replace") as f:
        for row in f:
            path = row.rsplit(",", 2)[0].strip().strip('"')
            if path and not path.startswith("/") and ".." not in path.split("/"):
                files.add(rel(os.path.join(site, *path.split("/")), track_dir))
                roots.add(rel(os.path.join(site, path.split("/")[0]), track_dir))
    return [Package("pypi", dependencies.normalise_pypi(fields["name"]), fields.get("version", ""), roots,
                    rel(record, track_dir), files=files)]


def hackage_package(directory, filenames, track_dir):
    """An unpacked Hackage source package (name-1.2.3/name.cabal) under a vendor directory."""
    name, _, version = os.path.basename(directory).rpartition("-")
    if name and version.replace(".", "").isdigit() and f"{name}.cabal" in filenames:
        root = rel(directory, track_dir)
        return [Package("hackage", name, version, [root], f"{root}/{name}.cabal")]
    return []


class Ownership:
    """Answers "is this file part of a vendored registry package?" for every path."""

    def __init__(self, packages):
        self.packages = packages
        self.by_file, self.by_dir, self.by_package_dir, self.by_root = {}, {}, {}, {}
        for p in packages:
            for root in p.roots:
                self.by_root.setdefault(root, p)
                if p.files is None and p.package_dirs is None:
                    self.by_dir.setdefault(root, p)
            for f in p.files or ():
                self.by_file.setdefault(f, p)
            for d in p.package_dirs or ():
                self.by_package_dir.setdefault(d, p)

    def owner(self, relpath):
        """The package that owns this file, or None."""
        if relpath in self.by_file:
            return self.by_file[relpath]
        directory = os.path.dirname(relpath)
        if directory in self.by_package_dir:
            return self.by_package_dir[directory]
        while directory:
            if directory in self.by_dir:
                return self.by_dir[directory]
            directory = os.path.dirname(directory)
        return None

    def root_of(self, relpath):
        """(root, package) for the package root that contains relpath, owned by it or not."""
        path = relpath
        while path and path != ".":
            if path in self.by_root:
                return path, self.by_root[path]
            path = os.path.dirname(path)
        return None, None

    def within_package(self, relpath):
        return self.root_of(relpath)[1] is not None


def reconcile(vendored, scan):
    """Square the vendored packages with what the manifests declare, and return the
    registry packages that S leaves out.

    A dependency declared by path into a vendored registry package is that registry
    package. A vendored package that a manifest declares as a git or URL dependency
    is not a registry package, whatever its metadata says, so its code counts in S."""
    ownership = Ownership([p for p in vendored if p.registry])
    deps = []
    for dep in scan.dependencies:
        _, target = ownership.root_of(dep.spec) if dep.source == PATH else (None, None)
        if target:
            dep = Dependency(dep.ecosystem, target.name, REGISTRY, dep.manifest, f"vendored at {dep.spec}")
        deps.append(dep)
    scan.dependencies = dependencies.dedupe(deps)
    not_registry = {(d.ecosystem, d.name.lower()): d for d in scan.dependencies if d.source in (GIT, URL)}
    kept, notes = [], []
    for p in vendored:
        declared = not_registry.get((p.ecosystem, p.name.lower()))
        where = ", ".join(p.roots)
        if not p.registry:
            why = ("its .cargo-checksum.json has no registry checksum (vendored from git)" if p.ecosystem == "cargo"
                   else "vendor/modules.txt replaces it with a local directory")
            notes.append(f"{where}: {p.label()} is not a registry package: {why}; its code counts in S")
        elif declared:
            notes.append(f"{where}: {declared.manifest} declares {p.name} as a {declared.source} dependency, "
                         f"so it is not a registry package and its code counts in S")
        else:
            kept.append(p)
    return kept, notes


# ------------------------------------------------------------------ running the tools image

class Tools:
    """The tools image, run with no network, a read-only root filesystem and the work
    directory mounted at /work. cpuset pins the container to CPUs (on the scoring box,
    the builder's E-cores, so a measurement never competes with a timed run)."""

    def __init__(self, image, work, cpuset=None):
        self.image, self.work, self.cpuset = image, work, cpuset

    def command(self, argv, env=None):
        cmd = ["docker", "run", "--rm", "--network=none", "--read-only", "--tmpfs", "/tmp:rw,size=512m",
               "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=4g", "--pids-limit=512",
               "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{self.work}:/work", "-w", "/work"]
        if self.cpuset:
            cmd.append(f"--cpuset-cpus={self.cpuset}")
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        return cmd + [self.image, *argv]

    def run(self, argv, timeout, env=None):
        try:
            return subprocess.run(self.command(argv, env), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"{argv[0]} in {self.image} did not finish within {timeout} s") from e


def check_docker(image):
    if shutil.which("docker") is None:
        raise ToolError("docker is not on PATH; S is measured inside the tools image")
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True)
    if probe.returncode != 0:
        if "daemon" in probe.stderr.lower():
            raise ToolError(f"the Docker daemon is not reachable: {probe.stderr.strip()}")
        raise ToolError(f"the tools image {image} is not built; from the repository root run: {BUILD_COMMAND}")


def tool_versions(tools):
    """The versions recorded in the image. This also proves Docker can see the work directory."""
    open(os.path.join(tools.work, "visible"), "w").close()
    proc = tools.run(["sh", "-c", f"test -f /work/visible && cat {CONFIG_DIR}/versions.tsv"], 120)
    if proc.returncode != 0:
        raise ToolError(f"Docker cannot see {tools.work} (or {tools.image} is not the tools image). Pass --work "
                        f"with a directory Docker can mount; Colima and Docker Desktop share your home directory.")
    return dict(line.split("\t", 1) for line in proc.stdout.splitlines() if "\t" in line)


def run_scc(tools):
    """scc's per-file counts for the copy, keyed by path relative to the track."""
    proc = tools.run(SCC_ARGS, 1800)
    if proc.returncode != 0:
        raise ToolError(f"scc failed: {proc.stderr.strip()[-2000:]}")
    counts = {}
    for language in json.loads(proc.stdout or "null") or []:
        for f in language.get("Files") or []:
            counts[f["Location"].removeprefix("/work/src/")] = {
                "language": f["Language"], "lines": f["Lines"], "code": f["Code"],
                "comment": f["Comment"], "blank": f["Blank"]}
    return counts


def rust_edition(relpath, copy, notes):
    """The edition from the nearest Cargo.toml above a .rs file, so rustfmt parses it as rustc will."""
    directory = os.path.dirname(os.path.join(copy, relpath))
    while inside(directory, copy):
        manifest = os.path.join(directory, "Cargo.toml")
        try:
            package = read_toml(manifest).get("package") if os.path.isfile(manifest) else None
        except (OSError, tomllib.TOMLDecodeError):
            package = None
        if package is not None:
            edition = package.get("edition", "2015")                    # Cargo's default
            if isinstance(edition, dict):                                # edition.workspace = true
                root = dependencies.cargo_workspace_root(manifest, copy)
                try:
                    edition = read_toml(root)["workspace"]["package"]["edition"] if root else "2015"
                except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
                    edition = "2015"
            if str(edition) in RUST_EDITIONS:
                return str(edition)
            notes.append(f"{rel(manifest, copy)}: unknown edition {edition!r}; formatted as {DEFAULT_RUST_EDITION}")
            return DEFAULT_RUST_EDITION
        if os.path.realpath(directory) == os.path.realpath(copy):
            break
        directory = os.path.dirname(directory)
    return DEFAULT_RUST_EDITION


def format_copy(tools, copy, files, versions, notes):
    """Run every file through its language's formatter, in place in the copy.
    files maps relpath -> scc language. Returns {relpath: reason} for each file
    that is counted as written instead."""
    groups, unformatted = {}, {}
    for path, language in sorted(files.items()):
        tool = FORMATTER_FOR_LANGUAGE.get(language)
        if tool is None:
            continue
        if tool not in versions:
            unformatted[path] = f"{tool} is not in the tools image"
        elif "\n" in path:
            unformatted[path] = "its file name contains a newline"
        else:
            command = FORMATTER_COMMANDS[tool]
            if tool == "rustfmt":
                command = command.format(edition=rust_edition(path, copy, notes))
            groups.setdefault((tool, command), []).append(path)

    os.makedirs(os.path.join(tools.work, "format"), exist_ok=True)
    for n, ((tool, command), paths) in enumerate(sorted(groups.items())):
        listing = os.path.join(tools.work, "format", f"{n}.list")
        status = os.path.join(tools.work, "format", f"{n}.status")
        with open(listing, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("".join(f"/work/src/{p}\n" for p in paths))
        proc = tools.run(["sh", "-c", FORMAT_LOOP, "sh", f"/work/format/{n}.list", f"/work/format/{n}.status", command],
                         timeout=FORMAT_TIMEOUT_S * len(paths) + 120, env={"TIMEOUT": str(FORMAT_TIMEOUT_S)})
        results = {}
        if os.path.exists(status):
            with open(status, encoding="utf-8", errors="surrogateescape") as f:
                for i, line in enumerate(f.read().splitlines(), start=1):
                    code, _, path = line.partition("\t")
                    results[path.removeprefix("/work/src/")] = (code, f"{status}.{i}.err")
        if not results:   # the loop never ran: a broken container, not a bad file
            raise ToolError(f"{tool} could not run in {tools.image}: {proc.stderr.strip()[-2000:]}")
        for path in paths:
            code, err = results.get(path, ("missing", ""))
            if code != "0":
                unformatted[path] = f"{tool} exit {code}: {first_line(err)}"
    return unformatted


def first_line(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return next((line.strip()[:200] for line in f if line.strip()), "no output")
    except OSError:
        return "no output"


# ------------------------------------------------------------------ the measurement

def copy_track(track_dir, copy):
    """Copy the track (without .git) and drop the formatter control files from the copy."""
    shutil.copytree(track_dir, copy, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    removed = []
    for dirpath, _, filenames in os.walk(copy):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            if name in FORMATTER_CONTROL_FILES:
                os.remove(path)
                removed.append(rel(path, copy))
            else:
                os.chmod(path, os.stat(path).st_mode | 0o600)
    return sorted(removed)


def inventory(copy):
    """Every file and symlink in the copy: relative path -> "file" or "symlink"."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(copy):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                found[rel(path, copy)] = "symlink"
            elif name in filenames:
                found[rel(path, copy)] = "file"
    return found


def sniff(path):
    """("binary", None) or ("unrecognised", line count), for a file scc did not count."""
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data[:8192]:
        return "binary", None
    return "unrecognised", data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def long_lines(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return sum(1 for line in f if len(line.rstrip("\n")) > LONG_LINE_CHARS)


GO_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|`[^`]*`|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/')
GO_HEADER = re.compile(r"\s*(\}\s*else\s+)?(for|if|switch|select)\b")


def go_statement_lines(path):
    """Go lines holding more than one statement. gofmt deletes every optional
    semicolon, so one left outside a for, if or switch header separates statements."""
    count = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            code = GO_LITERAL.sub('""', line).split("//")[0]
            if ";" in code and not GO_HEADER.match(code):
                count += 1
    return count


PUBLISHED_DIRS = ("generators", "reference")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def published_copies(track_dir):
    """Track files byte-identical to a file we published in generators/ or reference/.

    Copying the generator in for build-time training (a JVM AOT cache, PGO) costs
    nothing; an edited copy is the team's own code and counts.
    """
    digests = set()
    for sub in PUBLISHED_DIRS:
        folder = os.path.join(ROOT, sub)
        if os.path.isdir(folder):
            digests |= {file_sha256(os.path.join(folder, n)) for n in os.listdir(folder)
                        if os.path.isfile(os.path.join(folder, n))}
    found = set()
    for dirpath, dirnames, filenames in os.walk(track_dir):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.isfile(full) and not os.path.islink(full) and file_sha256(full) in digests:
                found.add(os.path.relpath(full, track_dir).replace(os.sep, "/"))
    return found


def tally(copy, counts, ownership, unformatted, versions, published=frozenset()):
    """Sort every file into counted code, an excluded registry package, or not counted."""
    per_language, formatter_used, excluded, not_counted, notes = {}, {}, {}, [], []
    files = inventory(copy)
    for path in sorted(set(counts) - set(files)):
        # scc reported a file under a name that matches nothing in the copy (odd bytes
        # in the name, say). Count it as scc saw it rather than lose it, and say so.
        files[path] = "unmatched"
        notes.append(f"{path}: scc reported this name, which matches no file in the copy; counted as scc saw it")
    for path, kind in sorted(files.items()):
        count = counts.get(path)
        if path in published:
            not_counted.append({"path": path, "kind": "published copy", "language": count and count["language"],
                                "lines": count["lines"] if count else None})
            notes.append(f"{path}: byte-identical to a published generator or reference file; not counted")
            continue
        package = ownership.owner(path)
        if package is not None:
            root, _ = ownership.root_of(path)
            entry = excluded.setdefault((root, package.ecosystem, package.label()), {
                "path": root, "ecosystem": package.ecosystem, "package": package.label(),
                "evidence": package.evidence, "files": 0, "code_lines": 0})
            entry["files"] += 1
            if count and count["language"] not in NOT_CODE:
                entry["code_lines"] += count["code"]
            continue
        if kind == "symlink":
            not_counted.append({"path": path, "kind": "symlink", "language": None, "lines": None})
            continue
        if os.path.basename(path) in LOCKFILE_NAMES:
            lines = count["lines"] if count else sniff(os.path.join(copy, path))[1]
            not_counted.append({"path": path, "kind": "lockfile", "language": count and count["language"],
                                "lines": lines})
            continue
        if count is None:
            sniffed, lines = sniff(os.path.join(copy, path))
            not_counted.append({"path": path, "kind": sniffed, "language": None, "lines": lines})
            continue
        language = count["language"]
        stats = per_language.setdefault(language, {"files": 0, "code": 0, "comment": 0, "blank": 0,
                                                   "counted": language not in NOT_CODE})
        stats["files"] += 1
        for key in ("code", "comment", "blank"):
            stats[key] += count[key]
        if language in NOT_CODE:
            not_counted.append({"path": path, "kind": "prose" if language in PROSE_LANGUAGES else "data",
                                "language": language, "lines": count["lines"]})
            continue
        tool = FORMATTER_FOR_LANGUAGE.get(language)
        formatter_used[language] = versions.get(tool) if tool else None
        if path in unformatted:
            notes.append(f"{path}: counted as written ({unformatted[path]})")
        too_long = long_lines(os.path.join(copy, path)) if kind == "file" else 0
        if too_long:
            notes.append(f"{path}: {plural(too_long)} longer than {LONG_LINE_CHARS} characters after formatting; "
                         f"spot-check for code kept out of the formatter's reach")
        crowded = go_statement_lines(os.path.join(copy, path)) if language == "Go" and kind == "file" else 0
        if crowded:
            notes.append(f"{path}: {plural(crowded)} with more than one statement, which gofmt leaves as written; "
                         f"spot-check")
        root, inside_package = ownership.root_of(path)
        if inside_package is not None:
            notes.append(f"{path}: inside vendored package {inside_package.label()} ({root}) but not one of "
                         f"the files its metadata lists; counted")
    for language, version in sorted(formatter_used.items()):
        if version is None:
            tool = FORMATTER_FOR_LANGUAGE.get(language)
            why = f"{tool} is not in the tools image" if tool else "no formatter for it in the tools image"
            notes.append(f"{language}: counted as written ({why})")
    for directory in sorted(vendor_dirs(files)):
        counted = sum(counts[p]["code"] for p in files
                      if p.startswith(directory + "/") and p in counts and ownership.owner(p) is None
                      and counts[p]["language"] not in NOT_CODE)
        if counted:
            notes.append(f"{directory}: {plural(counted, 'code line')} in here counted, not being identified "
                         f"as registry packages")
    return {
        "S_lines": sum(s["code"] for s in per_language.values() if s["counted"]),
        "per_language": dict(sorted(per_language.items())),
        "formatter_used": dict(sorted(formatter_used.items())),
        "excluded_paths": sorted(excluded.values(), key=lambda e: e["path"]),
        "not_counted": not_counted,
        "notes": notes,
    }


def plural(n, noun="line"):
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def vendor_dirs(files):
    """The outermost directories named like vendor directories."""
    found = set()
    for path in files:
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            if part in VENDOR_DIR_NAMES:
                found.add("/".join(parts[:i + 1]))
                break
    return found


def git_state(track_dir):
    """(commit, paths that differ from it) when the track is in a git checkout, else (None, [])."""
    def git(*args):
        try:
            return subprocess.run(["git", "-C", track_dir, *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
    head = git("rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        return None, []
    status = git("status", "--porcelain", "--ignored", "--untracked-files=all", "--", ".")
    changed = [line[3:] for line in status.stdout.splitlines()] if status and status.returncode == 0 else []
    return head.stdout.strip(), changed


def dependency_axis(track_dir):
    """D, and the registry packages S leaves out. Needs no Docker.

    D comes first because the manifests decide which vendored packages really are
    registry packages, and a vendored package's own manifest describes that
    package, not the team's program."""
    vendored = find_vendored_packages(track_dir)
    scan = dependencies.find_direct_dependencies(track_dir, skip=Ownership(vendored).within_package)
    packages, reconcile_notes = reconcile(vendored, scan)
    declaration, problem = dependencies.read_declaration(track_dir)
    d_value, d_basis, d_notes = dependencies.dependency_weight(scan, declaration)
    return {"D": d_value, "D_basis": d_basis, "scan": scan, "packages": packages, "declaration": declaration,
            "notes": reconcile_notes + scan.notes + d_notes + ([problem] if problem else [])}


def measure(track_dir, *, image=TOOLS_IMAGE, work_root=None, check_registry=False, keep_work=False, cpuset=None,
            exempt_published=True):
    """The full report for one track directory (the JSON the command line prints)."""
    track_dir = os.path.abspath(track_dir)
    if not os.path.isdir(track_dir):
        raise ToolError(f"{track_dir} is not a directory")
    d = dependency_axis(track_dir)
    scan, packages, declaration = d["scan"], d["packages"], d["declaration"]
    ownership = Ownership(packages)

    # S: copy, format, count.
    work_root = work_root or default_work_root()
    os.makedirs(work_root, exist_ok=True)
    work = tempfile.mkdtemp(prefix="static-axes-", dir=work_root)
    format_notes = []
    try:
        copy = os.path.join(work, "src")
        tools = Tools(image, work, cpuset)
        # The references are themselves published files, so measuring S_ref turns the exemption off.
        published = published_copies(track_dir) if exempt_published else set()
        removed = copy_track(track_dir, copy)
        versions = tool_versions(tools)
        before = run_scc(tools)
        to_format = {path: c["language"] for path, c in before.items()
                     if ownership.owner(path) is None and c["language"] not in NOT_CODE and path not in published}
        unformatted = format_copy(tools, copy, to_format, versions, format_notes)
        s = tally(copy, run_scc(tools), ownership, unformatted, versions, published)
    finally:
        if keep_work:
            print(f"static_axes: kept the formatted copy in {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)
    for path in removed:
        s["not_counted"].append({"path": path, "kind": "formatter control file", "language": None, "lines": None})

    notes = s["notes"] + format_notes
    notes += [f"{path}: left out of the copy before formatting, since clang-format would obey it; not code"
              for path in removed]
    notes += d["notes"]
    commit, changed = git_state(track_dir)
    if changed:
        sample = ", ".join(changed[:3]) + (", ..." if len(changed) > 3 else "")
        notes.append(f"{len(changed)} paths differ from commit {commit[:12]} (modified, untracked or ignored: "
                     f"{sample}); this measured the directory as it is, so measure a clean checkout")

    report = {
        "track_dir": track_dir,
        "commit": commit,
        "S_lines": s["S_lines"],
        "per_language": s["per_language"],
        "formatter_used": s["formatter_used"],
        "excluded_paths": s["excluded_paths"],
        "not_counted": sorted(s["not_counted"], key=lambda e: (-(e["lines"] or 0), e["path"])),
        "D": d["D"],
        "D_basis": d["D_basis"],
        "declared": {k: declaration[k] for k in ("track", "team", "language", "dependency_count") if k in declaration},
        "direct_dependencies": [dependency_entry(d) for d in scan.dependencies],
        "local_packages": [dependency_entry(d) for d in scan.local],
    }
    if check_registry:
        items = [{"ecosystem": d.ecosystem, "name": d.name, "source": d.source, "origin": "direct"}
                 for d in scan.dependencies]
        items += [{"ecosystem": p.ecosystem, "name": p.name, "source": REGISTRY, "origin": "vendored"}
                  for p in packages]
        report["registry_check"] = dependencies.check_registry(items)
        notes += registry_notes(report["registry_check"])
    report["tools"] = {"image": image, **versions}
    report["notes"] = list(dict.fromkeys(notes))
    return report


def dependency_entry(dep):
    entry = {"name": dep.name, "ecosystem": dep.ecosystem, "source": dep.source, "manifest": dep.manifest}
    if dep.spec:
        entry["spec"] = dep.spec
    return entry


def registry_notes(results):
    notes = []
    for status, words in (("flag", "first published on or after 2026-10-01"),
                          ("not-found", "not found on its registry"),
                          ("not-a-registry-package", "not a registry package"),
                          ("error", "the lookup failed"),
                          ("unknown", "no registry lookup for its ecosystem")):
        names = [f"{r['ecosystem']}:{r['name']}" for r in results if r["status"] == status]
        if names:
            notes.append(f"registry check, {words}: {', '.join(names)}")
    return notes


def default_work_root():
    if sys.platform == "darwin":
        # Docker on macOS runs in a VM that always shares the home directory,
        # but not necessarily the system temp directory.
        return os.path.join(os.path.expanduser("~"), ".cache", "speeddemon-static-axes")
    return tempfile.gettempdir()


def measure_references(*, image=TOOLS_IMAGE, work_root=None, cpuset=None):
    """S_ref for each track: the reference implementation, alone in a directory, measured like a submission."""
    work_root = work_root or default_work_root()
    os.makedirs(work_root, exist_ok=True)
    axes = {}
    for track, filename in REFERENCE_FILES.items():
        track_dir = tempfile.mkdtemp(prefix=f"reference-{track}-", dir=work_root)
        try:
            shutil.copy2(os.path.join(ROOT, "reference", filename), track_dir)
            axes[track] = {"S_lines": measure(track_dir, image=image, work_root=work_root, cpuset=cpuset,
                                             exempt_published=False)["S_lines"]}
        finally:
            shutil.rmtree(track_dir, ignore_errors=True)
    with open(REFERENCE_AXES, "w", encoding="utf-8") as f:
        json.dump(axes, f, indent=2)
        f.write("\n")
    return axes


def main(argv=None):
    ap = argparse.ArgumentParser(description="Measure S (source size) and D (dependency weight) of a submission.")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--track-dir", help="the submission's track directory, e.g. ./track-a")
    target.add_argument("--reference", action="store_true",
                        help="measure the four reference implementations and rewrite harness/reference_axes.json")
    ap.add_argument("--tools-image", default=TOOLS_IMAGE, help=f"default {TOOLS_IMAGE}")
    ap.add_argument("--check-registry", action="store_true",
                    help="look up first-publish dates on the package registries (needs network)")
    ap.add_argument("--json", help="also write the report to this file")
    ap.add_argument("--work", help="where the temporary copy goes; Docker must be able to mount it")
    ap.add_argument("--keep-work", action="store_true", help="keep the formatted copy for inspection")
    ap.add_argument("--cpuset", help="pin the tools container to these CPUs, e.g. 12-19 (the box's E-cores)")
    args = ap.parse_args(argv)
    try:
        check_docker(args.tools_image)
        if args.reference:
            result = measure_references(image=args.tools_image, work_root=args.work, cpuset=args.cpuset)
        else:
            result = measure(args.track_dir, image=args.tools_image, work_root=args.work,
                             check_registry=args.check_registry, keep_work=args.keep_work, cpuset=args.cpuset)
    except ToolError as e:
        print(f"static_axes: {e}", file=sys.stderr)
        return 2
    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
