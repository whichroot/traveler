# jacobian_oracle.py — exact-arithmetic oracle for examples/jacobian_counterexample.tv
#
# Subject: the Alpoge map F: C^3 -> C^3 (announced July 2026), the first
# counterexample to the Jacobian Conjecture:
#   F1 = (1+xy)^3 z + y^2 (1+xy)(4+3xy)
#   F2 = y + 3x(1+xy)^2 z + 3xy^2(4+3xy)
#   F3 = 2x - 3x^2 y - x^3 z
# det JF = -2 identically, yet (0,0,-1/4), (1,-3/2,13/2), (-1,3/2,13/2) all
# map to (-1/4, 0, 0).
#
# This oracle (pure Python, exact integer/Fraction arithmetic, no imports
# beyond stdlib) computes ground truth for the Traveler machine-check:
#   [1] det JF expanded symbolically over Z  (must be the constant -2);
#       the 9 partial derivatives emitted as monomial tables (Traveler data);
#       1-norm bound B for the CRT argument + admissible prime sets.
#   [2] elimination: z removed via F3, Res_y of the two cleared equations
#       -> eliminant R(x; w) over Z[w1,w2,w3]; generic degree d of F;
#       leading x-coefficient = Jelonek (non-properness) hypersurface candidate.
#   [3] fiber histogram of F over F_p^3 (p = 31) -> Chebotarev/monodromy
#       fixed-point statistics (cross-check for the Traveler p-loop).
#   [4] fiber-count streams N_n = #F^{-1}(a)(F_{3^n}), n = 1..6, for
#       a = (-1/4,0,0) (the triple point) and a generic rational target,
#       via log/exp tables in F_{3^n} -> the Berlekamp-Massey ground truth.
#   [5] irreducible moduli over F_3 of degrees 2..6 (for the Traveler F_{3^n}).
#
# Every printed line is meant to be diffed against the Traveler output.

from fractions import Fraction as Fr
from itertools import permutations
import sys

# ---------------------------------------------------------------- poly core
# multivariate sparse polys: dict {exponent-tuple: int/Fraction coeff}

def pmul(a, b):
    r = {}
    for ka, va in a.items():
        for kb, vb in b.items():
            k = tuple(i + j for i, j in zip(ka, kb))
            c = r.get(k, 0) + va * vb
            if c: r[k] = c
            elif k in r: del r[k]
    return r

def padd(*ps):
    r = {}
    for p in ps:
        for k, v in p.items():
            c = r.get(k, 0) + v
            if c: r[k] = c
            elif k in r: del r[k]
    return r

def scal(c, p):
    return {k: c * v for k, v in p.items()} if c else {}

def ppow(p, n):
    r = None
    for _ in range(n):
        r = p if r is None else pmul(r, p)
    return r if r is not None else {}

def deriv(p, i):
    r = {}
    for k, v in p.items():
        if k[i] > 0:
            k2 = list(k); k2[i] -= 1
            r[tuple(k2)] = v * k[i]
    return r

def ev(p, pt):
    s = 0
    for k, v in p.items():
        t = v
        for e, c in zip(k, pt): t *= c ** e
        s += t
    return s

# ------------------------------------------------------------- [1] det JF
NV = 3  # variables x,y,z
def V(i, n=NV):
    e = [0]*n; e[i] = 1; return {tuple(e): 1}
def C(c, n=NV):
    return {tuple([0]*n): c} if c else {}

X, Y, Z = V(0), V(1), V(2)
U   = padd(C(1), pmul(X, Y))                 # 1+xy
W43 = padd(C(4), scal(3, pmul(X, Y)))        # 4+3xy
F1 = padd(pmul(ppow(U, 3), Z), pmul(pmul(ppow(Y, 2), U), W43))
F2 = padd(Y, scal(3, pmul(pmul(X, ppow(U, 2)), Z)),
          scal(3, pmul(pmul(X, ppow(Y, 2)), W43)))
F3 = padd(scal(2, X), scal(-3, pmul(ppow(X, 2), Y)),
          scal(-1, pmul(ppow(X, 3), Z)))
Fs = [F1, F2, F3]

J = [[deriv(F, j) for j in range(3)] for F in Fs]
det = padd(
    pmul(J[0][0], padd(pmul(J[1][1], J[2][2]), scal(-1, pmul(J[1][2], J[2][1])))),
    scal(-1, pmul(J[0][1], padd(pmul(J[1][0], J[2][2]), scal(-1, pmul(J[1][2], J[2][0]))))),
    pmul(J[0][2], padd(pmul(J[1][0], J[2][1]), scal(-1, pmul(J[1][1], J[2][0])))))
print("[1] det JF (symbolic, Z[x,y,z]):", det)
assert det == {(0, 0, 0): -2}, "det JF is not the constant -2 ?!"

# triple point (exact rational)
pts = [(Fr(0), Fr(0), Fr(-1, 4)), (Fr(1), Fr(-3, 2), Fr(13, 2)),
       (Fr(-1), Fr(3, 2), Fr(13, 2))]
for pt in pts:
    im = tuple(ev(F, pt) for F in Fs)
    print("[1] point", tuple(map(str, pt)), "->", tuple(map(str, im)))
    assert im == (Fr(-1, 4), Fr(0), Fr(0))

# CRT bound: |coeff| of det (expanded WITHOUT cancellation) <= permanent of
# the 1-norm matrix; exhaustive check of det = -2 over F_p for p > maxdeg
# proves the identity in F_p[x,y,z]; product of primes > 2B proves it over Z.
norm = [[sum(abs(v) for v in J[i][j].values()) for j in range(3)] for i in range(3)]
B = sum(norm[0][s[0]] * norm[1][s[1]] * norm[2][s[2]] for s in permutations(range(3)))
maxdeg = 0
for i in range(3):
    for j in range(3):
        for k in J[i][j]:
            maxdeg = max(maxdeg, *k)
print("[1] 1-norm matrix:", norm)
print("[1] CRT bound B =", B, " max individual degree of J entries =", maxdeg)
primes_crt = [13, 31, 61, 101, 127]
prod = 1
for p in primes_crt: prod *= p
print("[1] primes", primes_crt, "product =", prod, " > 2B:", prod > 2 * B)
assert prod > 2 * B and min(primes_crt) > 3 * maxdeg  # det deg <= 3*maxdeg per var

# emit the 9 partials as a flat Traveler table:
#   per entry: nterms, then nterms x (ex, ey, ez, coeff)
flat = []
for i in range(3):
    for j in range(3):
        terms = sorted(J[i][j].items())
        flat.append(len(terms))
        for k, v in terms:
            flat.extend([k[0], k[1], k[2], v])
print("[1] JTAB (len {}):".format(len(flat)))
print("JTAB = [" + ",".join(map(str, flat)) + "]")

# ------------------------------------------------- [2] elimination over Q(w)
# variables now (x, y, w1, w2, w3); z eliminated: x^3 z = Zc := 2x-3x^2y-w3
N5 = 5
def V5(i): return V(i, N5)
def C5(c): return C(c, N5)
x5, y5, w1, w2, w3 = (V5(i) for i in range(5))
u5   = padd(C5(1), pmul(x5, y5))
w435 = padd(C5(4), scal(3, pmul(x5, y5)))
Zc = padd(scal(2, x5), scal(-3, pmul(ppow(x5, 2), y5)), scal(-1, w3))
x3 = ppow(x5, 3)
G1 = padd(pmul(ppow(u5, 3), Zc),
          pmul(x3, padd(pmul(pmul(ppow(y5, 2), u5), w435), scal(-1, w1))))
G2 = padd(scal(3, pmul(pmul(x5, ppow(u5, 2)), Zc)),
          pmul(x3, padd(y5, scal(3, pmul(pmul(x5, ppow(y5, 2)), w435)), scal(-1, w2))))

def ydeg(p): return max((k[1] for k in p), default=-1)
def ycoeffs(p, d):
    cs = [dict() for _ in range(d + 1)]
    for k, v in p.items():
        k2 = list(k); k2[1] = 0
        cs[k[1]][tuple(k2)] = v
    return cs

d1, d2 = ydeg(G1), ydeg(G2)
print("[2] deg_y G1 =", d1, " deg_y G2 =", d2)
c1, c2 = ycoeffs(G1, d1), ycoeffs(G2, d2)
n = d1 + d2
# Sylvester matrix (n x n), rows: d2 shifts of G1, d1 shifts of G2
S = [[{} for _ in range(n)] for _ in range(n)]
for s in range(d2):
    for k in range(d1 + 1): S[s][s + (d1 - k)] = c1[k]
for s in range(d1):
    for k in range(d2 + 1): S[d2 + s][s + (d2 - k)] = c2[k]
# determinant by Laplace with bitmask memo over columns
from functools import lru_cache
import math
sys.setrecursionlimit(10000)
memo = {}
def detS(row, mask):
    if row == n: return C5(1)
    key = mask
    if key in memo: return memo[key]
    acc = {}
    sgn = 1
    for col in range(n):
        if mask & (1 << col): continue
        e = S[row][col]
        if e:
            sub = detS(row + 1, mask | (1 << col))
            if sub: acc = padd(acc, scal(sgn, pmul(e, sub)))
        sgn = -sgn
    memo[key] = acc
    return acc
R = detS(0, 0)
xdeg = max((k[0] for k in R), default=-1)
xlow = min((k[0] for k in R), default=0)
print("[2] eliminant R(x;w): deg_x =", xdeg, " x-content = x^%d" % xlow,
      " #terms =", len(R))
# strip x-content
Rr = {}
for k, v in R.items():
    k2 = list(k); k2[0] -= xlow
    Rr[tuple(k2)] = v
xdeg_r = xdeg - xlow
# integer content
g = 0
for v in Rr.values(): g = math.gcd(g, abs(v))
Rr = {k: v // g for k, v in Rr.items()}
print("[2] after stripping: deg_x =", xdeg_r, " (= generic degree d of F, since")
print("    generic targets have no x=0 preimage: F(0,y,z)=(z+4y^2, y, 0))")
lead = {k: v for k, v in Rr.items() if k[0] == xdeg_r}
lead0 = {(k[2], k[3], k[4]): v for k, v in lead.items()}
print("[2] leading x-coefficient (Jelonek/non-properness candidate), in w:")
print("   ", lead0)
# fiber over the triple point, exact: specialize w=(-1/4,0,0), roots in x
spec = {}
for k, v in Rr.items():
    c = Fr(v) * Fr(-1, 4) ** k[2] * Fr(0) ** k[3] if k[3] or k[4] else Fr(v) * Fr(-1, 4) ** k[2]
    if k[3] == 0 and k[4] == 0:
        spec[k[0]] = spec.get(k[0], Fr(0)) + Fr(v) * Fr(-1, 4) ** k[2]
spec = {k: v for k, v in spec.items() if v}
print("[2] R(x; -1/4,0,0) coeffs {deg: coeff}:", {k: str(v) for k, v in sorted(spec.items())})
for r in (Fr(1), Fr(-1)):
    val = sum(v * r ** k for k, v in spec.items())
    print("    root check x =", str(r), "->", str(val))

# ------------------------------------------------- [3] fiber histogram, F_31
def fiber_hist(p):
    hist = {}
    imap = {}
    for x in range(p):
        for y in range(p):
            for z in range(p):
                u = (1 + x * y) % p
                w = (4 + 3 * x * y) % p
                f1 = (u ** 3 % p * z + y * y % p * u % p * w) % p
                f2 = (y + 3 * x * u * u % p * z + 3 * x % p * y * y % p * w) % p
                f3 = (2 * x - 3 * x * x % p * y - x ** 3 % p * z) % p
                key = (f1, f2, f3)
                imap[key] = imap.get(key, 0) + 1
    for v in imap.values(): hist[v] = hist.get(v, 0) + 1
    hist[0] = p ** 3 - len(imap)
    return hist

p = 31
h = fiber_hist(p)
tot = p ** 3
print("[3] fiber histogram over F_%d^3 (targets by #preimages):" % p)
for k in sorted(h): print("    %d preimages: %6d targets  (%.4f)" % (k, h[k], h[k] / tot))
chk = sum(k * h[k] for k in h)
print("    sum k*count =", chk, "= p^3:", chk == tot)
# S_3 fixed-point law: derangement-type frequencies 1/3, 1/2, 0, 1/6 for k=0,1,2,3
print("    S_3 Chebotarev prediction: k=0: 1/3=%.4f  k=1: 1/2=%.4f  k=3: 1/6=%.4f"
      % (1 / 3, 1 / 2, 1 / 6))

# --------------------------------------- [4] N_n over F_{3^n}, n=1..6  (+BM)
IRR3 = {1: [0, 1],                # x          (placeholder, F_3 itself)
        2: None, 3: None, 4: None, 5: None, 6: None}

def poly_is_irred(mod, q=3):
    # brute force: no roots in F_{q^k} for k <= deg/2 via subfield closure --
    # cheap here: test divisibility by all monic irreducibles of lower degree,
    # built recursively. For degrees <= 6 over F_3 just do trial division by
    # all monic polys of degree 1..deg//2.
    d = len(mod) - 1
    def polymodq(a, m):
        a = a[:]
        dm = len(m) - 1
        while len(a) - 1 >= dm and any(a):
            if a[-1] == 0: a.pop(); continue
            c = a[-1]; sh = len(a) - 1 - dm
            for i in range(dm + 1):
                a[sh + i] = (a[sh + i] - c * m[i]) % q
            while a and a[-1] == 0: a.pop()
        return a
    from itertools import product as iproduct
    for dd in range(1, d // 2 + 1):
        for tail in iproduct(range(q), repeat=dd):
            m = list(tail) + [1]
            if not polymodq(mod[:], m):
                return False
    return True

from itertools import product as iproduct
for d in range(2, 7):
    found = None
    for tail in iproduct(range(3), repeat=d):
        m = list(tail) + [1]
        if m[0] != 0 and poly_is_irred(m):
            found = m; break
    IRR3[d] = found
    print("[5] irreducible over F_3, degree %d: coeffs (low->high) = %s" % (d, found))

def build_field(pchar, mod):
    # F_{p^d} via log/exp tables; elements encoded as ints base p
    d = len(mod) - 1
    q = pchar ** d
    def mulraw(a, b):
        # a,b encoded ints -> polynomial mult mod (mod, p)
        da = [(a // pchar ** i) % pchar for i in range(d)]
        db = [(b // pchar ** i) % pchar for i in range(d)]
        prod = [0] * (2 * d - 1)
        for i in range(d):
            if da[i]:
                for j in range(d):
                    prod[i + j] = (prod[i + j] + da[i] * db[j]) % pchar
        for i in range(2 * d - 2, d - 1, -1):
            c = prod[i]
            if c:
                for j in range(d + 1):
                    prod[i - d + j] = (prod[i - d + j] - c * mod[j]) % pchar
        return sum(prod[i] * pchar ** i for i in range(d))
    # find generator
    def order(g):
        e, acc, k = 1, g, 1
        while acc != 1:
            acc = mulraw(acc, g); k += 1
            if k > q: return -1
        return k
    fact = []
    m = q - 1; f = 2
    while f * f <= m:
        while m % f == 0:
            fact.append(f); m //= f
        f += 1
    if m > 1: fact.append(m)
    fact = sorted(set(fact))
    g = None
    for cand in range(2, q):
        ok = True
        for f in fact:
            acc = 1
            e = (q - 1) // f
            b = cand
            ee = e
            while ee:
                if ee & 1: acc = mulraw(acc, b)
                b = mulraw(b, b); ee >>= 1
            if acc == 1: ok = False; break
        if ok: g = cand; break
    exp = [1] * (q - 1)
    log = {1: 0}
    acc = 1
    for i in range(1, q - 1):
        acc = mulraw(acc, g)
        exp[i] = acc; log[acc] = i
    def mul(a, b):
        if a == 0 or b == 0: return 0
        return exp[(log[a] + log[b]) % (q - 1)]
    def inv(a): return exp[(q - 1 - log[a]) % (q - 1)]
    def add(a, b):
        s = 0
        for i in range(d):
            s += ((a // pchar ** i + b // pchar ** i) % pchar) * pchar ** i
        return s
    def neg(a):
        s = 0
        for i in range(d):
            s += ((-(a // pchar ** i)) % pchar) * pchar ** i
        return s
    def emb(c):  # embed integer constant
        return c % pchar
    return q, add, neg, mul, inv, emb

def count_fiber_ext(n, target):
    # target given as integers mod 3 (w1,w2,w3)
    if n == 1:
        q, add, neg, mul, inv, emb = 3, lambda a, b: (a + b) % 3, lambda a: (-a) % 3, \
            lambda a, b: a * b % 3, lambda a: pow(a, 1, 3) if a == 1 else 2 if a == 2 else 0, lambda c: c % 3
        # inv in F_3: 1->1, 2->2
        inv = lambda a: a
    else:
        q, add, neg, mul, inv, emb = build_field(3, IRR3[n])
    W1, W2, W3 = emb(target[0]), emb(target[1]), emb(target[2])
    cnt = 0
    # x = 0 branch: F=(z+4y^2, y, 0)
    if W3 == 0:
        cnt += 1
    for x in range(1, q):
        x2 = mul(x, x); x3 = mul(x2, x)
        ix3 = inv(x3)
        for y in range(q):
            xy = mul(x, y)
            u = add(emb(1), xy)
            w = add(emb(4), mul(emb(3), xy))
            # z = (2x - 3x^2 y - w3)/x^3
            znum = add(add(mul(emb(2), x), neg(mul(emb(3), mul(x2, y)))), neg(W3))
            z = mul(znum, ix3)
            u2 = mul(u, u); u3 = mul(u2, u)
            y2 = mul(y, y)
            f1 = add(mul(u3, z), mul(mul(y2, u), w))
            if f1 != W1: continue
            f2 = add(add(y, mul(emb(3), mul(mul(x, u2), z))),
                     mul(emb(3), mul(mul(x, y2), w)))
            if f2 == W2: cnt += 1
    return cnt

targets = {"triple point (-1/4,0,0) mod 3 = (2,0,0)": (2, 0, 0),
           "generic (1,1,1)": (1, 1, 1)}
streams = {}
for name, t in targets.items():
    Ns = [count_fiber_ext(n, t) for n in range(1, 7)]
    streams[name] = Ns
    print("[4] N_n for", name, ":", Ns)

# Berlekamp-Massey over Q on the streams (ground truth for bm<F>)
def bm_q(s):
    C = [Fr(1)]; Bp = [Fr(1)]; L = 0; m = 1; b = Fr(1)
    for i, sn in enumerate(s):
        d = Fr(sn) + sum(C[j] * s[i - j] for j in range(1, L + 1))
        if d == 0: m += 1
        elif 2 * L <= i:
            T = C[:]
            coef = d / b
            C = C + [Fr(0)] * (len(Bp) + m - len(C))
            for j in range(len(Bp)):
                C[j + m] -= coef * Bp[j]
            L = i + 1 - L; Bp = T; b = d; m = 1
        else:
            coef = d / b
            C = C + [Fr(0)] * (len(Bp) + m - len(C))
            for j in range(len(Bp)):
                C[j + m] -= coef * Bp[j]
            m += 1
    return L, [str(c) for c in C[:L + 1]]
for name, Ns in streams.items():
    L, Cc = bm_q(Ns)
    print("[4] BM over Q:", name, " L =", L, " C =", Cc)
print("oracle done")
