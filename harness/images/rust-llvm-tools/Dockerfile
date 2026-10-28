# The allowlisted rust:1-trixie with rustup's llvm-tools component, so llvm-profdata
# (and the other LLVM tools) match rustc's own LLVM. Built for linux/amd64 and linux/arm64.
FROM rust:1-trixie@sha256:931c2c0531a8b143e044f0f3914dfe802f15bb3d1955f0b657e9359327ac2ffc
RUN rustup component add llvm-tools \
 && for f in "$(rustc --print sysroot)"/lib/rustlib/*/bin/llvm-*; do ln -s "$f" /usr/local/bin/; done \
 && rm -rf /usr/local/rustup/downloads /usr/local/rustup/tmp \
 && llvm-profdata --version
LABEL org.opencontainers.image.source="https://github.com/hackathon-raptors/speed-demon-2026" \
      org.opencontainers.image.description="Speed Demon 2026: rust:1-trixie (rustc 1.99.1) with the llvm-tools component"
