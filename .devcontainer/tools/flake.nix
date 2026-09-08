{
  # Tools the container bootstrap needs before the workspace dev shell exists.
  # `on-create.sh` installs the `default` output; extend `tools` below.
  #
  # flake.lock pins the same nixpkgs revision as the workspace flake.lock, so
  # the container's store holds one nixpkgs, not two. Keep them in sync.
  description = "Bootstrap tools for the hlsl-dev dev container";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (
        pkgs:
        let
          tools = {
            inherit (pkgs) direnv nix-direnv;
          };
        in
        tools
        // {
          default = pkgs.buildEnv {
            name = "hlsl-dev-container-tools";
            paths = builtins.attrValues tools;
          };
        }
      );
    };
}
