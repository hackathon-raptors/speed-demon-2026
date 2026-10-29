import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "harness"))

import dockerfile_check  # noqa: E402

PY = "python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962"
ALLOW = [{"image": "python:3.13-slim", "digest": PY.split("@")[1]}]


def problems(text):
    return dockerfile_check.check(text, ALLOW)


class DockerfileCheckTest(unittest.TestCase):
    def test_multi_stage_passes(self):
        self.assertEqual(problems(f"FROM {PY} AS build\nRUN echo hi\nFROM build\nCOPY --from=build /a /b\n"), [])
        self.assertEqual(problems(f"FROM {PY}\nFROM {PY}\nCOPY --from=0 /a /b\n"), [])

    def test_unpinned_or_unknown_base(self):
        self.assertTrue(problems("FROM python:3.13-slim\n"))
        self.assertTrue(problems("FROM ubuntu:24.04\n"))
        self.assertTrue(problems("ARG IMG\nFROM ${IMG}\n"))

    def test_copy_from_foreign_image(self):
        out = problems(f"FROM {PY}\nCOPY --from=postgres:17 /usr/lib /stolen\n")
        self.assertTrue(any("--from" in p for p in out))

    def test_mount_from_foreign_image_and_cache(self):
        self.assertTrue(problems(f"FROM {PY}\nRUN --mount=type=bind,from=nginx,target=/x ls /x\n"))
        self.assertTrue(problems(f"FROM {PY}\nRUN --mount=type=cache,target=/root/.cache pip --version\n"))
        self.assertTrue(problems(f"FROM {PY}\nRUN --mount=type=secret,id=x cat /run/secrets/x\n"))

    def test_add_urls(self):
        self.assertTrue(problems(f"FROM {PY}\nADD http://127.0.0.1:5432/ /x\n"))
        self.assertTrue(problems(f"FROM {PY}\nADD https://github.com/a/b.git /src\n"))
        self.assertTrue(problems(f"FROM {PY}\nADD git@github.com:a/b.git /src\n"))
        self.assertEqual(problems(f"FROM {PY}\nADD vendor.tar.gz /src/\n"), [])
        self.assertTrue(problems(f'FROM {PY}\nADD ["http://169.254.169.254/latest", "/x"]\n'))
        self.assertTrue(problems(f"FROM {PY}\nADD --chmod=644 https://example.com/f /f\n"))

    def test_frontend_and_network_flags(self):
        self.assertTrue(problems(f"# syntax=docker/dockerfile:1\nFROM {PY}\n"))
        self.assertTrue(problems(f"FROM {PY}\nRUN --network=host curl x\n"))
        self.assertTrue(problems(f"FROM {PY}\nRUN --security=insecure id\n"))
        self.assertTrue(problems(f"FROM {PY}\nONBUILD RUN id\n"))

    def test_continuations_are_joined(self):
        text = f"FROM {PY}\nCOPY \\\n  --from=redis:7 /data /x\n"
        self.assertTrue(problems(text))

    def test_comment_or_blank_inside_continuation_does_not_split_it(self):
        self.assertTrue(problems(f"FROM {PY}\nCOPY \\\n# note\n--from=busybox:1.37 /bin/busybox /bb\n"))
        self.assertTrue(problems(f"FROM {PY}\nCOPY \\\n   # note\n--from=busybox:1.37 /bin/busybox /bb\n"))
        self.assertTrue(problems(f"FROM {PY}\nCOPY \\\n\n--from=busybox:1.37 /bin/busybox /bb\n"))
        self.assertTrue(problems(f"FROM {PY}\nRUN \\\n# x\n--network=host curl x\n"))
        self.assertEqual(problems(f"FROM {PY}\nRUN echo a \\\n# note\n  && echo b\n"), [])

    def test_only_the_check_directive_is_allowed(self):
        self.assertTrue(problems(f"# escape=`\nFROM {PY}\nRUN echo \\\nFROM busybox:1.37\n"))
        self.assertTrue(problems(f"#Escape = `\nFROM {PY}\n"))
        self.assertEqual(problems(f"# check=skip=all\nFROM {PY}\n"), [])

    def test_heredoc_stays_allowed(self):
        self.assertEqual(problems(f"FROM {PY}\nRUN <<EOF\n#!/bin/sh\necho hi\nEOF\n"), [])
        self.assertEqual(problems(f"FROM {PY}\nCOPY <<EOF /app/x.txt\nhello\nEOF\n"), [])


if __name__ == "__main__":
    unittest.main()
