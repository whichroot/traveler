# Traveler

Traveler is a systems language for integer, finite-field, and polynomial
computation. Its self-hosted compiler produces LLVM IR and native executables.
Source files use the `.tv` extension.

```traveler
type F = Field<251>;

fn main() {
    let a: F = 200;
    let b: F = 100;
    print(a + b);  // 49
    print(a * b);  // 171
    print(a / b);  // 2
}
```

## Build and run

Use LLVM 21 and a system linker driver (`cc` or `clang`). The raw IR path
supports LLVM 15+; the optional optimization profiles require LLVM 21.
Bash and coreutils are needed for the build scripts.

On Nix/NixOS, `nix develop` supplies the toolchain. For other systems, see
[BUILD.md](BUILD.md). Run these commands from the repository root, with `llc`
and `cc` on `PATH`:

```sh
src/bootstrap/build.sh
src/bootstrap/out/stage1 examples/field_basics.tv --emit exe -o /tmp/field-basics
/tmp/field-basics
```

The example prints `49`, `100`, `171`, `2`, `123`, and `1`, each on its own line.
Set `LLC` for the bootstrap script or pass `-llc /path/to/llc` to the compiler
when LLVM is not on `PATH`.

The bootstrap starts from checked-in LLVM IR, compiles the current compiler
source, and checks its self-hosting fixed point. The canonical build compiles
no C source. See [bootstrap provenance](src/bootstrap/PROVENANCE.md).

## Language and libraries

- **Numeric types:** fixed-width integers, `Field<p>`, binary and quadratic
  extension fields, runtime field carriers, and the bound/scale families
  `Int<B>`, `Exp<E>`, and `Fix<B,E>`.
- **Static abstraction:** generic functions, structs and enums, traits,
  operator overloading, and non-escaping closures. Function pointers support
  explicit indirect calls.
- **Parallel loops:** the compiler analyzes ordinary `for` loops for iteration
  independence. Runtime alias checks and dispatch policy can select serial
  execution. Reports explain admission and fallback decisions.
- **Device code:** NVIDIA, AMD, Vulkan, and measured Apple AGX profiles, with
  backend-specific admission rules and runtimes written in Traveler.
- **Libraries:** polynomial codecs, error correction, number theory, RNS and
  fixed-point kernels, collections, files, networking, graphics, and GPU support.

Traveler has no floating-point source types. Explicit IEEE bit-pattern
operations and CUDA native tensor operations have separate numerical contracts.

## Documentation

| Start here | Reference |
|---|---|
| Language syntax and behavior | [Language specification](spec/language-spec.md) |
| Toolchain, compiler flags, and backend setup | [Build guide](BUILD.md) |
| Working programs | [Examples](examples/) |
| Writing Traveler with an agent | [Agent guide](agent-instructions/AGENTS.md) |
| Compiler and library contributions | [Contributing](CONTRIBUTING.md) |
| Repository work rules | [AGENTS.md](AGENTS.md) |

The compiler is in `src/tvc_self.tv`, libraries in `src/lib/`, and the formatter
and documentation generator in `src/tools/`. The language server is in
`tools/tv-lsp/`. Backend and numerical contracts live under `spec/`.

## Tests

```sh
tests/run_all.sh --list       # Show the local toolchain and suite plan
tests/run_all.sh              # Run the environment-aware gate
tests/run_all.sh --suite=gpu  # Run device codegen and available runtime checks
```

Hardware-dependent checks can skip when the required device or driver is absent.
The [build guide](BUILD.md) describes individual suites and overrides.

## Status

Traveler is an early-alpha research project. The cryptographic/ZK proof stack
is quarantined and under repair; it is not a security guarantee. See
[SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## License

Apache License 2.0. See [LICENSE](LICENSE). Contributions use a DCO sign-off;
see [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).
