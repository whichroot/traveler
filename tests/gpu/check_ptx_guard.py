"""Check the packager's integer abs rewrite against a PTX-level oracle."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cuda_package", ROOT / "tools/cuda_package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)
guard = package.guard_integer_abs


def refuses(ptx):
    try:
        guard(ptx)
    except ValueError:
        return True
    return False


source = (b"\tld.global.u64 \t%rd5, [%rd4];\n"
          b"\tabs.s64 \t%rd6, %rd5;\n"
          b"\tneg.s64 \t%rd7, %rd6;\n"
          b"\t@%p1 abs.s32 \t%r2, %r1;\n"
          b"\t@!%p2 abs.s16 %rs3, %rs1 ;\n"
          b"\tabs.f32 \t%f1, %f2;\n")
expected = (b"\tld.global.u64 \t%rd5, [%rd4];\n"
            b"\tneg.s64 \t%rd6, %rd5;\n"
            b"\tmax.s64 \t%rd6, %rd5, %rd6;\n"
            b"\tneg.s64 \t%rd7, %rd6;\n"
            b"\t@%p1 neg.s32 \t%r2, %r1;\n"
            b"\t@%p1 max.s32 \t%r2, %r1, %r2;\n"
            b"\t@!%p2 neg.s16 \t%rs3, %rs1;\n"
            b"\t@!%p2 max.s16 \t%rs3, %rs1, %rs3;\n"
            b"\tabs.f32 \t%f1, %f2;\n")
assert guard(source) == expected, guard(source)
assert guard(expected) == expected
assert guard(b"") == b""
assert refuses(b"\tabs.s64 \t%rd6, %rd6;\n")
assert refuses(b"\tabs.s64 \t%rd6, %rd5; add.s64 %rd1, %rd1, 1;\n")
assert refuses(b"\tabs.s32 \t%r1, 5;\n")
assert b"abs.s" not in guard(b"\n".join(b"\tabs.s%d \t%%v%d, %%w%d;" % (w, i, i)
                                         for w in (16, 32, 64) for i in range(40)))


# Value oracle: neg d, x; max d, x, d at each two's-complement width equals abs.sN.
def wrap(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


for bits in (16, 32, 64):
    edges = [0, 1, -1, 2, -2, (1 << (bits - 1)) - 1, -(1 << (bits - 1)), -(1 << (bits - 1)) + 1]
    for x in edges + list(range(-300, 300)):
        neg = wrap(-x, bits)
        assert max(x, neg) == wrap(abs(x), bits), (bits, x)
print("PTX guard PASS: rewrite, predicates, idempotence, refusals, and abs.s16/32/64 value oracle")
