# Historical language proposals

This document indexes unimplemented ideas removed from the language reference.
It is not a specification, implementation claim, approved roadmap, or commitment
to add a feature. The [language specification](language-spec.md) describes the
implemented surface. The pre-audit specification remains available in Git history
at `b1bfccb:spec/language-spec.md` for the original sketches.

## Types and syntax

- Runtime-degree `DynPoly<F>`, degree discovery returning an optional polynomial,
  and conversion into a statically sized polynomial.
- First-class `Piecewise`, `Segment`, `Regime`, and `Stream` types. The shipped
  codec and stream libraries already implement related operations using ordinary
  structs and buffers; they do not establish this proposed language syntax.
- Length-carrying slices, borrowed string slices, sub-slicing, tuple/unit values,
  and binding destructuring.
- General aliases for primitive/aggregate types. Field-constructor aliases are
  already implemented and are not part of this deferred item.
- Top-level `const` evaluation beyond the implemented global-initializer and
  type-constant subsets.
- Value-producing `if`/`match`, inclusive ranges, and direct field enumeration.
- Explicit polynomial-representation conversions, vector/range `eval`, polynomial
  methods/operators for multiplication and composition, and automatic algorithm
  selection among schoolbook, Karatsuba, and NTT.
- Namespaced modules, per-module privacy, item imports, and a package manager.

## Runtime and targets

- General bounds/OOM traps and match-exhaustiveness diagnostics.
- Async language streams and an FPGA/RTL backend.
- Static field moduli wider than 64 bits; runtime wide-field carriers are a
  separate implemented facility.
- First-class lossy piecewise approximation; existing library codecs have their
  own, separate numerical contracts.

## Superseded or conflicting sketches

The old document mixed proposed ownership/lifetime checking, RAII, and `unsafe`
blocks with a raw-pointer model that implements none of them. Those sketches
are not current promises. Likewise, `std::io`, `std::mem`, and a hypothetical
`PIECEWISE_CONST` wire block do not describe the shipped file-based library or
codec format.

Compound assignment, field aliases, generic aggregates, static traits, closures,
and explicit function pointers are implemented. Older lists marking them as
future work were stale and have not been carried forward here.
