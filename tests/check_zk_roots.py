"""Check static ZK domain roots against the field NTT chain and root order."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile


SOURCE = """
type F = Field<PRIME>;
#[zk]
fn chain(x: F) -> F {
    var acc: F = x;
    for i in 0..STEPS {
        acc = acc * x;
    }
    return acc;
}
fn main() -> i32 { return 0; }
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tvc", default="src/bootstrap/out/stage1")
    parser.add_argument("--opt", default="opt")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "roots.tv"
        output = Path(tmp) / "roots.ll"
        for prime, bits, steps, log_n in [
            (18446744069414584321, 64, 1, 3),
            (2013265921, 32, 1, 3),
            (65521, 16, 1, 3),
            (17, 8, 4, 4),
        ]:
            source.write_text(SOURCE.replace("PRIME", str(prime))
                              .replace("STEPS", str(steps)))
            subprocess.run([args.tvc, str(source), "-o", str(output)], check=True)
            subprocess.run([args.opt, "-passes=verify", "-disable-output",
                            str(output)], check=True)
            ir = output.read_text()
            body = ir.split("define i32 @chain_zk_prove(", 1)[1].split("\n}", 1)[0]
            assert f"@plonk_prove(i32 {1 << log_n}, i32 {log_n}," in body
            lookup = re.search(
                rf"(%\w+) = getelementptr \[\d+ x i{bits}\], "
                rf"ptr @ntt_roots_{prime}, i64 0, i64 {log_n - 1}\n", body)
            assert lookup, (prime, "missing field root lookup")
            load = re.search(rf"(%\w+) = load i{bits}, ptr {lookup[1]}\n", body)
            assert load, (prime, "missing root load")
            assert re.search(rf"call i{bits} @\w+mul\(i{bits} %\w+, "
                             rf"i{bits} {load[1]}\)", body)
            table = re.search(rf"@ntt_roots_{prime} = .*?\[\s*(i{bits} .*?)\]",
                              ir, re.S)
            assert table, (prime, "missing root table")
            roots = [int(v) for v in re.findall(rf"i{bits} (\d+)", table[1])]
            omega = roots[log_n - 1]
            assert pow(omega, 1 << log_n, prime) == 1
            assert pow(omega, 1 << (log_n - 1), prime) != 1
            print(f"PASS: Field<{prime}>, domain {1 << log_n}")


if __name__ == "__main__":
    main()
