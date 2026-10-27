"""Reject a Dockerfile that could pull in code or data from outside its own repo.

Every FROM must name an allowlisted image pinned by digest, or an earlier
stage of the same Dockerfile. Nothing else may enter the image: no other
image via COPY --from or a mount, no URL via ADD, no custom BuildKit
frontend, no cache mounts shared between teams on the same builder, and no
build-time network. So no code can hide outside the S and D axes, and no
build can reach anything on the machine that runs it.
"""
import json
import re
import sys

INSTRUCTION_RE = re.compile(r"^\s*(?P<op>[A-Za-z]+)\b\s*(?P<rest>.*)$")
AS_RE = re.compile(r"^(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?$", re.IGNORECASE)
FLAG_RE = re.compile(r"--(?P<name>[a-z-]+)(?:=(?P<value>\S+))?")
URL_RE = re.compile(r"^(?:https?|git|ssh|ftp)://|^git@|\.git(?:#\S*)?$", re.IGNORECASE)
DIRECTIVE_RE = re.compile(r"^\s*#\s*(?P<key>[a-z]+)\s*=", re.IGNORECASE)


def logical_lines(text):
    """Dockerfile instructions with backslash continuations joined and comments dropped."""
    buf = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not buf and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        if buf:
            yield buf
        buf = ""
    if buf:
        yield buf


def parser_directives(text):
    """`# syntax=` and friends: only valid before the first instruction."""
    found = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        m = DIRECTIVE_RE.match(stripped)
        if not m:
            break
        found.append(m.group("key").lower())
    return found


def flags_of(rest):
    """Leading --flags of an instruction, as (name, value) pairs."""
    flags = []
    for token in rest.split():
        if not token.startswith("--"):
            break
        m = FLAG_RE.match(token)
        if m:
            flags.append((m.group("name").lower(), m.group("value") or ""))
    return flags


def args_of(rest):
    """Arguments after the --flags, in shell form or the JSON-array form."""
    tokens = rest.split()
    while tokens and tokens[0].startswith("--"):
        tokens.pop(0)
    remainder = " ".join(tokens)
    if remainder.startswith("["):
        try:
            return [str(a) for a in json.loads(remainder)]
        except ValueError:
            pass
    return tokens


def mount_options(value):
    return dict(part.split("=", 1) if "=" in part else (part, "") for part in value.split(","))


def check(dockerfile_text, allowlist):
    """Return a list of problems; empty means the Dockerfile passes."""
    allowed = {f"{entry['image']}@{entry['digest']}" for entry in allowlist}
    stages = []
    problems = []

    for key in parser_directives(dockerfile_text):
        if key == "syntax":
            problems.append("a `# syntax=` directive loads a custom BuildKit frontend; remove it")

    def is_stage(ref):
        return ref.lower() in stages or (ref.isdigit() and int(ref) < len(stages))

    saw_from = False
    for line in logical_lines(dockerfile_text):
        m = INSTRUCTION_RE.match(line)
        if not m:
            continue
        op, rest = m.group("op").upper(), m.group("rest").strip()
        flags = flags_of(rest)

        if op == "FROM":
            saw_from = True
            if flags:
                problems.append(f"FROM flags are not allowed: {line!r}")
                continue
            parts = AS_RE.match(rest)
            if not parts:
                problems.append(f"cannot parse: {line!r}")
                continue
            image, stage = parts.group("image"), parts.group("stage")
            if "$" in image:
                problems.append(f"FROM may not use build arguments: {line!r}")
            elif not is_stage(image) and image not in allowed:
                problems.append(f"base image is not on the allowlist (pin it by digest): {image}")
            stages.append((stage or f"#{len(stages)}").lower())
            continue

        if op == "ONBUILD":
            problems.append(f"ONBUILD is not allowed: {line!r}")
        for name, value in flags:
            if name == "from" and not is_stage(value):
                problems.append(f"{op} --from must name an earlier stage, not an image: {value}")
            if name == "mount":
                opts = mount_options(value)
                kind = opts.get("type", "bind")
                if kind in ("cache", "secret", "ssh"):
                    problems.append(f"RUN --mount=type={kind} is not allowed (builds share one builder)")
                src = opts.get("from")
                if src and not is_stage(src):
                    problems.append(f"RUN --mount from= must name an earlier stage, not an image: {src}")
            if name in ("network", "security", "device"):
                problems.append(f"RUN --{name} is not allowed: {line!r}")
        if op == "ADD":
            sources = args_of(rest)[:-1]
            for src in sources:
                if URL_RE.search(src):
                    problems.append(f"ADD may not fetch a URL or git repo: {src}")
    if not saw_from:
        problems.append("no FROM instruction")
    return problems


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: dockerfile_check.py <Dockerfile> <allowlist.json>\n")
        return 2
    with open(argv[1], encoding="utf-8") as f:
        text = f.read()
    with open(argv[2], encoding="utf-8") as f:
        allowlist = json.load(f)["images"]
    problems = check(text, allowlist)
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
