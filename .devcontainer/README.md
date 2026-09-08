# Dev container

A ready-to-use container for the workspace: Ubuntu + Nix + direnv, with the
flake's dev shell pre-built.

The container adds no toolchain of its own. `cmake`, `ninja`, `clang`, `lld`,
`sccache`, `mask`, `wt`, the Vulkan loader and lavapipe all come from
`flake.nix`, so what you build inside the container is what you build outside
it. direnv is what delivers that shell: the hook installed in `~/.bashrc` runs
for every terminal, and for the shell an editor runs to capture a project's
environment — which is how clangd and the Python tooling end up seeing the Nix
toolchain rather than the bare image.

No editor-specific configuration is included: no extensions, no editor
settings. The config is plain dev container spec, so any implementation that
reads it — an editor with dev container support, the `devcontainer` CLI,
Codespaces — should produce the same container.

## Using it

```bash
devcontainer up --workspace-folder .
```

or open the folder in an editor that supports dev containers and accept its
prompt to reopen the project inside the container.

First creation is slow — it realises the dev shell closure, several GB and
tens of minutes on a cold cache. That is cached afterwards, and on GitHub
Codespaces it happens during a prebuild because it runs in `onCreateCommand`.

Re-running `bash .devcontainer/on-create.sh` *inside the container* is safe.
Do not run it on the host: it appends to `~/.bashrc` and `~/.profile`, writes
`~/.config/direnv/direnvrc`, installs into your Nix profile, and sets
`git config --global --add safe.directory '*'`, which is a reasonable trade in
a throwaway container and not on a real machine.

The container does not clone the submodules; that is `mask setup`, from inside
the container or on the host. Once they are there, the normal workflow from
the root `README.md` applies:

```bash
mask setup
mask ls
cd llvm-project && mask build clang
```

## What the container sets up

| Thing | Why |
|---|---|
| Nix feature (multi-user, flakes on) | the store is a named volume keyed by the dev container id, so `/nix` — and the built dev shell — survive a rebuild |
| direnv + nix-direnv | installed from the pinned bootstrap flake in `.devcontainer/tools/`; `.envrc` (`use flake`) then applies to every shell, and nix-direnv caches the dev shell and GC-roots its closure |
| `hlsl-dev-sccache` volume on `.sccache` | the shared compilation cache survives a container rebuild |
| `safe.directory = *` | the workspace bind mount, the submodules and any `wt` worktree are owned by a foreign uid |
| `SYS_PTRACE` + `seccomp=unconfined` | so gdb/lldb can attach to clang and the offload tools |
| Port 10240 forwarded | `mask compiler-explorer` |
| `GH_TOKEN` / `GITHUB_TOKEN` forwarded | `mask monitor` / `mask triage` in `offloader-scripts` |

## Things that vary by implementation

The config sticks to widely supported spec keys — `image`, `features`,
`onCreateCommand`, `mounts`, `containerEnv`, `remoteEnv`, `init`, `capAdd`,
`securityOpt`, `forwardPorts` — so it should behave the same everywhere. Three
things are worth knowing anyway:

- **`hostRequirements` is Codespaces-only.** Everything else ignores it; it is
  kept as documentation of what building LLVM needs.
- **`forwardPorts` uses a number, not a string.** Not every implementation
  honours the string forms.
- **Changing `devcontainer.json` may not rebuild anything.** Some tools offer
  an explicit rebuild command; others match an existing container by its
  `devcontainer.local_folder` / `devcontainer.config_file` labels and reuse it
  unchanged, so edits appear to do nothing and the create-time commands never
  re-run. If in doubt, remove the container and reopen:

  ```bash
  docker ps -a --filter label=devcontainer.local_folder=$PWD
  docker rm -f <container>
  ```

For language servers, the thing to check is direnv. Editors capture a
project's environment by running a shell in the project directory, and it is
the hook `on-create.sh` puts in `~/.bashrc` that turns that into the flake's
`clangd`, `python3` and friends. If your editor has a direnv integration,
leave it enabled; if it has none, point its language servers at the binaries
under the worktree's `build/` and `~/.nix-profile` yourself.

## The pinned tools flake

`.devcontainer/tools/` is a two-file flake providing whatever `on-create.sh`
needs *before* the workspace dev shell exists — currently `direnv` and
`nix-direnv`. Anything else the bootstrap grows a need for goes in the `tools`
attribute set in its `flake.nix`; each entry becomes both its own package and
part of the `default` environment the script installs, so adding one is a
one-line change. Tools that a *build* needs belong in the workspace
`flake.nix` instead.

It exists so that container creation does not depend on the flake registry:
`nix profile install nixpkgs#direnv` would pull whatever `nixos-unstable`
happens to be that day.

Its `flake.lock` pins the **same nixpkgs revision** as the workspace
`flake.lock`. The container's `/nix` is its own volume — nothing is shared with
the host — but `on-create.sh` installs these tools and warms the dev shell into
that one store, so a single pin means nixpkgs is fetched once and `direnv`
links against the same `glibc` and friends the dev shell already pulled in.

That pairing is maintained by hand. After `nix flake update` at the repository
root, bring this one along:

```bash
cd .devcontainer/tools && nix flake update
```

and check that its `nixpkgs` `rev` matches the root's. Nothing breaks if they
drift — the cost is a second nixpkgs source fetch and a duplicate set of base
packages in the container's store, on the order of a few hundred MB.

## Vulkan

The container has no GPU by default, so `HLSL_VK_DRIVER` is left at
`lavapipe`, Mesa's CPU rasterizer. The `vk` and `clang-vk` suites do run, just
slowly — and a test that fails *only* here is far more likely to be hitting a
software-rasterizer limitation than a compiler bug.

To use a real driver, pass the render node through by adding to
`devcontainer.json`:

```jsonc
"runArgs": ["--device=/dev/dri"]
```

then, inside the container:

```bash
mask vk-list
mask vk-use system      # or radeon, intel, ...
mask vk-info
```

`mask vk-use` writes `.env`, which direnv watches, so the change takes effect
in new terminals immediately.

## Sizing and performance

`hostRequirements` asks for 8 CPUs / 16 GB / 128 GB, which is about the
minimum for a comfortable LLVM build. Docker Desktop on macOS and Windows
needs its VM raised to match — and note that bind-mounted source is slow
there, so if builds crawl, keep the workspace in a Docker volume (point
`workspaceMount` at one, or use your tooling's "clone into a container volume"
flow) rather than mounting it from the host.

Build artifacts live inside each worktree (`<worktree>/build`), i.e. on the
workspace mount, not in the image — so they survive a rebuild of the container
and are removed together with the worktree.
