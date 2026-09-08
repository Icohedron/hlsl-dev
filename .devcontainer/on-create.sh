#!/usr/bin/env bash
# Bootstrap the dev container. See .devcontainer/README.md.
set -euo pipefail

WORKSPACE=$(cd "$(dirname "$0")/.." && pwd)

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# This edits ~/.bashrc, ~/.profile and the global git config, which is fine in
# a container and not on a developer's machine. Docker leaves /.dockerenv,
# Podman /run/.containerenv.
if [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ]; then
  echo "refusing to run outside a container; see .devcontainer/README.md" >&2
  exit 1
fi

# The nix feature puts /nix/var/nix/profiles/default/bin on PATH, but not the
# user profile that `nix profile install` writes to.
export PATH="$HOME/.nix-profile/bin:$PATH"
nix --version

log "Installing the bootstrap tools"
# `path:` keeps this a plain-directory flake; a bare path inside a git
# repository is read as `git+file:` and ignores uncommitted files.
nix profile list 2>/dev/null | grep -q hlsl-dev-container-tools ||
  nix profile install "path:$WORKSPACE/.devcontainer/tools"

log "Configuring direnv"
mkdir -p "$HOME/.config/direnv"
grep -qs nix-direnv "$HOME/.config/direnv/direnvrc" ||
  echo "source $HOME/.nix-profile/share/nix-direnv/direnvrc" \
    >>"$HOME/.config/direnv/direnvrc"

grep -qs 'direnv hook' "$HOME/.bashrc" ||
  printf '\n# hlsl-dev devcontainer\neval "$(direnv hook bash)"\n' >>"$HOME/.bashrc"

# Editors open a login shell, which reads .profile, not .bashrc.
grep -qs bashrc "$HOME/.profile" ||
  printf '\n# hlsl-dev devcontainer\nif [ -n "$BASH_VERSION" ] && [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi\n' \
    >>"$HOME/.profile"

# The bind mount, the submodules and any `wt` worktree beside them are owned by
# a foreign uid.
log "Configuring git"
git config --global --get-all safe.directory | grep -qx '\*' ||
  git config --global --add safe.directory '*'

# The named volume is created root-owned.
if [ -d "$WORKSPACE/.sccache" ] && [ ! -w "$WORKSPACE/.sccache" ]; then
  log "Taking ownership of .sccache"
  sudo chown -R "$(id -u):$(id -g)" "$WORKSPACE/.sccache"
fi

log "Building the Nix dev shell"
cd "$WORKSPACE"
nix develop --command true
direnv allow .

cat <<'EOF'

  hlsl-dev is ready. Open a new terminal (direnv enters the dev shell), then
  `mask setup` to clone the submodules if they are not checked out yet.
  `mask ls` shows what exists; `mask info` what it builds against.

  Vulkan defaults to the lavapipe software rasterizer, so vk / clang-vk
  results reflect a CPU implementation.

EOF
