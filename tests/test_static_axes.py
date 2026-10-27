"""Tests for the S and D measurement: harness/static_axes.py and harness/dependencies.py.

    python3.13 -m unittest discover -s tests -p 'test_static_axes.py'

The D, vendoring and registry tests need only Python. The S tests run scc and the
formatters in the tools image and skip when Docker or the image is missing:

    docker build -t speeddemon/static-tools:2026-10-28 harness/tools

tests/fixtures/static_axes/ holds one small track directory per ecosystem (Rust,
Go, Python, JavaScript, Java, and Zig for "unknown"), each with runtime and dev
dependencies, vendored code that is and is not a registry package, and a file
the formatter has to expand.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "static_axes")
sys.path.insert(0, os.path.join(ROOT, "harness"))

import dependencies  # noqa: E402
import static_axes  # noqa: E402

# Docker on macOS can mount anything under the home directory, so the S tests keep
# their temporary copies in the repository's (gitignored) .data directory.
WORK = os.path.join(ROOT, ".data", "static-axes-tests")


def docker_problem():
    """None when the S tests can run, else why they cannot."""
    if shutil.which("docker") is None:
        return "docker is not on PATH"
    try:
        probe = subprocess.run(["docker", "image", "inspect", static_axes.TOOLS_IMAGE],
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"docker is not usable: {e}"
    if probe.returncode != 0:
        return f"the tools image is not built; run: {static_axes.BUILD_COMMAND}"
    return None


DOCKER_PROBLEM = docker_problem()


def tearDownModule():
    try:
        os.rmdir(WORK)   # only when empty: each test removes its own copies
    except OSError:
        pass


def fixture(name):
    return os.path.join(FIXTURES, name)


def names(deps):
    return sorted(f"{d.ecosystem}:{d.name}" for d in deps)


def tree_digest(path):
    """One hash over every file name and byte under path."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            h.update(os.path.relpath(full, path).encode())
            with open(full, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


class TrackDirs:
    """Mixin: make throwaway track directories from {relative path: text}."""

    def make_track(self, files, parent=None):
        if parent:
            os.makedirs(parent, exist_ok=True)
        track = tempfile.mkdtemp(prefix="track-", dir=parent)
        self.addCleanup(shutil.rmtree, track, ignore_errors=True)
        for path, text in files.items():
            full = os.path.join(track, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        return track

    def scan(self, files):
        track = self.make_track(files)
        return dependencies.find_direct_dependencies(track)


# ------------------------------------------------------------------ D on the fixtures

class DependencyAxisTest(unittest.TestCase):
    """Runtime dependencies only; dev, test and build ones never count."""

    def axis(self, name):
        return static_axes.dependency_axis(fixture(name))

    def test_rust_counts_target_and_workspace_dependencies_not_dev_or_build(self):
        d = self.axis("rust")
        self.assertEqual(names(d["scan"].dependencies),
                         ["cargo:fast-float2", "cargo:gitdep", "cargo:itoa", "cargo:libc", "cargo:memchr"])
        self.assertEqual(d["D"], 6)
        self.assertEqual(names(d["scan"].local), ["cargo:local"])   # a path crate is the team's code
        # The vendored git crate declares serde; that is its dependency, not the team's.
        self.assertNotIn("cargo:serde", names(d["scan"].dependencies))
        self.assertTrue(any("dependency_count = 4 but the manifests declare 5" in n for n in d["notes"]))

    def test_go_skips_indirect_requires_and_local_replacements(self):
        d = self.axis("go")
        self.assertEqual(names(d["scan"].dependencies), ["go:github.com/a/b", "go:golang.org/x/sys"])
        self.assertEqual(names(d["scan"].local), ["go:example.com/local"])
        self.assertEqual(d["D"], 3)

    def test_python_reads_requirements_includes_and_pyproject(self):
        d = self.axis("python")
        self.assertEqual(names(d["scan"].dependencies),
                         ["pypi:attrs", "pypi:orjson", "pypi:requests", "pypi:sortedcontainers", "pypi:uvloop"])
        self.assertEqual(names(d["scan"].local), ["pypi:localpkg"])
        self.assertEqual(d["D"], 6)
        self.assertTrue(any(n.startswith("requirements-dev.txt: dependencies declared here are not read")
                            for n in d["notes"]))

    def test_javascript_counts_optional_dependencies_and_resolves_aliases(self):
        d = self.axis("javascript")
        self.assertEqual(names(d["scan"].dependencies),
                         ["npm:@scope/pkg", "npm:fsevents", "npm:left-pad", "npm:real-pkg"])
        self.assertEqual(d["D"], 5)

    def test_java_counts_compile_and_runtime_scopes_only(self):
        d = self.axis("java")
        self.assertEqual(names(d["scan"].dependencies),
                         ["maven:com.google.guava:guava", "maven:org.example:runtime-only"])
        self.assertEqual(d["D"], 3)

    def test_unknown_ecosystem_falls_back_to_the_declared_count(self):
        d = self.axis("unknown")
        self.assertEqual(d["D"], 3)
        self.assertEqual(d["D_basis"], "declared, unverified")
        self.assertTrue(any(n.startswith("build.zig.zon: dependencies declared here are not read") for n in d["notes"]))


# ------------------------------------------------------------------ vendored packages

class VendoredPackagesTest(unittest.TestCase):
    """Only code whose own metadata shows it came from a registry is left out of S."""

    def packages(self, name):
        packages = static_axes.dependency_axis(fixture(name))["packages"]
        return packages, static_axes.Ownership(packages)

    def test_cargo_registry_crate_is_excluded_git_crate_and_added_files_are_not(self):
        packages, owner = self.packages("rust")
        self.assertEqual([(p.ecosystem, p.label(), p.roots) for p in packages],
                         [("cargo", "memchr 2.7.4", ["vendor/memchr"])])
        self.assertIs(owner.owner("vendor/memchr/src/lib.rs"), packages[0])
        self.assertIsNone(owner.owner("vendor/memchr/src/extra.rs"))   # not in .cargo-checksum.json
        self.assertIsNone(owner.owner("vendor/gitdep/src/lib.rs"))     # "package": null, vendored from git

    def test_go_vendor_modules_own_only_their_listed_package_directories(self):
        packages, owner = self.packages("go")
        self.assertEqual(sorted(p.label() for p in packages),
                         ["github.com/a/b v1.2.0", "github.com/c/d v0.3.0", "golang.org/x/sys v0.30.0"])
        self.assertEqual(owner.owner("vendor/golang.org/x/sys/unix/u.go").name, "golang.org/x/sys")
        self.assertIsNone(owner.owner("vendor/github.com/a/b/internal/x.go"))   # not in modules.txt
        self.assertIsNone(owner.owner("vendor/example.com/local/local.go"))     # replaced by ./local

    def test_python_distribution_owns_exactly_its_record(self):
        packages, owner = self.packages("python")
        self.assertEqual([p.label() for p in packages], ["sortedcontainers 2.4.0"])
        self.assertIsNotNone(owner.owner("third_party/sortedcontainers/sorteddict.py"))
        self.assertIsNone(owner.owner("third_party/sortedcontainers/added.py"))   # not in RECORD
        self.assertIsNone(owner.owner("third_party/bare/helper.py"))              # no metadata at all

    def test_npm_packages_need_a_package_json(self):
        packages, owner = self.packages("javascript")
        self.assertEqual(sorted(p.label() for p in packages), ["@scope/pkg 1.0.0", "left-pad 1.3.0", "real-pkg 2.0.1"])
        self.assertIsNotNone(owner.owner("node_modules/@scope/pkg/index.js"))
        self.assertIsNone(owner.owner("node_modules/not-a-package/x.js"))

    def test_a_crate_declared_as_git_is_not_a_registry_package(self):
        """Even with a registry checksum, a crate the manifest pulls from git counts in S."""
        track = tempfile.mkdtemp(prefix="track-")
        self.addCleanup(shutil.rmtree, track, ignore_errors=True)
        shutil.copytree(fixture("rust"), track, dirs_exist_ok=True)
        manifest = os.path.join(track, "Cargo.toml")
        with open(manifest, encoding="utf-8") as f:
            text = f.read().replace('memchr = "2"', 'memchr = { git = "https://example.com/memchr.git" }')
        with open(manifest, "w", encoding="utf-8") as f:
            f.write(text)
        d = static_axes.dependency_axis(track)
        self.assertEqual(d["packages"], [])
        self.assertTrue(any("declares memchr as a git dependency" in n for n in d["notes"]))


# ------------------------------------------------------------------ tallying scc's counts

class TallyTest(TrackDirs, unittest.TestCase):
    """tally() turns scc's per-file counts into S; these feed it counts directly, no Docker."""

    def counts(self, language, code, lines=None):
        return {"language": language, "code": code, "comment": 0, "blank": 0, "lines": lines or code}

    def test_a_file_scc_names_differently_still_counts(self):
        copy = self.make_track({"main.c": "int main(void) { return 0; }\n"})
        counts = {"main.c": self.counts("C", 1), "odd\udcff.c": self.counts("C", 40)}
        s = static_axes.tally(copy, counts, static_axes.Ownership([]), {}, {"clang-format": "clang-format 23"})
        self.assertEqual(s["S_lines"], 41)
        self.assertTrue(any("matches no file in the copy" in n for n in s["notes"]))

    def test_prose_data_unrecognised_and_binary_files_are_listed(self):
        copy = self.make_track({"PERF.md": "# Log\n", "data.csv": "a,b\n", "notes.weird": "x\ny\n", "main.c": "int x;\n"})
        with open(os.path.join(copy, "blob.bin"), "wb") as f:
            f.write(b"\0\1\2")
        counts = {"PERF.md": self.counts("Markdown", 1), "data.csv": self.counts("CSV", 1), "main.c": self.counts("C", 1)}
        s = static_axes.tally(copy, counts, static_axes.Ownership([]), {}, {})
        kinds = {e["path"]: (e["kind"], e["lines"]) for e in s["not_counted"]}
        self.assertEqual(kinds, {"PERF.md": ("prose", 1), "data.csv": ("data", 1), "notes.weird": ("unrecognised", 2),
                                 "blob.bin": ("binary", None)})
        self.assertEqual(s["S_lines"], 1)
        self.assertIn("C: counted as written (clang-format is not in the tools image)", s["notes"])


# ------------------------------------------------------------------ manifest parsers

class ManifestParserTest(TrackDirs, unittest.TestCase):

    def test_cargo_workspace_path_dependency_is_local(self):
        scan = self.scan({
            "Cargo.toml": '[workspace]\nmembers = ["app", "core"]\n[workspace.dependencies]\n'
                          'core = { path = "core" }\nryu = "1"\n',
            "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n[dependencies]\n'
                              'core.workspace = true\nryu = { workspace = true, features = ["small"] }\n'
                              '[dev_dependencies]\nproptest = "1"\n',
            "core/Cargo.toml": '[package]\nname = "core"\nversion = "0.1.0"\n',
        })
        self.assertEqual(names(scan.dependencies), ["cargo:ryu"])
        self.assertEqual(names(scan.local), ["cargo:core"])

    def test_go_replace_by_another_module_counts_the_replacement(self):
        scan = self.scan({"go.mod": "module m\n\nrequire (\n\tgithub.com/x/y v1.0.0\n)\n\n"
                                    "replace (\n\tgithub.com/x/y => github.com/fork/y v1.0.1\n)\n"})
        self.assertEqual(names(scan.dependencies), ["go:github.com/fork/y"])

    def test_npm_specs(self):
        scan = self.scan({
            "package.json": json.dumps({"name": "root", "workspaces": ["packages/*"], "dependencies": {
                "lib": "workspace:*", "local-dir": "file:./vendor-src", "from-git": "github:user/repo",
                "shorthand": "user/other", "tarball": "https://example.com/t.tgz", "plain": "^1.0.0"},
                "peerDependencies": {"peer": "^2"}, "devDependencies": {"jest": "^29"}}),
            "packages/lib/package.json": json.dumps({"name": "lib", "version": "1.0.0"}),
            "vendor-src/package.json": json.dumps({"name": "local-dir"}),
        })
        self.assertEqual(names(scan.dependencies), ["npm:from-git", "npm:peer", "npm:plain", "npm:shorthand", "npm:tarball"])
        self.assertEqual({d.name: d.source for d in scan.dependencies}["from-git"], dependencies.GIT)
        self.assertEqual(names(scan.local), ["npm:lib", "npm:local-dir"])

    def test_requirements_edge_cases(self):
        scan = self.scan({
            "requirements.txt": "-r more.txt\n--requirement=more.txt\nZope.Interface[docs] ~= 6.0\n"
                                "pkg-from-git @ git+https://example.com/p.git\n"
                                "-e git+https://example.com/q.git#egg=Q_pkg\n"
                                "./wheels/fast_lib-1.0-py3-none-any.whl\n"
                                "local-src @ file:./src/local\n# just a comment\n",
            "more.txt": "-r requirements.txt\nattrs  # pinned elsewhere\n",
        })
        self.assertEqual(names(scan.dependencies),
                         ["pypi:attrs", "pypi:fast-lib", "pypi:pkg-from-git", "pypi:q-pkg", "pypi:zope-interface"])
        sources = {d.name: d.source for d in scan.dependencies}
        self.assertEqual(sources["fast-lib"], dependencies.FILE)   # a wheel committed to the repo
        self.assertEqual(sources["q-pkg"], dependencies.GIT)
        self.assertEqual(names(scan.local), ["pypi:local-src"])

    def test_pipfile_and_poetry(self):
        scan = self.scan({
            "Pipfile": '[packages]\nflask = "*"\nmine = { path = "./mine", editable = true }\n'
                       '[dev-packages]\npytest = "*"\n',
            "pyproject.toml": '[tool.poetry.dependencies]\npython = "^3.13"\nhttpx = "^0.27"\n'
                              'gitlib = { git = "https://example.com/g.git" }\n'
                              '[tool.poetry.group.dev.dependencies]\nblack = "*"\n',
        })
        self.assertEqual(names(scan.dependencies), ["pypi:flask", "pypi:gitlib", "pypi:httpx"])
        self.assertEqual(names(scan.local), ["pypi:mine"])

    def test_gradle(self):
        scan = self.scan({
            "settings.gradle.kts": 'include(":core")\n',
            "gradle/libs.versions.toml": '[libraries]\nfast-csv = { module = "org.fast:csv", version = "1" }\n'
                                         'kotlinx-io = { group = "org.jetbrains.kotlinx", name = "kotlinx-io-core", version = "0.5" }\n'
                                         '[bundles]\nio = ["kotlinx-io"]\n',
            "app/build.gradle.kts": """\
dependencies {
    implementation(project(":core"))
    implementation("com.google.guava:guava:33.0.0-jre") // a comment with "quotes:x:y"
    api(libs.fast.csv)
    runtimeOnly(libs.bundles.io)
    implementation(platform("org.example:bom:1"))
    implementation(kotlin("stdlib"))
    implementation(files("libs/local.jar"))
    compileOnly("org.example:compile-only:1")
    testImplementation("junit:junit:4.13.2")
    /* implementation("org.example:commented-out:1") */
}
""",
            "core/build.gradle": "dependencies {\n    implementation group: 'org.example', name: 'groovy-style', version: '1'\n}\n",
        })
        self.assertEqual(names(scan.dependencies),
                         ["maven:com.google.guava:guava", "maven:local file \"libs/local.jar\"",
                          "maven:org.example:groovy-style", "maven:org.fast:csv",
                          "maven:org.jetbrains.kotlinx:kotlinx-io-core"])
        self.assertEqual(names(scan.local), ["maven:project :core"])

    def test_msbuild(self):
        scan = self.scan({
            "App/App.csproj": """\
<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageReference Include="StyleCop.Analyzers" Version="1.1.118" PrivateAssets="all" />
    <PackageReference Include="Microsoft.SourceLink.GitHub"><PrivateAssets>all</PrivateAssets></PackageReference>
    <PackageReference Include="System.IO.Pipelines"><Version>9.0.0</Version></PackageReference>
    <ProjectReference Include="..\\Core\\Core.csproj" />
  </ItemGroup>
</Project>
""",
            "Core/Core.fsproj": '<Project><ItemGroup><PackageReference Include="FSharp.Core" Version="9.0.0" />'
                                '</ItemGroup></Project>\n',
        })
        self.assertEqual(names(scan.dependencies), ["nuget:Newtonsoft.Json", "nuget:System.IO.Pipelines"])
        self.assertEqual(names(scan.local), ["nuget:Core.csproj"])

    def test_package_swift(self):
        scan = self.scan({"Package.swift": """\
// swift-tools-version:6.0  (the // in "https://" below is not a comment)
import PackageDescription
let package = Package(name: "Fast", dependencies: [
    .package(url: "https://github.com/apple/swift-collections.git", from: "1.1.0"),
    .package(name: "Named", url: "https://example.com/x/whatever", branch: "main"),
    .package(path: "LocalKit"),
    // .package(url: "https://github.com/commented/out.git", from: "1.0.0"),
])
"""})
        self.assertEqual(names(scan.dependencies), ["swiftpm:Named", "swiftpm:swift-collections"])
        self.assertEqual({d.source for d in scan.dependencies}, {dependencies.GIT})
        self.assertEqual(names(scan.local), ["swiftpm:LocalKit"])

    def test_cabal(self):
        scan = self.scan({"fast.cabal": """\
cabal-version: 3.0
name:          fast
version:       0.1.0

common shared
  build-depends: base >=4.18 && <5,
                 bytestring

library
  import:          shared
  exposed-modules: Fast
  build-depends:   containers ^>=0.7,
                   vector

executable fast
  main-is:       Main.hs
  build-depends: base, fast, fast:{internal}
  if flag(simd)
    build-depends: primitive

test-suite spec
  type:          exitcode-stdio-1.0
  main-is:       Spec.hs
  build-depends: base, hspec
"""})
        self.assertEqual(names(scan.dependencies),
                         ["hackage:bytestring", "hackage:containers", "hackage:primitive", "hackage:vector"])
        self.assertEqual(names(scan.local), ["hackage:fast"])

    def test_declared_count_is_used_only_without_manifests(self):
        track = self.make_track({".speed-demon.toml": 'track = "b"\ndependency_count = 3\n', "main.c": "int main(){}\n"})
        d = static_axes.dependency_axis(track)
        self.assertEqual((d["D"], d["D_basis"]), (4, "declared, unverified"))
        track = self.make_track({".speed-demon.toml": 'dependency_count = "two"\n'})
        d = static_axes.dependency_axis(track)
        self.assertEqual((d["D"], d["D_basis"]), (1, "no manifest, nothing declared"))
        self.assertTrue(any("not a non-negative integer" in n for n in d["notes"]))
        d = static_axes.dependency_axis(self.make_track({"main.c": "int main(){}\n"}))
        self.assertEqual(d["D"], 1)
        self.assertTrue(any("no .speed-demon.toml" in n for n in d["notes"]))

    def test_path_outside_the_track_counts_and_is_noted(self):
        scan = self.scan({"Cargo.toml": '[package]\nname = "x"\nversion = "0.1.0"\n'
                                        '[dependencies]\nshared = { path = "../shared" }\n'})
        self.assertEqual(names(scan.dependencies), ["cargo:shared"])
        self.assertTrue(any("points outside the track directory" in n for n in scan.notes))

    def test_unparseable_manifest_is_noted_not_fatal(self):
        scan = self.scan({"Cargo.toml": "[package\nname = \n", "package.json": '{"dependencies": {"a": "1"}}'})
        self.assertEqual(names(scan.dependencies), ["npm:a"])
        self.assertTrue(any(n.startswith("Cargo.toml: could not be parsed") for n in scan.notes))


# ------------------------------------------------------------------ registry spot-check

class FakeRegistry:
    """Stands in for dependencies.fetch: URL -> body, NotFound for anything else."""

    def __init__(self, responses):
        self.responses = responses

    def __call__(self, url):
        if url not in self.responses:
            raise dependencies.NotFound(url)
        body = self.responses[url]
        return body if isinstance(body, str) else json.dumps(body)


class RegistryCheckTest(unittest.TestCase):

    def check(self, items, responses):
        results = dependencies.check_registry(items, get=FakeRegistry(responses), log=io.StringIO())
        return {r["name"]: r for r in results}

    def item(self, ecosystem, name, source=dependencies.REGISTRY, origin="direct"):
        return {"ecosystem": ecosystem, "name": name, "source": source, "origin": origin}

    def test_flags_packages_first_published_on_or_after_the_cutoff(self):
        results = self.check(
            [self.item("cargo", "memchr"), self.item("cargo", "brand-new"), self.item("cargo", "memchr", origin="vendored"),
             self.item("npm", "@scope/pkg"), self.item("pypi", "sortedcontainers")],
            {"https://crates.io/api/v1/crates/memchr": {"crate": {"created_at": "2015-06-10T22:14:46.462135+00:00"}},
             "https://crates.io/api/v1/crates/brand-new": {"crate": {"created_at": "2026-10-05T08:00:00.000000+00:00"}},
             "https://registry.npmjs.org/@scope%2Fpkg": {"time": {"created": "2026-10-01T00:00:00.000Z"}},
             "https://pypi.org/pypi/sortedcontainers/json": {"releases": {
                 "2.4.0": [{"upload_time_iso_8601": "2021-05-16T22:03:25.264563Z"}],
                 "0.8.0": [{"upload_time_iso_8601": "2014-05-01T10:00:00.000000Z"}],
                 "yanked-empty": []}}})
        self.assertEqual(results["memchr"]["status"], "ok")
        self.assertEqual(results["memchr"]["origin"], ["direct", "vendored"])
        self.assertEqual(results["brand-new"]["status"], "flag")
        self.assertEqual(results["@scope/pkg"]["status"], "flag")         # the cutoff itself is "on or after"
        self.assertEqual(results["sortedcontainers"]["first_published"], "2014-05-01T10:00:00Z")

    def test_go_proxy_paths_are_case_encoded_and_the_earliest_version_wins(self):
        base = "https://proxy.golang.org/github.com/!burnt!sushi/toml/@v/"
        results = self.check([self.item("go", "github.com/BurntSushi/toml")], {
            base + "list": "v1.4.0\nv0.3.1\nv1.0.0\n",
            base + "v0.3.1.info": {"Version": "v0.3.1", "Time": "2018-08-22T19:23:00Z"},
            base + "v1.0.0.info": {"Version": "v1.0.0", "Time": "2022-01-10T00:00:00Z"},
            base + "v1.4.0.info": {"Version": "v1.4.0", "Time": "2024-06-01T00:00:00Z"}})
        self.assertEqual(results["github.com/BurntSushi/toml"]["first_published"], "2018-08-22T19:23:00Z")

    def test_maven_pages_through_every_version(self):
        url = ("https://search.maven.org/solrsearch/select?q=g%3A%22com.google.guava%22%20AND%20a%3A%22guava%22"
               "&core=gav&rows=200&start={}&wt=json")
        results = self.check([self.item("maven", "com.google.guava:guava")], {
            url.format(0): {"response": {"numFound": 3, "docs": [{"timestamp": 1700000000000}, {"timestamp": 1600000000000}]}},
            url.format(2): {"response": {"numFound": 3, "docs": [{"timestamp": 1270000000000}]}}})
        self.assertEqual(results["com.google.guava:guava"]["first_published"], "2010-03-31T01:46:40Z")

    def test_nuget_skips_unlisted_versions_and_follows_pages(self):
        index = "https://api.nuget.org/v3/registration5-gz-semver2/newtonsoft.json/index.json"
        page = "https://api.nuget.org/v3/registration5-gz-semver2/newtonsoft.json/page/1.json"
        results = self.check([self.item("nuget", "Newtonsoft.Json")], {
            index: {"items": [{"items": [{"catalogEntry": {"published": "1900-01-01T00:00:00+00:00"}},
                                         {"catalogEntry": {"published": "2019-11-09T01:27:30.7240000+00:00"}}]},
                              {"@id": page}]},
            page: {"items": [{"catalogEntry": {"published": "2011-01-08T22:12:57.713+00:00"}}]}})
        self.assertEqual(results["Newtonsoft.Json"]["first_published"], "2011-01-08T22:12:57Z")

    def test_reports_what_it_cannot_check_instead_of_guessing(self):
        results = self.check(
            [self.item("hackage", "vector"), self.item("cargo", "gitdep", source=dependencies.GIT),
             self.item("npm", "missing"), self.item("pypi", "garbled"), self.item("go", "example.com/untagged")],
            {"https://pypi.org/pypi/garbled/json": "not json",
             "https://proxy.golang.org/example.com/untagged/@v/list": ""})
        self.assertEqual(results["vector"]["status"], "unknown")
        self.assertEqual(results["gitdep"]["status"], "not-a-registry-package")
        self.assertEqual(results["missing"]["status"], "not-found")
        self.assertEqual(results["garbled"]["status"], "error")
        self.assertEqual(results["example.com/untagged"]["status"], "error")
        self.assertIn("first publish date unknown", results["example.com/untagged"]["detail"])


# ------------------------------------------------------------------ S, in the tools image

class DockerChecksTest(unittest.TestCase):

    def test_the_tools_container_is_sandboxed_and_can_be_pinned(self):
        cmd = static_axes.Tools("img:1", "/w", cpuset="12-19").command(["scc", "--version"], env={"TIMEOUT": "5"})
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--cpuset-cpus=12-19", "/w:/work", "TIMEOUT=5"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[-3:], ["img:1", "scc", "--version"])
        self.assertNotIn("--cpuset-cpus", " ".join(static_axes.Tools("img:1", "/w").command(["scc"])))

    def test_options_are_keyword_only(self):
        with self.assertRaises(TypeError):
            static_axes.measure(FIXTURES, "a")   # a stray positional argument must not become the image

    def test_missing_docker_is_a_clear_error(self):
        with mock.patch.object(static_axes.shutil, "which", return_value=None):
            with self.assertRaisesRegex(static_axes.ToolError, "not on PATH"):
                static_axes.check_docker(static_axes.TOOLS_IMAGE)

    @unittest.skipIf(shutil.which("docker") is None, "docker is not on PATH")
    def test_missing_image_names_the_build_command(self):
        with self.assertRaises(static_axes.ToolError) as caught:
            static_axes.check_docker("speeddemon/static-tools:does-not-exist")
        self.assertTrue("docker build" in str(caught.exception) or "daemon" in str(caught.exception))


EXPECTED_S = {"rust": 18, "go": 16, "python": 17, "javascript": 10, "java": 10, "unknown": 2}


@unittest.skipIf(DOCKER_PROBLEM, DOCKER_PROBLEM or "")
class SourceSizeTest(TrackDirs, unittest.TestCase):
    reports = {}

    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)
        cls.before = tree_digest(FIXTURES)
        cls.reports = {name: static_axes.measure(fixture(name), work_root=WORK) for name in EXPECTED_S}
        cls.after = tree_digest(FIXTURES)

    def measure_files(self, files, **kw):
        return static_axes.measure(self.make_track(files, parent=WORK), work_root=WORK, **kw)

    def test_s_for_each_fixture(self):
        self.assertEqual({name: r["S_lines"] for name, r in self.reports.items()}, EXPECTED_S)

    def test_d_is_reported_alongside(self):
        self.assertEqual({name: r["D"] for name, r in self.reports.items()},
                         {"rust": 6, "go": 3, "python": 6, "javascript": 5, "java": 3, "unknown": 3})

    def test_the_submission_is_never_modified(self):
        self.assertEqual(self.before, self.after)

    def test_registry_packages_are_excluded_and_listed(self):
        excluded = {name: [e["path"] for e in r["excluded_paths"]] for name, r in self.reports.items()}
        self.assertEqual(excluded["rust"], ["vendor/memchr"])
        self.assertEqual(excluded["go"], ["vendor/github.com/a/b", "vendor/github.com/c/d", "vendor/golang.org/x/sys"])
        self.assertEqual(excluded["python"], ["third_party/sortedcontainers", "third_party/sortedcontainers-2.4.0.dist-info"])
        self.assertEqual(excluded["javascript"], ["node_modules/@scope/pkg", "node_modules/fast-alias", "node_modules/left-pad"])
        memchr = self.reports["rust"]["excluded_paths"][0]
        self.assertEqual((memchr["package"], memchr["ecosystem"], memchr["code_lines"]), ("memchr 2.7.4", "cargo", 3))

    def test_vendored_code_that_is_not_a_registry_package_counts_and_is_flagged(self):
        expected = {"rust": ["vendor/memchr/src/extra.rs: inside vendored package", "vendor/gitdep: gitdep 0.1.0 is not a registry package"],
                    "go": ["vendor/github.com/a/b/internal/x.go: inside vendored package",
                           "vendor/example.com/local: example.com/local v0.0.0 is not a registry package"],
                    "python": ["third_party/sortedcontainers/added.py: inside vendored package",
                               "third_party: 3 code lines in here counted"],
                    "javascript": ["node_modules: 1 code line in here counted"]}
        for name, starts in expected.items():
            for start in starts:
                self.assertTrue(any(n.startswith(start) for n in self.reports[name]["notes"]), (name, start))

    def test_prose_data_and_lockfiles_are_listed_not_counted(self):
        rust = {e["path"]: e for e in self.reports["rust"]["not_counted"]}
        self.assertEqual(rust["README.md"]["kind"], "prose")
        self.assertEqual(rust["Cargo.toml"]["kind"], "data")
        self.assertFalse(self.reports["rust"]["per_language"]["Markdown"]["counted"])
        javascript = {e["path"]: e["kind"] for e in self.reports["javascript"]["not_counted"]}
        self.assertEqual(javascript["package-lock.json"], "lockfile")

    def test_formatters_are_recorded_and_missing_ones_said(self):
        used = {name: r["formatter_used"] for name, r in self.reports.items()}
        self.assertTrue(used["rust"]["Rust"].startswith("rustfmt 1.10.0"))
        self.assertTrue(used["javascript"]["JavaScript"].startswith("prettier 3.9.10"))
        self.assertEqual(used["unknown"], {"Zig": None})
        self.assertIn("Zig: counted as written (no formatter for it in the tools image)", self.reports["unknown"]["notes"])

    def test_go_lines_with_several_statements_are_flagged(self):
        self.assertTrue(any(n.startswith("main.go: 1 line with more than one statement") for n in self.reports["go"]["notes"]))

    def test_minified_files_count_as_formatted(self):
        """One-line files expand under their formatter, and configuration in the submission is ignored."""
        cases = {
            "rust": ({"main.rs": "fn main() { let a = 1; let b = 2; if a < b { println!(\"{}\", a + b); } }\n",
                      "rustfmt.toml": 'max_width = 300\nuse_small_heuristics = "Max"\n'}, 7),
            "javascript": ({"index.js": "const a=1;const b=2;function add(x,y){return x+y}console.log(add(a,b));\n",
                            ".prettierrc": '{"printWidth": 1000}\n', ".prettierignore": "*\n",
                            ".editorconfig": "[*]\nmax_line_length = 1000\n"}, 6),
            "python": ({"main.py": "import sys; total = 0; count = 0; print(total, count, file=sys.stderr)\n",
                        "pyproject.toml": "[tool.ruff]\nline-length = 300\n"}, 4),
            # Counted code in places formatters skip by default is still formatted.
            "javascript under node_modules": ({"node_modules/plain/x.js": "const a=1;const b=2;console.log(a+b);\n"}, 3),
            "python under build": ({"build/x.py": "a = 1; b = 2; print(a + b)\n"}, 3),
            "java": ({"Main.java": "public class Main { public static void main(String[] a) { int x = 1; System.out.println(x); } }\n"}, 6),
            "c": ({"main.c": "int main(void){int a=1;int b=2;return a+b;}\n",
                   ".clang-format": "BasedOnStyle: LLVM\nColumnLimit: 300\nAllowShortFunctionsOnASingleLine: All\n",
                   ".clang-format-ignore": "*\n"}, 5),
        }
        for language, (files, expected) in cases.items():
            with self.subTest(language=language):
                self.assertEqual(self.measure_files(files)["S_lines"], expected)

    def test_comments_and_blank_lines_are_not_code(self):
        report = self.measure_files({"counts.c": """\
/* A block comment
   over two lines. */

// A line comment.
int add(int a, int b) {
  int sum = a + b; // a trailing comment leaves this a code line
  return sum;
}

int main(void) {
  /* between */
  return add(1, 2);
}
"""})
        self.assertEqual(report["S_lines"], 7)
        self.assertEqual(report["per_language"]["C"], {"files": 1, "code": 7, "comment": 4, "blank": 2, "counted": True})

    def test_a_file_the_formatter_rejects_is_counted_as_written(self):
        report = self.measure_files({"broken.rs": "fn main( { let a = 1; let b = 2; }\n"})
        self.assertEqual(report["S_lines"], 1)
        self.assertTrue(any(n.startswith("broken.rs: counted as written (rustfmt exit 1") for n in report["notes"]))

    def test_long_lines_after_formatting_are_flagged(self):
        report = self.measure_files({"main.go": 'package main\n\nvar s = "' + "x" * 250 + '"\n'})
        self.assertTrue(any(n.startswith("main.go: 1 line longer than 200 characters") for n in report["notes"]))

    def test_reference_axes_file_is_current(self):
        with open(static_axes.REFERENCE_AXES, encoding="utf-8") as f:
            recorded = json.load(f)
        self.assertEqual(sorted(recorded), ["a", "b", "c", "d"])
        for track, filename in static_axes.REFERENCE_FILES.items():
            with open(os.path.join(ROOT, "reference", filename), encoding="utf-8") as f:
                report = self.measure_files({filename: f.read()}, exempt_published=False)
            self.assertEqual(recorded[track], {"S_lines": report["S_lines"]}, track)

    def test_command_line(self):
        out = os.path.join(self.make_track({}, parent=WORK), "report.json")
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "harness", "static_axes.py"),
                               "--track-dir", fixture("unknown"), "--work", WORK, "--json", out],
                              capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(json.loads(proc.stdout), written)
        self.assertEqual((written["S_lines"], written["D"]), (2, 3))


if __name__ == "__main__":
    unittest.main()


class PublishedCopiesTest(unittest.TestCase):
    """Byte-identical copies of the published generators and references cost nothing in S."""

    def test_identical_copy_is_found_and_edited_copy_is_not(self):
        import shutil
        import tempfile
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        original = os.path.join(root, "generators", "a_ingest.py")
        with tempfile.TemporaryDirectory() as track:
            os.makedirs(os.path.join(track, "training"))
            shutil.copy(original, os.path.join(track, "training", "a_ingest.py"))
            with open(original, encoding="utf-8") as f:
                edited = f.read() + "\n# tuned\n"
            with open(os.path.join(track, "gen_edited.py"), "w", encoding="utf-8") as f:
                f.write(edited)
            self.assertEqual(static_axes.published_copies(track), {"training/a_ingest.py"})
