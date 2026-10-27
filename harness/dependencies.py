"""D, the dependency axis, and the registry spot-check behind S.

D = 1 + the number of direct, non-stdlib, runtime dependencies declared in the
manifests inside a track directory. What counts as a runtime dependency:

  Cargo.toml          [dependencies] and [target.*.dependencies], optional ones
                      included; not [dev-dependencies] or [build-dependencies]
  go.mod              require lines not marked // indirect
  package.json        dependencies, optionalDependencies and peerDependencies
                      (npm installs all three at run time); not devDependencies
  requirements.txt    every requirement, following -r includes
  pyproject.toml      [project] dependencies, and [tool.poetry.dependencies]
                      minus python; not optional-dependencies or dependency-groups
  Pipfile             [packages]; not [dev-packages]
  pom.xml             <dependencies> with scope compile (the default) or runtime;
                      not test, provided or system, and not <dependencyManagement>
  build.gradle(.kts)  implementation, api, runtimeOnly (and the old compile and
                      runtime); the Kotlin standard library is stdlib
  *.csproj *.fsproj   PackageReference, except PrivateAssets="all" (build-time
                      only); FSharp.Core is stdlib
  Package.swift       .package(...)
  *.cabal             build-depends of library and executable stanzas and the
                      common stanzas they import, minus base

A dependency on a directory inside the track (a Cargo path or workspace crate,
a Gradle project, a Maven module, an npm or uv workspace member, a cabal
package's own library) is the team's own code. It counts towards S, not D, and
the dependencies its own manifest declares count in D like any others.

With no manifest at all, D comes from dependency_count in the track's
.speed-demon.toml and is marked "declared, unverified".

check_registry() is the organiser's spot-check: it looks up when each package
was first published on its registry and flags anything first published on or
after REGISTRY_CUTOFF. It needs the network and is never part of a scored run.
"""
import gzip
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

REGISTRY_CUTOFF = datetime(2026, 10, 1, tzinfo=timezone.utc)
SPEED_DEMON_TOML = ".speed-demon.toml"

# Where a dependency comes from: a registry, a git repository, a directory, a URL,
# or a package archive (a jar, wheel or tarball) committed to the repository.
REGISTRY, GIT, PATH, URL, FILE = "registry", "git", "path", "url", "file"
ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".jar", ".crate", ".nupkg")

# Files that declare dependencies in a format this module does not read.
# Finding one adds a note so the organiser knows where to look.
UNREAD_MANIFESTS = {
    "setup.py", "setup.cfg", "package.yaml", "vcpkg.json", "conanfile.txt", "conanfile.py",
    "build.zig.zon", "deno.json", "deno.jsonc", "Gemfile", "pubspec.yaml", "mix.exs",
    "build.sbt", "deps.edn", "project.clj", "dub.json", "dub.sdl", "gleam.toml", "shard.yml",
    "Project.toml", "cpanfile", "v.mod",
}

PARSE_ERRORS = (ValueError, OSError, ET.ParseError, tomllib.TOMLDecodeError, UnicodeDecodeError)


@dataclass(frozen=True)
class Dependency:
    ecosystem: str   # cargo, go, npm, pypi, maven, nuget, swiftpm or hackage
    name: str        # the name the registry knows it by
    source: str      # REGISTRY, GIT, PATH, URL or FILE
    manifest: str    # the manifest that declares it, relative to the track directory
    spec: str = ""   # the version requirement as written; for PATH, the target relative to the track


@dataclass
class Scan:
    """What the manifests of one track directory declare."""
    manifests: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)   # counted in D
    local: list = field(default_factory=list)          # the team's own packages, counted in S
    notes: list = field(default_factory=list)


# ------------------------------------------------------------------ finding manifests

def rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def inside(path, root):
    """True when path is root or below it, after resolving '..' and symlinks."""
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def parser_name(filename):
    if filename.endswith((".csproj", ".fsproj", ".vbproj")) or filename in ("Directory.Build.props",
                                                                          "Directory.Build.targets"):
        return "msbuild"
    if filename.endswith(".cabal"):
        return "cabal"
    return filename if filename in PARSERS else None


def walk_files(track_dir, skip):
    """Every regular file under the track, sorted, leaving out .git and whatever skip(relpath) owns."""
    for dirpath, dirnames, filenames in os.walk(track_dir):
        dirnames[:] = sorted(d for d in dirnames
                             if d != ".git" and not skip(rel(os.path.join(dirpath, d), track_dir)))
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if not os.path.islink(path) and not skip(rel(path, track_dir)):
                yield path


def find_direct_dependencies(track_dir, skip=lambda relpath: False):
    """Parse every manifest in the track. skip(relpath) is true for vendored registry
    packages: their manifests describe them, not the team's program."""
    scan = Scan()
    manifests = []
    for path in walk_files(track_dir, skip):
        name = os.path.basename(path)
        if parser_name(name):
            manifests.append(path)
        elif name in UNREAD_MANIFESTS or (name.endswith(".txt") and "requirements" in name):
            scan.notes.append(f"{rel(path, track_dir)}: dependencies declared here are not read; spot-check it")
    context = Context(track_dir, manifests, skip)
    for path in manifests:
        scan.manifests.append(rel(path, track_dir))
        try:
            declared = PARSERS[parser_name(os.path.basename(path))](path, context, scan.notes)
        except PARSE_ERRORS as e:
            scan.notes.append(f"{rel(path, track_dir)}: could not be parsed ({e}); spot-check its dependencies")
            continue
        for dep in declared:
            (scan.local if context.is_local(dep) else scan.dependencies).append(dep)
    scan.dependencies = dedupe(scan.dependencies)
    scan.local = dedupe(scan.local)
    for dep in scan.dependencies:
        if dep.source in (PATH, FILE) and dep.spec.startswith("../"):
            scan.notes.append(f"{dep.manifest}: {dep.name} points outside the track directory ({dep.spec}); "
                              f"a scored build cannot see it")
    return scan


def dedupe(deps):
    """One entry per (ecosystem, name), keeping the first manifest in path order."""
    first = {}
    for dep in deps:
        first.setdefault((dep.ecosystem, dep.name.lower()), dep)
    return sorted(first.values(), key=lambda d: (d.ecosystem, d.name.lower()))


# Package managers that resolve a dependency to a package in the same repository by
# its name alone (npm workspaces, Maven modules, Go modules, cabal packages). Cargo
# and pip only do that through an explicit path, so for them only paths are local.
LOCAL_BY_NAME = {"npm", "maven", "go", "hackage"}


class Context:
    """What a parser needs to know about the rest of the track: where it is, which
    paths are vendored packages, and what the team's own packages are called."""

    def __init__(self, track_dir, manifests, skip):
        self.track_dir = track_dir
        self.skip = skip
        self.local_names = set()   # (ecosystem, lowercased name)
        for path in manifests:
            for ecosystem, name in own_package_names(path):
                self.local_names.add((ecosystem, name.lower()))

    def is_local(self, dep):
        if dep.source == PATH:
            target = os.path.join(self.track_dir, dep.spec)
            return inside(target, self.track_dir) and not self.skip(rel(target, self.track_dir))
        return dep.ecosystem in LOCAL_BY_NAME and (dep.ecosystem, dep.name.lower()) in self.local_names

    def path_dependency(self, ecosystem, name, manifest_path, target, base_dir=None):
        """A dependency on a directory (PATH) or on an archive file (FILE), resolved against
        base_dir (default: the manifest's directory). Its spec is the target relative to the track."""
        base_dir = base_dir or os.path.dirname(manifest_path)
        resolved = os.path.normpath(os.path.join(base_dir, target))
        source = FILE if resolved.endswith(ARCHIVE_SUFFIXES) else PATH
        return Dependency(ecosystem, name, source, rel(manifest_path, self.track_dir), rel(resolved, self.track_dir))


def own_package_names(path):
    """The names a manifest gives its own package, for the ecosystems in LOCAL_BY_NAME."""
    name = os.path.basename(path)
    try:
        if name == "package.json":
            data = read_json(path)
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                yield "npm", data["name"]
        elif name == "go.mod":
            module = re.search(r"^\s*module\s+(\S+)", read_text(path), re.MULTILINE)
            if module:
                yield "go", module.group(1).strip('"')
        elif name == "pom.xml":
            coordinates = pom_coordinates(ET.parse(path).getroot())
            if coordinates:
                yield "maven", coordinates
        elif name.endswith(".cabal"):
            for package in cabal_own_names(read_text(path)):
                yield "hackage", package
    except PARSE_ERRORS:
        return


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_toml(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ Rust: Cargo.toml

def parse_cargo(path, ctx, notes):
    data = read_toml(path)
    workspace_root = cargo_workspace_root(path, ctx.track_dir)
    workspace_deps = read_toml(workspace_root).get("workspace", {}).get("dependencies", {}) if workspace_root else {}
    tables = [data.get("dependencies", {})]
    tables += [target.get("dependencies", {}) for target in data.get("target", {}).values()]
    deps = []
    for table in tables:
        for key, spec in table.items():
            base_dir = os.path.dirname(path)
            if isinstance(spec, dict) and spec.get("workspace") is True:
                # key.workspace = true: the source comes from the root's [workspace.dependencies].
                inherited = workspace_deps.get(key)
                if inherited is None:
                    notes.append(f"{rel(path, ctx.track_dir)}: {key}.workspace = true has no "
                                 f"[workspace.dependencies] entry; counted as a registry crate")
                    inherited = {}
                inherited = {"version": inherited} if isinstance(inherited, str) else dict(inherited)
                inherited.update({k: v for k, v in spec.items() if k != "workspace"})
                spec, base_dir = inherited, os.path.dirname(workspace_root or path)
            deps.append(cargo_dependency(key, spec, path, base_dir, ctx))
    return deps


def cargo_dependency(key, spec, manifest_path, base_dir, ctx):
    manifest = rel(manifest_path, ctx.track_dir)
    if isinstance(spec, str):
        return Dependency("cargo", key, REGISTRY, manifest, spec)
    name = spec.get("package", key)          # renamed: alias = { package = "real-name" }
    if "path" in spec:
        return ctx.path_dependency("cargo", name, manifest_path, spec["path"], base_dir)
    if "git" in spec:
        return Dependency("cargo", name, GIT, manifest, spec["git"])
    version = str(spec.get("version", ""))
    if "registry" in spec:
        version = f"{version} (registry {spec['registry']})".strip()
    return Dependency("cargo", name, REGISTRY, manifest, version)


def cargo_workspace_root(manifest_path, track_dir):
    """The nearest Cargo.toml at or above this one, inside the track, that has a [workspace]."""
    directory = os.path.dirname(os.path.abspath(manifest_path))
    while inside(directory, track_dir):
        candidate = os.path.join(directory, "Cargo.toml")
        try:
            if os.path.isfile(candidate) and "workspace" in read_toml(candidate):
                return candidate
        except PARSE_ERRORS:
            pass
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return None


# ------------------------------------------------------------------ Go: go.mod

def parse_go_mod(path, ctx, notes):
    requires, replacements = [], {}
    block = None
    for raw in read_text(path).splitlines():
        line, _, comment = raw.partition("//")
        words = line.split()
        if not words:
            continue
        if block:
            if words == [")"]:
                block = None
                continue
            directive = block
        elif words[0] in ("require", "replace"):
            directive, words = words[0], words[1:]
            if words == ["("]:
                block = directive
                continue
        else:
            continue
        if directive == "require" and len(words) >= 2:
            indirect = comment.strip().split(";")[0].strip() == "indirect"
            requires.append((words[0].strip('"'), words[1], indirect))
        elif directive == "replace" and "=>" in words:
            arrow = words.index("=>")
            if arrow + 1 < len(words):
                replacements[words[0].strip('"')] = words[arrow + 1].strip('"')
    deps = []
    for module, version, indirect in requires:
        if indirect:
            continue
        target = replacements.get(module)
        if target and target.startswith(("./", "../", "/")):
            deps.append(ctx.path_dependency("go", module, path, target))
        else:
            deps.append(Dependency("go", target or module, REGISTRY, rel(path, ctx.track_dir), version))
    return deps


# ------------------------------------------------------------------ JavaScript: package.json

NPM_RUNTIME_FIELDS = ("dependencies", "optionalDependencies", "peerDependencies")
GITHUB_SHORTHAND = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(#.*)?$")


def parse_package_json(path, ctx, notes):
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError("package.json is not a JSON object")
    deps = []
    for section in NPM_RUNTIME_FIELDS:
        table = data.get(section) or {}
        if isinstance(table, dict):
            deps += [npm_dependency(name, str(spec), path, ctx) for name, spec in table.items()]
    return deps


def npm_dependency(name, spec, manifest_path, ctx):
    manifest = rel(manifest_path, ctx.track_dir)
    if spec.startswith("npm:"):                       # an alias: "x": "npm:real-name@^1"
        real = spec[4:]
        at = real.rfind("@")
        return Dependency("npm", real[:at] if at > 0 else real, REGISTRY, manifest, spec)
    if spec.startswith(("file:", "link:")):
        return ctx.path_dependency("npm", name, manifest_path, spec.split(":", 1)[1])
    if spec.startswith(("git+", "git:", "github:", "gitlab:", "bitbucket:", "gist:")) or GITHUB_SHORTHAND.match(spec):
        return Dependency("npm", name, GIT, manifest, spec)
    if spec.startswith(("http://", "https://")):
        return Dependency("npm", name, URL, manifest, spec)
    # A semver range, a dist-tag, or workspace:* (local when the name is one of the track's packages).
    return Dependency("npm", name, REGISTRY, manifest, spec)


# ------------------------------------------------------------------ Python

PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
DIRECT_REFERENCE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*@\s*(\S+)")
ARCHIVE_NAME = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?)-\d")


def normalise_pypi(name):
    """PEP 503: names are case-insensitive, and runs of -, _ and . are one character."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(path, ctx, notes, seen=None):
    """requirements.txt, following -r includes (each file at most once)."""
    seen = set() if seen is None else seen
    if os.path.realpath(path) in seen:
        return []
    seen.add(os.path.realpath(path))
    deps = []
    for line in requirement_lines(read_text(path)):
        if not line.startswith("-"):
            dep = python_requirement(line, path, ctx)
            if dep:
                deps.append(dep)
            continue
        option, value = split_option(line)
        if option in ("-r", "--requirement"):
            include = os.path.normpath(os.path.join(os.path.dirname(path), value))
            if inside(include, ctx.track_dir) and os.path.isfile(include):
                deps += parse_requirements(include, ctx, notes, seen)
            else:
                notes.append(f"{rel(path, ctx.track_dir)}: -r {value} is not a file in the track; not followed")
        elif option in ("-e", "--editable"):
            deps.append(python_location(value, path, ctx))
        # Anything else (-c constraints, --index-url, --find-links, ...) declares no dependency.
    return deps


def requirement_lines(text):
    """Logical lines: continuations joined, comments and blank lines dropped."""
    for raw in re.sub(r"\\\r?\n", " ", text).splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if line:
            yield line


def split_option(line):
    """'-r x', '--requirement x' and '--requirement=x' all give (option, 'x')."""
    first, _, rest = line.partition(" ")
    if first.startswith("--") and "=" in first:
        first, _, value = first.partition("=")
        rest = f"{value} {rest}"
    return first, rest.strip()


def python_requirement(text, manifest_path, ctx):
    """A PEP 508 requirement, a direct reference (name @ url) or a bare path or URL."""
    text = re.split(r"\s--", text)[0].strip()        # drop per-requirement options such as --hash
    reference = DIRECT_REFERENCE.match(text)
    if reference:
        name, location = normalise_pypi(reference.group(1)), reference.group(3)
        if location.startswith("file:"):
            return ctx.path_dependency("pypi", name, manifest_path, urllib.parse.urlparse(location).path)
        source = GIT if location.startswith("git+") else URL
        return Dependency("pypi", name, source, rel(manifest_path, ctx.track_dir), location)
    if "/" in text or "\\" in text or text.startswith(".") or text.endswith(ARCHIVE_SUFFIXES):
        return python_location(text, manifest_path, ctx)   # names never contain a slash
    match = PEP508_NAME.match(text)
    if not match:
        return None
    return Dependency("pypi", normalise_pypi(match.group(1)), REGISTRY, rel(manifest_path, ctx.track_dir),
                      text[match.end():].strip())


def python_location(location, manifest_path, ctx):
    """A requirement given as a path, a VCS URL or an archive URL instead of a name."""
    manifest = rel(manifest_path, ctx.track_dir)
    egg = re.search(r"[#&]egg=([A-Za-z0-9._-]+)", location)
    base = os.path.basename(location.split("#")[0].rstrip("/"))
    archive = ARCHIVE_NAME.match(base)
    name = normalise_pypi(egg.group(1) if egg else archive.group(1) if archive else base or location)
    if location.startswith("git+"):
        return Dependency("pypi", name, GIT, manifest, location)
    if "://" in location and not location.startswith("file:"):
        return Dependency("pypi", name, URL, manifest, location)
    target = urllib.parse.urlparse(location).path if location.startswith("file:") else location
    return ctx.path_dependency("pypi", name, manifest_path, target.split("#")[0])


def parse_pyproject(path, ctx, notes):
    data = read_toml(path)
    tool = data.get("tool", {})
    uv_sources = tool.get("uv", {}).get("sources", {})
    deps = []
    for requirement in data.get("project", {}).get("dependencies", []):
        dep = python_requirement(requirement, path, ctx)
        if dep is None:
            continue
        source = next((v for k, v in uv_sources.items() if normalise_pypi(k) == dep.name), None)
        deps.append(python_with_source(dep, source, path, ctx) if isinstance(source, dict) else dep)
    for name, spec in tool.get("poetry", {}).get("dependencies", {}).items():
        if name.lower() == "python":
            continue
        dep = Dependency("pypi", normalise_pypi(name), REGISTRY, rel(path, ctx.track_dir),
                         spec if isinstance(spec, str) else "")
        deps.append(python_with_source(dep, spec, path, ctx) if isinstance(spec, dict) else dep)
    return deps


def parse_pipfile(path, ctx, notes):
    deps = []
    for name, spec in read_toml(path).get("packages", {}).items():
        dep = Dependency("pypi", normalise_pypi(name), REGISTRY, rel(path, ctx.track_dir),
                         spec if isinstance(spec, str) else "")
        deps.append(python_with_source(dep, spec, path, ctx) if isinstance(spec, dict) else dep)
    return deps


def python_with_source(dep, source, manifest_path, ctx):
    """Apply a table like { path = "..." } or { git = "..." } from uv, Poetry or a Pipfile."""
    if "path" in source:
        return ctx.path_dependency("pypi", dep.name, manifest_path, source["path"])
    if "git" in source:
        return Dependency("pypi", dep.name, GIT, dep.manifest, source["git"])
    if "url" in source or "file" in source:
        return Dependency("pypi", dep.name, URL, dep.manifest, source.get("url") or source.get("file"))
    if source.get("workspace") is True:   # a uv workspace member: somewhere in this repository
        return ctx.path_dependency("pypi", dep.name, manifest_path, ".")
    return Dependency("pypi", dep.name, REGISTRY, dep.manifest, str(source.get("version", dep.spec)))


# ------------------------------------------------------------------ Java: Maven and Gradle

def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def child(element, name):
    return next((c for c in element if local_name(c.tag) == name), None)


def child_text(element, name, default=""):
    c = child(element, name)
    return c.text.strip() if c is not None and c.text else default


def pom_coordinates(project):
    """groupId:artifactId of a POM; the groupId may be inherited from <parent>."""
    parent = child(project, "parent")
    group = child_text(project, "groupId") or (child_text(parent, "groupId") if parent is not None else "")
    artifact = child_text(project, "artifactId")
    return f"{group}:{artifact}" if group and artifact else None


def parse_pom(path, ctx, notes):
    project = ET.parse(path).getroot()
    manifest = rel(path, ctx.track_dir)
    group = (pom_coordinates(project) or ":").split(":")[0]
    parent = child(project, "parent")
    if parent is not None:
        coordinates = f"{child_text(parent, 'groupId')}:{child_text(parent, 'artifactId')}"
        if ("maven", coordinates.lower()) not in ctx.local_names:
            notes.append(f"{manifest}: inherits from parent POM {coordinates}, which is not in the track; "
                         f"dependencies declared there are not counted (spot-check)")
    profiles = child(project, "profiles")
    if profiles is not None and any(child(p, "dependencies") is not None for p in profiles):
        notes.append(f"{manifest}: dependencies inside <profiles> are not counted (spot-check)")
    deps = []
    dependencies = child(project, "dependencies")      # not dependencyManagement, not plugins
    for dep in (dependencies if dependencies is not None else []):
        if local_name(dep.tag) != "dependency" or child_text(dep, "scope", "compile") not in ("compile", "runtime"):
            continue
        dep_group = child_text(dep, "groupId").replace("${project.groupId}", group)
        deps.append(Dependency("maven", f"{dep_group}:{child_text(dep, 'artifactId')}", REGISTRY, manifest,
                               child_text(dep, "version")))
    return deps


GRADLE_CONFIGURATION = re.compile(
    r"(?<![\w.])(?:implementation|api|runtimeOnly|compile|runtime)(?=\s*[(\s'\"])\s*\(?\s*")
KOTLIN_STDLIB = re.compile(r"^org\.jetbrains\.kotlin:kotlin-stdlib")


def parse_gradle(path, ctx, notes):
    """build.gradle and build.gradle.kts, read by pattern. Gradle files are programs,
    so a dependency added in an unusual way can be missed; the note says so."""
    text = strip_comments(read_text(path))
    manifest = rel(path, ctx.track_dir)
    catalog = gradle_catalog(ctx.track_dir)
    deps = []
    for match in GRADLE_CONFIGURATION.finditer(text):
        rest = text[match.end():match.end() + 300]
        if re.match(r"(platform|enforcedPlatform)\s*\(", rest):
            continue   # a BOM: version constraints, no code of its own
        project = re.match(r"project\s*\(\s*(?:path\s*[:=]\s*)?['\"]([^'\"]+)", rest)
        if project:
            root = gradle_root(path, ctx.track_dir)
            target = os.path.join(*([p for p in project.group(1).split(":") if p] or ["."]))
            deps.append(ctx.path_dependency("maven", f"project {project.group(1)}", path, target, root))
            continue
        kotlin = re.match(r"kotlin\s*\(\s*['\"]([^'\"]+)", rest)
        if kotlin:
            names = [f"org.jetbrains.kotlin:kotlin-{kotlin.group(1)}"]
        elif re.match(r"(files|fileTree)\s*\(", rest):
            names = []
            argument = balanced(rest, rest.index("("))
            deps.append(Dependency("maven", f"local file {argument.strip()}", FILE, manifest, argument.strip()))
        elif re.match(r"libs(\.[A-Za-z0-9_]+)+", rest):
            accessor = re.match(r"libs(\.[A-Za-z0-9_]+)+", rest).group(0)
            names = catalog_modules(accessor, catalog, manifest, notes)
        elif re.match(r"['\"]([^'\"$:]+):([^'\"$:]+)", rest):
            notation = re.match(r"['\"]([^'\"$:]+):([^'\"$:]+)", rest)
            names = [f"{notation.group(1)}:{notation.group(2)}"]
        elif re.match(r"group\s*[:=]\s*['\"]([^'\"]+)['\"]\s*,\s*name\s*[:=]\s*['\"]([^'\"]+)['\"]", rest):
            named = re.match(r"group\s*[:=]\s*['\"]([^'\"]+)['\"]\s*,\s*name\s*[:=]\s*['\"]([^'\"]+)['\"]", rest)
            names = [f"{named.group(1)}:{named.group(2)}"]
        else:
            names = []
        deps += [Dependency("maven", n, REGISTRY, manifest, "") for n in names if not KOTLIN_STDLIB.match(n)]
    notes.append(f"{manifest}: Gradle build files are read by pattern; spot-check its dependencies block")
    return deps


def gradle_root(build_file, track_dir):
    """The directory of the nearest settings.gradle(.kts), where project(':x') paths start."""
    directory = os.path.dirname(os.path.abspath(build_file))
    while inside(directory, track_dir):
        if any(os.path.isfile(os.path.join(directory, s)) for s in ("settings.gradle", "settings.gradle.kts")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return os.path.dirname(os.path.abspath(build_file))


def gradle_catalog(track_dir):
    path = os.path.join(track_dir, "gradle", "libs.versions.toml")
    try:
        return read_toml(path) if os.path.isfile(path) else {}
    except PARSE_ERRORS:
        return {}


def catalog_key(alias):
    return re.sub(r"[-_.]", ".", alias).lower()


def catalog_modules(accessor, catalog, manifest, notes):
    """libs.foo.bar is alias foo-bar (or foo_bar, foo.bar) in gradle/libs.versions.toml."""
    parts = accessor.split(".")[1:]
    if parts[:1] == ["bundles"]:
        bundles = {catalog_key(k): v for k, v in catalog.get("bundles", {}).items()}
        aliases = bundles.get(catalog_key(".".join(parts[1:])))
        if aliases:
            return [m for alias in aliases for m in catalog_modules("libs." + alias, catalog, manifest, notes)]
    else:
        entry = {catalog_key(k): v for k, v in catalog.get("libraries", {}).items()}.get(catalog_key(".".join(parts)))
        if isinstance(entry, str):
            return [":".join(entry.split(":")[:2])]
        if isinstance(entry, dict) and "module" in entry:
            return [entry["module"]]
        if isinstance(entry, dict) and "group" in entry and "name" in entry:
            return [f"{entry['group']}:{entry['name']}"]
    notes.append(f"{manifest}: {accessor} is not in gradle/libs.versions.toml; counted as one dependency")
    return [accessor]


def strip_comments(text):
    """Drop // and /* */ comments from C-like source (Gradle, Kotlin, Swift), keeping string literals."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith('"""', i):
            end = text.find('"""', i + 3)
            end = n if end < 0 else end + 3
            out.append(text[i:end])
            i = end
        elif text[i] in "'\"":
            j = i + 1
            while j < n and text[j] not in (text[i], "\n"):
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def balanced(text, open_index):
    """The text between the parenthesis at open_index and the one that closes it."""
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1:i]
    return text[open_index + 1:]


# ------------------------------------------------------------------ .NET: MSBuild project files

NUGET_STDLIB = {"fsharp.core"}


def parse_msbuild(path, ctx, notes):
    manifest = rel(path, ctx.track_dir)
    deps = []
    for element in ET.parse(path).getroot().iter():
        kind = local_name(element.tag)
        if kind == "ProjectReference" and element.get("Include"):
            include = element.get("Include").replace("\\", "/")
            deps.append(ctx.path_dependency("nuget", os.path.basename(include), path, os.path.dirname(include) or "."))
        if kind != "PackageReference" or not element.get("Include"):
            continue
        name = element.get("Include")
        if (element.get("PrivateAssets") or child_text(element, "PrivateAssets")).strip().lower() == "all":
            notes.append(f"{manifest}: {name} has PrivateAssets=\"all\" (build-time only); not counted")
        elif name.lower() not in NUGET_STDLIB:
            version = element.get("Version") or child_text(element, "Version")
            deps.append(Dependency("nuget", name, REGISTRY, manifest, version))
    return deps


# ------------------------------------------------------------------ Swift: Package.swift

def parse_package_swift(path, ctx, notes):
    text = strip_comments(read_text(path))
    manifest = rel(path, ctx.track_dir)
    deps = []
    for match in re.finditer(r"\.package\s*\(", text):
        args = balanced(text, match.end() - 1)
        named = re.search(r'\bname:\s*"([^"]+)"', args)
        url = re.search(r'\burl:\s*"([^"]+)"', args)
        local = re.search(r'\bpath:\s*"([^"]+)"', args)
        identity = re.search(r'\bid:\s*"([^"]+)"', args)
        if local:
            name = named.group(1) if named else os.path.basename(local.group(1).rstrip("/"))
            deps.append(ctx.path_dependency("swiftpm", name, path, local.group(1)))
        elif url:   # SwiftPM has no central registry: a URL dependency is a git repository
            name = named.group(1) if named else os.path.basename(url.group(1).rstrip("/")).removesuffix(".git")
            deps.append(Dependency("swiftpm", name, GIT, manifest, url.group(1)))
        elif identity:
            deps.append(Dependency("swiftpm", identity.group(1), REGISTRY, manifest, ""))
    return deps


# ------------------------------------------------------------------ Haskell: *.cabal

CABAL_STANZA = re.compile(r"^(library|executable|test-suite|benchmark|foreign-library|common|flag"
                          r"|source-repository|custom-setup)\b[ \t]*(\S*)", re.IGNORECASE)
CABAL_RUNTIME_STANZAS = {"library", "executable", "foreign-library"}
HACKAGE_STDLIB = {"base"}


def cabal_stanzas(text):
    """[(kind, name, lines)] in file order. The first entry, kind "", holds the package fields."""
    stanzas = [("", "", [])]
    for line in text.splitlines():
        if re.match(r"^\s*--", line):
            continue
        header = CABAL_STANZA.match(line)
        if header:
            stanzas.append((header.group(1).lower(), header.group(2), []))
        else:
            stanzas[-1][2].append(line)
    return stanzas


def cabal_field(lines, name):
    """Every value of a field, continuation lines joined. A field can repeat inside if blocks."""
    pattern = re.compile(rf"^(\s*){re.escape(name)}\s*:(.*)$", re.IGNORECASE)
    values, i = [], 0
    while i < len(lines):
        match = pattern.match(lines[i])
        i += 1
        if not match:
            continue
        indent, value = len(match.group(1)), [match.group(2)]
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            value.append(lines[i])
            i += 1
        values.append(" ".join(value))
    return values


def cabal_own_names(text):
    stanzas = cabal_stanzas(text)
    names = [v.strip() for v in cabal_field(stanzas[0][2], "name")]
    return [n for n in names + [name for kind, name, _ in stanzas if kind == "library" and name] if n]


def parse_cabal(path, ctx, notes):
    text = read_text(path)
    stanzas = cabal_stanzas(text)
    commons = {name: lines for kind, name, lines in stanzas if kind == "common"}
    queue = [lines for kind, _, lines in stanzas if kind in CABAL_RUNTIME_STANZAS]
    bodies, imported = [], set()
    while queue:   # the runtime stanzas, then every common stanza they import
        lines = queue.pop()
        bodies.append(lines)
        for value in cabal_field(lines, "import"):
            for name in re.split(r"[\s,]+", value.strip()):
                if name in commons and name not in imported:
                    imported.add(name)
                    queue.append(commons[name])
    own = {n.lower() for n in cabal_own_names(text)}
    deps = []
    for lines in bodies:
        for value in cabal_field(lines, "build-depends"):
            for item in re.sub(r":\s*\{[^}]*\}", "", value).split(","):   # pkg:{sub1, sub2} is pkg
                match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9-]*)", item)
                if not match or match.group(1).lower() in HACKAGE_STDLIB:
                    continue
                name = match.group(1)
                if name.lower() in own:
                    deps.append(ctx.path_dependency("hackage", name, path, "."))
                else:
                    deps.append(Dependency("hackage", name, REGISTRY, rel(path, ctx.track_dir),
                                           item.strip()[len(name):].strip()))
    return deps


PARSERS = {
    "Cargo.toml": parse_cargo,
    "go.mod": parse_go_mod,
    "package.json": parse_package_json,
    "requirements.txt": parse_requirements,
    "pyproject.toml": parse_pyproject,
    "Pipfile": parse_pipfile,
    "pom.xml": parse_pom,
    "build.gradle": parse_gradle,
    "build.gradle.kts": parse_gradle,
    "msbuild": parse_msbuild,
    "Package.swift": parse_package_swift,
    "cabal": parse_cabal,
}


# ------------------------------------------------------------------ .speed-demon.toml and D

def read_declaration(track_dir):
    """The track's .speed-demon.toml (track, team, language, dependency_count), and a
    problem string when it is missing or unreadable."""
    path = os.path.join(track_dir, SPEED_DEMON_TOML)
    if not os.path.isfile(path):
        return {}, f"no {SPEED_DEMON_TOML} in the track directory"
    try:
        return read_toml(path), None
    except PARSE_ERRORS as e:
        return {}, f"{SPEED_DEMON_TOML} could not be parsed ({e})"


def dependency_weight(scan, declaration):
    """(D, basis, notes). The manifests decide when there are any; otherwise the declared count."""
    notes = []
    declared = declaration.get("dependency_count")
    if declared is not None and (isinstance(declared, bool) or not isinstance(declared, int) or declared < 0):
        notes.append(f"dependency_count in {SPEED_DEMON_TOML} is not a non-negative integer: {declared!r}")
        declared = None
    if scan.manifests:
        counted = len(scan.dependencies)
        if declared is not None and declared != counted:
            notes.append(f"{SPEED_DEMON_TOML} declares dependency_count = {declared} but the manifests "
                         f"declare {counted}; D uses the manifests")
        return 1 + counted, "manifests", notes
    if declared is not None:
        return 1 + declared, "declared, unverified", notes
    notes.append(f"no manifest and no dependency_count in {SPEED_DEMON_TOML}; D assumes no dependencies")
    return 1, "no manifest, nothing declared", notes


# ------------------------------------------------------------------ registry spot-check

USER_AGENT = "speeddemon-static-axes/2026 (+https://speeddemon.dev; organiser spot-check)"
MIN_INTERVAL_S = {"crates.io": 1.0}   # crates.io asks crawlers for at most one request a second
_last_request = {}


class NotFound(Exception):
    pass


def fetch(url):
    """GET url and return the body as text; NotFound on 404. One timeout on every call."""
    host = urllib.parse.urlparse(url).hostname or ""
    wait = MIN_INTERVAL_S.get(host, 0.0) - (time.monotonic() - _last_request.get(host, float("-inf")))
    if wait > 0:
        time.sleep(wait)
    _last_request[host] = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from None
        raise
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body.decode("utf-8")


def utc(text):
    """An ISO 8601 timestamp as written by any of the registries, as an aware UTC datetime."""
    text = re.sub(r"(\.\d{6})\d+", r"\1", text.strip().replace("Z", "+00:00"))
    when = datetime.fromisoformat(text)
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


def first_published_crates(name, get):
    data = json.loads(get(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}"))
    return utc(data["crate"]["created_at"]), ""


def first_published_npm(name, get):
    data = json.loads(get(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}"))
    return utc(data["time"]["created"]), ""


def first_published_pypi(name, get):
    """PyPI has no creation date: the earliest upload across all releases is the first publish."""
    releases = json.loads(get(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json"))["releases"]
    uploads = [utc(f["upload_time_iso_8601"]) for files in releases.values() for f in files]
    if not uploads:
        raise LookupError("the project has no uploaded files")
    return min(uploads), ""


def go_escape(module):
    """proxy.golang.org case-encodes paths: each capital letter becomes ! and its lowercase."""
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), module)


def semver_key(version):
    numbers = [int(n) for n in re.findall(r"\d+", version.split("-")[0].split("+")[0])[:3]]
    return numbers + [0] * (3 - len(numbers)), "-" not in version


GO_VERSIONS_CHECKED = 10


def first_published_go(module, get):
    """The earliest time among the lowest tagged versions. A module with no tags has no
    first-publish date the proxy can give, so it is reported, not guessed."""
    base = f"https://proxy.golang.org/{go_escape(module)}/@v/"
    versions = get(base + "list").split()
    if not versions:
        raise LookupError("no tagged versions on the proxy; first publish date unknown")
    lowest = sorted(versions, key=semver_key)[:GO_VERSIONS_CHECKED]
    times = [utc(json.loads(get(base + f"{go_escape(v)}.info"))["Time"]) for v in lowest]
    detail = "" if len(versions) <= GO_VERSIONS_CHECKED else f"earliest of the {GO_VERSIONS_CHECKED} lowest of {len(versions)} versions"
    return min(times), detail


def first_published_maven(name, get):
    group, _, artifact = name.partition(":")
    if not group or not artifact:
        raise LookupError(f"not a groupId:artifactId coordinate: {name}")
    stamps, start = [], 0
    query = urllib.parse.quote(f'g:"{group}" AND a:"{artifact}"')
    while True:
        data = json.loads(get(f"https://search.maven.org/solrsearch/select?q={query}&core=gav&rows=200&start={start}&wt=json"))
        docs = data["response"]["docs"]
        stamps += [d["timestamp"] for d in docs]
        start += len(docs)
        if not docs or start >= data["response"]["numFound"]:
            break
    if not stamps:
        raise NotFound(name)
    return datetime.fromtimestamp(min(stamps) / 1000, tz=timezone.utc), ""


def first_published_nuget(name, get):
    index = json.loads(get(f"https://api.nuget.org/v3/registration5-gz-semver2/{name.lower()}/index.json"))
    published = []
    for page in index["items"]:
        leaves = page.get("items") or json.loads(get(page["@id"]))["items"]
        published += [utc(leaf["catalogEntry"]["published"]) for leaf in leaves]
    published = [p for p in published if p.year > 1900]   # unlisted versions show 1900-01-01
    if not published:
        raise LookupError("no listed versions")
    return min(published), ""


REGISTRIES = {
    "cargo": ("crates.io", first_published_crates),
    "npm": ("registry.npmjs.org", first_published_npm),
    "pypi": ("pypi.org", first_published_pypi),
    "go": ("proxy.golang.org", first_published_go),
    "maven": ("Maven Central", first_published_maven),
    "nuget": ("nuget.org", first_published_nuget),
}


def check_registry(items, get=fetch, log=sys.stderr):
    """items: dicts with ecosystem, name, source and origin ("direct" or "vendored").
    Returns one result per (ecosystem, name), with status ok, flag (first published
    on or after the cutoff), not-found, not-a-registry-package, unknown or error."""
    merged = {}
    for item in items:
        entry = merged.setdefault((item["ecosystem"], item["name"].lower()),
                                  {"ecosystem": item["ecosystem"], "name": item["name"],
                                   "origin": set(), "source": REGISTRY})
        entry["origin"].add(item["origin"])
        if item["source"] != REGISTRY:
            entry["source"] = item["source"]
    results = []
    for done, (_, entry) in enumerate(sorted(merged.items()), start=1):
        result = {"ecosystem": entry["ecosystem"], "name": entry["name"], "origin": sorted(entry["origin"])}
        registry = REGISTRIES.get(entry["ecosystem"])
        if entry["source"] != REGISTRY:
            result.update(status="not-a-registry-package", detail=f"declared as a {entry['source']} dependency")
        elif registry is None:
            result.update(status="unknown", detail=f"no registry lookup for {entry['ecosystem']}")
        else:
            result["registry"] = registry[0]
            try:
                when, detail = registry[1](entry["name"], get)
                result.update(first_published=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              status="flag" if when >= REGISTRY_CUTOFF else "ok")
                if detail:
                    result["detail"] = detail
            except NotFound:
                result.update(status="not-found", detail=f"not on {registry[0]}")
            except (LookupError, KeyError, ValueError, TypeError, OSError) as e:
                result.update(status="error", detail=f"{type(e).__name__}: {e}")
        results.append(result)
        if done % 20 == 0:
            print(f"registry check: {done} of {len(merged)} looked up", file=log)
    return results
