# Bootstrap provenance — how Traveler builds itself without C

Traveler is self-hosted: the compiler (`src/tvc_self.tv`) is written in
Traveler. The only question a self-hosted language must answer is
*"where does the first binary come from?"* This directory is that answer.

## The trust root

`bootstrap/tvc_self.boot.ll` is LLVM IR **produced by Traveler compiling
Traveler** the Stage-2 output at the self-hosting fixed point (Stage 2 ==
Stage 3, byte-identical). It is not hand-written and contains no C. It is the
checked-in snapshot of the compiler, exactly as `rustc` checks in snapshot
binaries to build itself.

To build the compiler from it, with **no C source compiled**:

```sh
bootstrap/build.sh
```

The pipeline (clang/cc appears only as the system *linker* — it compiles no
source):

```
tvc_self.boot.ll  --llc-->  .o  --link-->  stage0     (the booted compiler)
stage0  compiles  src/tvc_self.tv  -->  stage1.ll  -->  stage1
stage1  compiles  src/tvc_self.tv  -->  stage2.ll
assert  stage1.ll == stage2.ll                         (fixed point, no C)
```

`bootstrap/out/stage1` is the canonical compiler; byte-for-byte the same one
the old C seed produced (the bootstrap gate proves this, see below).

## An Explanation

The IR is checked in; you do not need a working Traveler compiler to get
one. The snapshot is a *fixed point*: the compiler it boots, when run on 
the current source, reproduces the snapshot exactly (`build.sh --check`).

This is the same trust model as every self-hosted compiler. The chain of
custody traces back, snapshot by snapshot, to an original genesis that *was*
C-seeded, but no current build touches C.

## Keeping the snapshot fresh

After an intentional change to `src/tvc_self.tv` that alters emitted IR:

```sh
bootstrap/refresh.sh        # regenerates tvc_self.boot.ll from current source
git add bootstrap/tvc_self.boot.ll && git commit ...
```

`refresh.sh` is self-perpetuating: the new snapshot is produced by the
*previous* snapshot (Traveler compiling Traveler), never by C. It re-verifies
the new snapshot reaches its own fixed point before accepting it.

The CI bootstrap gate (`tests/run_bootstrap.sh`) fails if the committed
snapshot is stale.

## The C seed (optional provenance / audit path)

`src-legacy/tvc.c` is the original bootstrap seed. It is **no longer required** to
build Traveler. It remains in the tree as:

1. **An independent second source.** The bootstrap gate (assertion 4) builds
   the seed and proves the C-free path emits byte-identical IR to the C-seed
   path so the two derivations of the compiler agree. This is a defense
   against a corrupted snapshot: two independent toolchains, one answer.
2. **Historical genesis.** It is how the very first Traveler binary existed
   before any snapshot could.

`src-legacy/tvc.c` is frozen (Phase 6); it builds only Stage 1 of `tvc_self` and is
not extended. New language features live in `src/tvc_self.tv`.

## Files

| File | Role |
|---|---|
| `tvc_self.boot.ll` | the trust root: Traveler-produced compiler IR (committed) |
| `build.sh` | C-free build: boot IR → compiler, assert fixed point |
| `refresh.sh` | regenerate the snapshot from current source (self-perpetuating) |
| `out/` | build artifacts (gitignored) |
| `../../src-legacy/tvc.c` | optional C provenance/audit seed (frozen, not required) |
