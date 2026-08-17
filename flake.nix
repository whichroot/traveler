{
  description = "Traveler dev shell: LLVM 21 toolchain + C link driver + make + python3";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
    in
    {
      devShells = forAllSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system}; in
        {
          default = pkgs.mkShell {
            packages = [
              # llc / opt / llvm-objdump at LLVM 21. The emit gate pins
              # LLVM 21 for its optimized profiles (tests/emit/run.sh);
              # tests/lib/env.sh discovers llc/opt from PATH.
              pkgs.llvmPackages_21.llvm
              # mkShell's stdenv already provides cc (bootstrap LINK=cc
              # default) and gnumake (tests/dynfield builds the C seed).
              pkgs.gnumake
              # tests/gpu/run.sh AGX encoder gates and PROOF1 diff run python3.
              pkgs.python3
            ];
          };
        });
    };
}
