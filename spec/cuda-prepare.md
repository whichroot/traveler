# CUDA source identity and prepared package reuse

`tools/cuda_prepare.py` prepares ordinary `TVCP0001` PTX packages from a closed
source snapshot. The compiler remains responsible for language semantics,
specialization, verification, and kernel descriptors. LLVM emits PTX. The
checked Traveler runtime loads the resulting package through the CUDA driver.

## Dependency discovery and snapshots

The compiler query `--dependencies` emits a JSON array containing the entry
source and transitive imported paths, in import-discovery order. It runs the
normal lexer and import resolver and exits before parsing or code generation.
It is a dependency query, not a typechecking result. Missing imports fail the
query.

The preparation tool takes a root directory and root-relative entry path.
Imports must resolve to regular source paths inside that root. Absolute imports
and symlink dependencies are refused in this distributable profile. Parent
directory imports within the root are supported. Source files must be smaller
than the compiler's 16 MiB per-file read limit.

The tool captures dependency bytes, copies them into a private tree with the
same relative layout, and reruns dependency discovery there. The snapshot must
have exactly the captured dependency inventory and bytes before it is compiled.
Compilation reads this snapshot. A concurrent source edit can affect a later
preparation request; it cannot silently change this captured build's inputs.

## Two identities

The request key is SHA-256 of canonical JSON with:

- Preparation schema and package ABI version.
- Relative entry path and sorted transitive path/content-digest inventory.
- Compiler executable digest, LLVM executable digest and version output.
- Digests of the preparer and packager implementations.
- A caller-supplied immutable toolchain-closure identity.
- Target triple, SM, and the fixed raw-IR optimization profile.

`--toolchain-id` identifies the complete toolchain environment, including shared
libraries and other executable dependencies. Use an immutable environment or
closure digest and change it when those dependencies change. Executable hashes
alone do not identify a dynamically linked toolchain's entire environment.

The artifact identity hashes the request key and complete resulting package
manifest. The manifest includes PTX identity and all kernel descriptors:
specialization/type arguments, parameter layouts, effects, numerical/tensor
policies, and target capabilities. Source instantiations and explicit operation
choices are inputs to the request; compiler-derived descriptors are outputs
bound into the artifact identity. There are no extra unkeyed compiler options.

An unchanged source tree relocated under a different root can reuse the same
entry with the same toolchain identity. Editing an unimported file does not
invalidate it. Imported contents, compiler/toolchain changes, target changes,
and policy or type changes in source produce different requests.

## Cache and publication

Each `<request-key>.json` cache entry contains the request record, base64-encoded
package, whole-package digest, and artifact identity. This record can accompany
the ordinary `.tvcp` and matching relative-layout source tree for distribution.

A warm hit requires exact canonical request identity, bounded cache/package
sizes, matching package and PTX hashes, closed kernel schemas, entry-symbol
agreement, and sufficient target capabilities. A malformed or incompatible
entry is rebuilt from the snapshot and reported as `rebuild-corrupt`. Successful
reuse is `hit`; a new request is `miss`. Dependency discovery, hashing, and
validation still run on a hit; compiler code generation and LLVM lowering do not.

Cache entries and output packages are individually staged, fsynced, atomically
replaced, and followed by parent-directory fsync. Concurrent builders may do
redundant work; readers never observe a partially written entry. A failed build
preserves any previous output. A successful cache publication followed by an
output-publication failure leaves a reusable cache entry. Filesystem write
errors are reported rather than presented as successful preparation.

The command emits a JSON report with request/artifact/package identities,
cache status, dependency count, `discovery_ns`, and `prepare_publish_ns`.
These are host preparation intervals. They exclude runtime module JIT, uploads,
kernel launches, and steady-state execution and are not GPU timing claims.

## Driver and resident compatibility

This cache stores PTX packages. It does not persist native cubins or process-local
CUDA handles. Native preparation occurs when `cuda_module_load` runs on a checked
device context. Existing module and kernel handles can then be reused within
their owning device/context generation and resource-lifetime rules.

The target SM and actual driver PTX support are checked before launch. Driver
JIT failures return the existing `CudaError` boundary, status, and diagnostic
log. Driver-owned JIT caching is separate from this Traveler-owned package
cache. A driver update need not invalidate portable PTX input packages; it may
produce different native code, especially under `cuda-mma-native-v1`. A future
persisted native-artifact cache would need its own device/driver compatibility
identity and is not part of this PTX profile.

See [K5-D/K6 validation](../tests/gpu/shared-async-prepare-validation.md).
