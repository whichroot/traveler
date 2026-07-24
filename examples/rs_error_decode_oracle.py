#!/usr/bin/env python3
"""rs_error_decode_oracle.py — independent reference for rs_error_decode_test.tv.

Reproduces, in plain Python GF(2^8) bigint-free arithmetic, the ERROR-correction
decode pipeline built on the carrier-generic Berlekamp-Massey (bm<GF>, from
src/lib/nt/linrec.tv) and the existing evaluation-encoder (src/lib/ecc/
reed_solomon.tv). Pins the .tv's printed integer stream bit-for-bit, and — because
it re-derives the field math independently — confirms the design itself.

The math (GRS-dual syndromes over the evaluation encoder, no encoder change):
  encode:  code[j] = P(x_j),  x_j = j+1,  deg P < k        (existing rs_encode)
  dual:    u_i = 1 / prod_{j!=i} (x_i - x_j)               (char 2: - == +)
  synd:    S_m = sum_i u_i * recv_i * x_i^m,  m = 0..n-k-1  (vanish on codewords)
           => S_m = sum_l Y_l X_l^m,  X_l = x_at_error, Y_l = u * e   (a power sum)
  locate:  bm<GF>(S) -> nu, Lambda(x)=prod(1 - X_l x);  Chien: Lambda(x_i^-1)==0
  values:  Forney  E_l = X_l * Omega(X_l^-1) / Lambda'(X_l^-1),  e = E/u   (char2)
           AND erasure-reduction (located errors -> erasures -> Lagrange decode);
           the two independent value routes must agree (the cross-gate).
  refuse:  a valid codeword has ZERO syndromes (an eq0 certificate). If the
           corrected word's syndromes are nonzero, or Chien root-count != nu, or
           nu > t, REFUSE (print -1). For RS(12,5) (d=8 > 2t+1=7) a 4-error word
           is deterministically refused (no codeword within t=3 of it).
"""

# ---- GF(2^8) with the AES polynomial 0x11B (matches BinField<8,0x11B>) --------

def gf_mul(a, b):
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        b >>= 1
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
    return r

def gf_pow(a, e):
    r = 1
    b = a
    while e > 0:
        if e & 1:
            r = gf_mul(r, b)
        b = gf_mul(b, b)
        e >>= 1
    return r

def gf_inv(a):
    return gf_pow(a, 254)   # Fermat: a^(2^8 - 2)

# ---- encode = eval_at (mirror rs_encode, Horner) ------------------------------

def rs_encode(data, k, n):
    code = [0] * n
    for j in range(n):
        x = (j + 1) & 0xFF
        acc = 0
        for i in range(k - 1, -1, -1):
            acc = gf_mul(acc, x) ^ data[i]
        code[j] = acc
    return code

# ---- Lagrange interpolation decode from k survivors (mirror rs_decode) --------

def rs_decode(have_idx, have_val, k):
    out = [0] * k
    for m in range(k):
        xm = (have_idx[m] + 1) & 0xFF
        ym = have_val[m]
        scratch = [0] * k
        scratch[0] = 1
        deg = 0
        denom = 1
        for t in range(k):
            if t != m:
                xt = (have_idx[t] + 1) & 0xFF
                for i in range(deg + 1, 0, -1):
                    scratch[i] = scratch[i - 1] ^ gf_mul(scratch[i], xt)
                scratch[0] = gf_mul(scratch[0], xt)
                deg += 1
                denom = gf_mul(denom, xm ^ xt)
        scale = gf_mul(ym, gf_inv(denom))
        for i in range(k):
            out[i] ^= gf_mul(scratch[i], scale)
    return out

# ---- dual multipliers, syndromes ---------------------------------------------

def rs_dual_mult(i, n):
    xi = (i + 1) & 0xFF
    denom = 1
    for j in range(n):
        if j != i:
            xj = (j + 1) & 0xFF
            denom = gf_mul(denom, xi ^ xj)
    return gf_inv(denom)

def rs_syndromes(recv, n, k):
    nk = n - k
    syn = [0] * nk
    for m in range(nk):
        s = 0
        for i in range(n):
            xi = (i + 1) & 0xFF
            ui = rs_dual_mult(i, n)
            s ^= gf_mul(gf_mul(recv[i], ui), gf_pow(xi, m))
        syn[m] = s
    return syn

# ---- Berlekamp-Massey over GF(2^8) (mirror linrec bm<F>, division-free form) --

def bm_gf(s, n):
    C = [0] * (n + 1)
    B = [0] * (n + 1)
    C[0] = 1
    B[0] = 1
    lstar = 0
    m = 1
    b = 1
    for nn in range(n):
        d = s[nn]
        for j in range(1, lstar + 1):
            d ^= gf_mul(C[j], s[nn - j])
        if d == 0:
            m += 1
        else:
            coef = gf_mul(d, gf_inv(b))
            if 2 * lstar <= nn:
                T = C[:]
                k = 0
                while k + m <= n:
                    C[k + m] ^= gf_mul(coef, B[k])
                    k += 1
                lstar = nn + 1 - lstar
                B = T[:]
                b = d
                m = 1
            else:
                k = 0
                while k + m <= n:
                    C[k + m] ^= gf_mul(coef, B[k])
                    k += 1
                m += 1
    return lstar, C

# ---- locate = BM + Chien search ----------------------------------------------

def rs_locate(syn, n, k):
    nu, lam = bm_gf(syn, n - k)
    err_pos = []
    for i in range(n):
        xi = (i + 1) & 0xFF
        y = gf_inv(xi)
        val = 0
        xp = 1
        for mm in range(nu + 1):
            val ^= gf_mul(lam[mm], xp)
            xp = gf_mul(xp, y)
        if val == 0:
            err_pos.append(i)
    if len(err_pos) != nu:
        return -1, lam, []
    return nu, lam, err_pos

# ---- Forney error values ------------------------------------------------------

def rs_forney(syn, n, k, lam, nu, err_pos):
    nk = n - k
    # Omega = S * Lambda mod x^nk  (deg < nu)
    omega = [0] * nk
    for a in range(nk):
        acc = 0
        for b in range(a + 1):
            if a - b <= nu:
                acc ^= gf_mul(syn[b], lam[a - b])
        omega[a] = acc
    vals = []
    for pos in err_pos:
        xl = (pos + 1) & 0xFF
        y = gf_inv(xl)
        # Omega(y)
        ov = 0
        xp = 1
        for a in range(nu):
            ov ^= gf_mul(omega[a], xp)
            xp = gf_mul(xp, y)
        # Lambda'(y) = sum_{m odd} C[m] y^{m-1}
        lp = 0
        ypow = 1
        for mm in range(1, nu + 1):
            if mm % 2 == 1:
                lp ^= gf_mul(lam[mm], ypow)
            ypow = gf_mul(ypow, y)
        Yl = gf_mul(gf_mul(xl, ov), gf_inv(lp))   # char 2: -1 == 1
        e = gf_mul(Yl, gf_inv(rs_dual_mult(pos, n)))
        vals.append(e)
    return vals

# ---- full Forney decode (correct -> verify zero-syndrome -> interpolate) ------

def rs_decode_errors(recv, n, k):
    nk = n - k
    t = nk // 2
    syn = rs_syndromes(recv, n, k)
    nu, lam, err_pos = rs_locate(syn, n, k)
    if nu < 0 or nu > t:
        return -1, [], []
    corrected = recv[:]
    err_val = []
    if nu > 0:
        err_val = rs_forney(syn, n, k, lam, nu, err_pos)
        for l in range(nu):
            corrected[err_pos[l]] ^= err_val[l]
    # verify: a real codeword has all-zero syndromes
    if any(v != 0 for v in rs_syndromes(corrected, n, k)):
        return -1, [], []
    out = rs_decode(list(range(k)), corrected[:k], k)
    return nu, err_pos, err_val, out

# ---- erasure-reduction route (located errors treated as erasures) -------------

def rs_decode_erasure_at(recv, n, k, err_pos, nu):
    idx = []
    val = []
    for i in range(n):
        if i not in err_pos[:nu] and len(idx) < k:
            idx.append(i)
            val.append(recv[i])
    if len(idx) < k:
        return None
    return rs_decode(idx, val, k)

# ---- the driver: mirror rs_error_decode_test.tv's printed stream --------------

def run_case(data, k, n, errs, out):
    """errs: list of (pos, added_value). Prints the case's integer stream."""
    code = rs_encode(data, k, n)
    recv = code[:]
    for (pos, add) in errs:
        recv[pos] ^= add
    res = rs_decode_errors(recv, n, k)
    if res[0] < 0:
        out.append(-1)
        return
    nu, err_pos, err_val, dataF = res
    out.append(nu)
    for l in range(nu):
        out.append(err_val[l])
    dataE = rs_decode_erasure_at(recv, n, k, err_pos, nu)
    agree = 1 if dataE == dataF else 0
    ok = 1 if dataF == data else 0
    out.append(agree)
    out.append(ok)


def main():
    out = []
    # RS(12,5): t = 3. Cases: 0,1,2,3 correctable errors; 4 -> deterministic refuse.
    d125 = [0x12, 0x34, 0x56, 0x78, 0x9A]
    run_case(d125, 5, 12, [], out)
    run_case(d125, 5, 12, [(3, 0x1F)], out)
    run_case(d125, 5, 12, [(2, 0x1F), (7, 0x2A)], out)
    run_case(d125, 5, 12, [(1, 0x05), (5, 0x8C), (10, 0x33)], out)
    run_case(d125, 5, 12, [(0, 0x11), (4, 0x22), (8, 0x44), (11, 0x88)], out)  # refuse
    # RS(8,4): the DE AD BE EF continuity case, 2 errors (= t).
    d84 = [0xDE, 0xAD, 0xBE, 0xEF]
    run_case(d84, 4, 8, [(2, 0x1F), (5, 0x2A)], out)
    for v in out:
        print(v)


if __name__ == "__main__":
    main()
