"""Compare small static-field admission with independent trial division."""
from math import isqrt
from pathlib import Path
import subprocess
import sys
import tempfile

tvc = sys.argv[1]
with tempfile.TemporaryDirectory(prefix='field-primality-') as tmp:
    d = Path(tmp)
    for modulus in range(2, 130):
        source, artifact = d / 'field.tv', d / (str(modulus) + '.ll')
        source.write_text(f'type F = Field<{modulus}>;\nfn main() {{ let x: F = 1; print(x); }}\n')
        result = subprocess.run([tvc, str(source), '-o', str(artifact)],
                                capture_output=True, text=True, timeout=30)
        prime = all(modulus % divisor for divisor in range(2, isqrt(modulus) + 1))
        if prime:
            assert result.returncode == 0 and artifact.exists(), (modulus, result.stderr)
        else:
            assert result.returncode != 0 and 'is not prime' in result.stderr, (modulus, result)
            assert not artifact.exists(), modulus
    print('field primality PASS: exhaustive static admission for moduli 2..129')
