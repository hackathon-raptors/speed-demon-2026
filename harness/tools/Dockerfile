# The tools image for harness/static_axes.py: the scc line counter plus the
# canonical formatter of each allowlisted language that has one.
#
#   docker build -t speeddemon/static-tools:2026-10-28 harness/tools
#
# The organiser builds it once, with network. Measuring never uses the network:
# static_axes.py runs this image with --network=none --read-only.
#
# Every tool is pinned. Downloads are pinned by version and by a sha256 for each
# architecture (amd64 is the scoring box, arm64 is a laptop); the rest come from
# the same digest-pinned images as harness/allowlist.json, so gofmt and rustfmt
# are the ones that ship with the toolchains teams build against.
#
#   scc                 4.1.0       line counter
#   rustfmt             1.10.0      Rust 1.99.1, from the allowlisted rust image
#   gofmt               go1.27.3    from the allowlisted golang image
#   clang-format        23.1.3      C and C++ (the binary in the PyPI wheel)
#   ruff                0.17.0      Python (ruff format)
#   google-java-format  1.37.0      Java (native build)
#   prettier            3.9.10      JavaScript and TypeScript, on node 24.22.1
#   fourmolu            0.21.0.0    Haskell (ormolu only ships x86_64 binaries)
#
# Not included: swift-format. It ships only inside the 3 GB Swift toolchain
# and has no standalone release, so Swift is counted as written.

# ---------------------------------------------------------------- downloads
FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS fetch
# fetch <url> <sha256> <file>: download, then refuse the file unless its hash matches.
COPY fetch.py /usr/local/bin/fetch.py
RUN printf '#!/bin/sh\nexec python3 /usr/local/bin/fetch.py "$@"\n' > /usr/local/bin/fetch \
 && chmod +x /usr/local/bin/fetch \
 && mkdir -p /out/bin
WORKDIR /tmp/fetch
ARG TARGETARCH

RUN case "$TARGETARCH" in \
      amd64) f=scc_Linux_x86_64.tar.gz s=c7328436d3027f4357d3d7853f7dc3ac2bbcb4ca08f1adad91a27c593884079b ;; \
      arm64) f=scc_Linux_arm64.tar.gz  s=6e0d2a1f8d3540ba7df185477dec40bb7340f1b214bfd303147de5cad2bd7b8b ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && fetch "https://github.com/boyter/scc/releases/download/v4.1.0/$f" "$s" scc.tar.gz \
 && tar -xzf scc.tar.gz -C /out/bin scc

RUN case "$TARGETARCH" in \
      amd64) a=x86_64  s=0df9421fd0df9aa7a62dab4d1086e085b7c7888b6343fc5a9c5f630771d61aae ;; \
      arm64) a=aarch64 s=cac779097e34bf1ca40175b4bacf8fca7a92fce5030bfc3edf1fd357428a33b0 ;; \
      *) exit 1 ;; \
    esac \
 && fetch "https://github.com/astral-sh/ruff/releases/download/0.17.0/ruff-$a-unknown-linux-musl.tar.gz" "$s" ruff.tar.gz \
 && tar -xzf ruff.tar.gz --strip-components=1 -C /out/bin "ruff-$a-unknown-linux-musl/ruff"

RUN case "$TARGETARCH" in \
      amd64) a=linux-x86-64 s=881821ffd1a52b175886c4ebeeacce2ba5b5d7326b52d32a18d384d8a783bd77 ;; \
      arm64) a=linux-arm64  s=e8c1c4ae92f5a86367500d78ff0d09ad12bf8ad213fa35b372468fcabf281bd3 ;; \
      *) exit 1 ;; \
    esac \
 && fetch "https://github.com/google/google-java-format/releases/download/v1.37.0/google-java-format_$a" "$s" /out/bin/google-java-format \
 && chmod +x /out/bin/google-java-format

RUN case "$TARGETARCH" in \
      amd64) a=linux-x86_64 s=aadf6931653df7b96f52014cf8eb4285a3e8778cd0b531499fc3ee013e1e00c7 ;; \
      arm64) a=linux-arm64  s=8ef665cab33041a9ef702a4f6d7879b0e5988a71eb790fd0841c5c8d07789988 ;; \
      *) exit 1 ;; \
    esac \
 && fetch "https://github.com/fourmolu/fourmolu/releases/download/v0.21.0.0/fourmolu-0.21.0.0-$a.zip" "$s" fourmolu.zip \
 && python3 -m zipfile -e fourmolu.zip fourmolu \
 && install -m 0755 "fourmolu/fourmolu-0.21.0.0-$a/fourmolu" /out/bin/fourmolu

# The clang-format wheel on PyPI is just a packaged clang-format binary; take the binary.
RUN case "$TARGETARCH" in \
      amd64) u=11/d1/658d3da4dcbca98620956868c7cf6f4ba19fccecbdfc30cc055e19c9d4a7/clang_format-23.1.3-py2.py3-none-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl \
             s=58bf1804216501669832a39f2415e14284337438a062ff564729792adec664ac ;; \
      arm64) u=1d/00/a0f99e24dc4892fb120e82fa9cf6e8fcb92714181ffd1165ab7eaf5ba1fd/clang_format-23.1.3-py2.py3-none-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl \
             s=072113527b49a9c0ab9058006d9fe20e583823e6482608a34e1883dd2c49ae7d ;; \
      *) exit 1 ;; \
    esac \
 && fetch "https://files.pythonhosted.org/packages/$u" "$s" clang-format.whl \
 && python3 -m zipfile -e clang-format.whl clang-format \
 && install -m 0755 clang-format/clang_format/data/bin/clang-format /out/bin/clang-format

# prettier is one self-contained npm tarball with no dependencies.
RUN fetch https://registry.npmjs.org/prettier/-/prettier-3.9.10.tgz \
      9dc72045591793dff972432fb78722f7ab91aaa90a7fa8c1261ede55969246b7 prettier.tgz \
 && mkdir -p /out/prettier \
 && tar -xzf prettier.tgz --strip-components=1 -C /out/prettier

# ---------------------------------------------------------------- toolchain formatters
FROM rust:1-slim-trixie@sha256:1a939748a7cbab172de91a014e3804a97af1394a8c844faa67fce9401c4e40fb AS rustfmt
# rustfmt links against the toolchain's librustc_driver and libLLVM through $ORIGIN/../lib,
# so keep its bin/ and lib/ side by side.
RUN rustup component add rustfmt \
 && sysroot="$(rustc --print sysroot)" \
 && mkdir -p /out/rustfmt/bin /out/rustfmt/lib \
 && cp "$sysroot/bin/rustfmt" /out/rustfmt/bin/ \
 && cp -P "$sysroot"/lib/*.so* /out/rustfmt/lib/

FROM golang:1-trixie@sha256:fbb7ae40b62ff8afe31662df570d39b4f5052ac32b90dae8e60d7657a20a74f4 AS gofmt
# gofmt has no --version flag; it is the gofmt of this Go release.
RUN mkdir -p /out && go env GOVERSION > /out/gofmt.version

FROM node:24-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS node

# ---------------------------------------------------------------- the image
FROM debian:13-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
COPY --from=fetch /out/bin/ /usr/local/bin/
COPY --from=fetch /out/prettier /opt/prettier
COPY --from=rustfmt /out/rustfmt /opt/rustfmt
COPY --from=gofmt /usr/local/go/bin/gofmt /usr/local/bin/gofmt
COPY --from=gofmt /out/gofmt.version /usr/local/share/static-tools/gofmt.version
COPY --from=node /usr/local/bin/node /usr/local/bin/node
ENV PATH=/opt/rustfmt/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Neutral configuration files. static_axes.py points rustfmt and fourmolu at these
# so that a rustfmt.toml or fourmolu.yaml in a submission cannot change the result.
# Then record every tool's version, and format one line with each, so a missing
# shared library fails this build rather than a measurement.
RUN printf '#!/bin/sh\nexec node /opt/prettier/bin/prettier.cjs "$@"\n' > /usr/local/bin/prettier \
 && chmod +x /usr/local/bin/prettier \
 && set -e \
 && echo '# rustfmt defaults: static_axes.py passes this file as --config-path' \
      > /usr/local/share/static-tools/rustfmt.toml \
 && fourmolu --print-defaults > /usr/local/share/static-tools/fourmolu.yaml \
 && cd /tmp \
 && echo 'fn main(){println!("x");}' | rustfmt --edition 2021 >/dev/null \
 && echo 'package main;func main(){}' | gofmt >/dev/null \
 && echo 'int main(){return 0;}' | clang-format --style=LLVM >/dev/null \
 && echo 'x=1;y=2' | ruff format --isolated --no-cache - >/dev/null \
 && echo 'class A{void f(){}}' | google-java-format - >/dev/null \
 && echo 'let a=1;let b=2' | prettier --no-config --no-editorconfig --stdin-filepath a.js >/dev/null \
 && echo 'main=pure()' | fourmolu --stdin-input-file a.hs >/dev/null \
 && scc_v="$(scc --version)" \
 && rustfmt_v="$(rustfmt --version)" \
 && gofmt_v="gofmt $(cat /usr/local/share/static-tools/gofmt.version)" \
 && clang_v="$(clang-format --version)" \
 && ruff_v="$(ruff --version)" \
 && gjf_v="$(google-java-format --version 2>&1)" \
 && prettier_v="prettier $(prettier --version) (node $(node --version))" \
 && fourmolu_v="$(fourmolu --version | head -n 1)" \
 && printf '%s\t%s\n' \
      scc "$scc_v" rustfmt "$rustfmt_v" gofmt "$gofmt_v" clang-format "$clang_v" \
      ruff "$ruff_v" google-java-format "$gjf_v" prettier "$prettier_v" fourmolu "$fourmolu_v" \
      > /usr/local/share/static-tools/versions.tsv \
 && cat /usr/local/share/static-tools/versions.tsv

WORKDIR /work
USER 65534:65534
