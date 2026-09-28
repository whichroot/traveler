#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern void expert_dot(int32_t *,int64_t *,uint8_t *,uint8_t *,int64_t *,int64_t,int64_t,int64_t);
extern void expert_situ(int64_t *,int64_t *,int64_t *,int64_t,int64_t,int64_t);
extern int expert_full(uint8_t *,int64_t *,int64_t *,int64_t,int64_t,int64_t);
static uint64_t rng = 911;
static uint64_t next(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return rng; }
static int64_t rne(__int128 v, int bits) {
    if (!bits) return (int64_t)v;
    if (bits >= 128) return 0;
    __int128 q = v >> bits;
    __uint128_t mask = (((__uint128_t)1)<<bits)-1, rem = ((__uint128_t)v)&mask, half = ((__uint128_t)1)<<(bits-1);
    return (int64_t)(q + (rem > half || (rem == half && (q&1))));
}
static int weight(unsigned c) { const int w[8] = {0,1,2,3,4,6,8,12}; return c&8 ? -w[c&7] : w[c&7]; }
static int64_t neg(int64_t v) { return (int64_t)(0-(uint64_t)v); }
static int64_t mul(int64_t a,int64_t b) { return rne((__int128)a*b,32); }
static int64_t qratio(int64_t a,int64_t b) {
    __int128 rem = (__int128)a*((__int128)1<<32), den = b; uint64_t out = 0;
    for (int bit = 62; bit >= 0; --bit) if (rem >= (den<<bit)) { rem -= den<<bit; out |= UINT64_C(1)<<bit; }
    if (rem*2 > den || (rem*2 == den && (out&1))) ++out;
    return (int64_t)out;
}
static int64_t expneg(int64_t x) {
    if (x >= 0) return INT64_C(1)<<32;
    if (x <= -INT64_C(137438953472)) return 0;
    int64_t z = x*65536, k = -z/INT64_C(195103586505167), r = z+k*INT64_C(195103586505167);
    int64_t term = INT64_C(1)<<48, sum = term;
    for (int j = 1; j <= 14; ++j) { term = rne((__int128)term*r,48)/j; sum += term; }
    return rne(sum,16+k);
}
static int64_t qtanh(int64_t x) {
    int64_t a = x<0 ? neg(x) : x, e = expneg(neg((int64_t)(2*(uint64_t)a)));
    int64_t y = qratio((INT64_C(1)<<32)-e,(INT64_C(1)<<32)+e);
    return x<0 ? neg(y) : y;
}
static int64_t sigmoid(int64_t x) {
    int64_t a = x<0 ? neg(x) : x, e = expneg(neg(a));
    return qratio(x<0 ? e : INT64_C(1)<<32,(INT64_C(1)<<32)+e);
}
static int64_t situ(int64_t g,int64_t u,int64_t beta,int64_t lb) {
    int64_t a = qratio(g<0 ? neg(g):g,beta), b = qratio(u<0 ? neg(u):u,lb);
    if (g<0) a=neg(a);
    if (u<0) b=neg(b);
    return mul(mul(mul(beta,qtanh(a)),sigmoid(g)),mul(lb,qtanh(b)));
}
static void block(const int64_t *x,int32_t *mant,int64_t *exp,int groups) {
    for (int g = 0; g < groups; ++g) {
        uint64_t mx = 0;
        for (int j = 0; j < 32; ++j) { int64_t v=x[g*32+j]; uint64_t a=v<0 ? (uint64_t)neg(v):(uint64_t)v; if(a>mx) mx=a; }
        int bits=0; for(uint64_t v=mx;v;v>>=1) ++bits;
        int shift=bits-18;
        if(shift>0 && rne(mx,shift)==262144) ++shift;
        exp[g]=mx ? shift-32:0;
        for(int j=0;j<32;++j) mant[g*32+j]=shift>0 ? rne(x[g*32+j],shift):(int64_t)((uint64_t)x[g*32+j]<<(-shift));
    }
}
static void rows(const int32_t *x,const int64_t *ex,const uint8_t *w,int64_t *y,int m,int n,int k) {
    int groups=k/32; const uint8_t *scale=w+n*k/2;
    for(int t=0;t<m;++t) for(int row=0;row<n;++row) {
        int64_t emin=1000;
        for(int g=0;g<groups;++g) {int64_t e=ex[t*groups+g]+scale[row*groups+g]-128;if(e<emin)emin=e;}
        uint64_t acc=0;
        for(int g=0;g<groups;++g) {
            int64_t sum=0;
            for(int j=0;j<32;++j) sum+=(int64_t)x[t*k+g*32+j]*weight((w[row*k/2+g*16+j/2]>>(4*(j%2)))&15);
            int64_t shift=ex[t*groups+g]+scale[row*groups+g]-128-emin;
            if(shift<64)acc+=(uint64_t)sum<<shift;
        }
        y[t*n+row]=emin+32>=0 ? (emin+32>=64 ? 0:(int64_t)(acc<<(emin+32))):rne((int64_t)acc,-emin-32);
    }
}
int main(int argc, char **argv) {
    (void)argv;
    if (argc > 1 && (!__builtin_cpu_supports("avx512vnni") || !__builtin_cpu_supports("avx512ifma") ||
                    !__builtin_cpu_supports("avx512bw") || !__builtin_cpu_supports("avx512dq") ||
                    !__builtin_cpu_supports("avx512vl") || !__builtin_cpu_supports("avx512vbmi") ||
                    !__builtin_cpu_supports("avx512vbmi2") || !__builtin_cpu_supports("avx512bitalg") ||
                    !__builtin_cpu_supports("avx512vpopcntdq") || !__builtin_cpu_supports("bmi2"))) return 77;
    const int sizes[4] = {64,128,3072,3584};
    for (int sample = 0; sample < 32; ++sample) {
        int k = sizes[sample%4], groups = k/32, n = 5, m = sample&4 ? 4 : 1;
        int32_t *x = malloc(m*k*4); int64_t *e = malloc(m*groups*8), *out = malloc((m*n+2)*8);
        uint8_t *p = malloc(n*k/2), *s = malloc(n*groups);
        assert(x && e && out && p && s);
        for (int i = 0; i < m*k; ++i) x[i] = (int32_t)(next()%524289)-262144;
        x[0] = -262144; x[1] = 262144; x[2] = 0;
        for (int i = 0; i < m*groups; ++i) e[i] = (int64_t)(next()%16)-55;
        for (int i = 0; i < n*k/2; ++i) p[i] = next();
        for (int i = 0; i < n*groups; ++i) s[i] = sample&8 ? next()%255 : 110+next()%20;
        for (int i = 0; i < m*n+2; ++i) out[i] = 1234567;
        expert_dot(x,e,p,s,out+1,m,n,k);
        assert(out[0] == 1234567 && out[m*n+1] == 1234567);
        for (int t = 0; t < m; ++t) for (int row = 0; row < n; ++row) {
            if (row == 0 || row == n-1) { assert(out[1+t*n+row] == 1234567); continue; }
            int64_t emin = 1000;
            for (int g = 0; g < groups; ++g) { int64_t v = e[t*groups+g]+s[row*groups+g]-128; if(v<emin) emin=v; }
            uint64_t acc = 0;
            for (int g = 0; g < groups; ++g) {
                int32_t sum = 0;
                for (int j = 0; j < 32; ++j) { unsigned b = p[row*k/2+g*16+j/2]; sum += x[t*k+g*32+j]*weight((b>>(4*(j%2)))&15); }
                int64_t shift = e[t*groups+g]+s[row*groups+g]-128-emin;
                if (shift < 64) acc += ((uint64_t)(int64_t)sum)<<shift;
            }
            int64_t expected = emin+32 >= 0 ? (emin+32 >= 64 ? 0 : (int64_t)(acc<<(emin+32))) : rne((int64_t)acc,-emin-32);
            assert(out[1+t*n+row] == expected);
        }
        free(x); free(e); free(out); free(p); free(s);
    }
    puts("CPU expert dot oracle PASS: one/four tokens, real widths, wrapping, shifts, rows, guards");
    const int lengths[] = {0,1,7,8,9,15,16,17,257};
    const int64_t edges[] = {0,1,-1,INT64_MIN,INT64_MAX,INT64_MIN+1,INT64_C(1)<<32,-(INT64_C(1)<<32),
        INT64_C(137438953472),-INT64_C(137438953472),INT64_C(137438953471),-INT64_C(137438953471)};
    for (int sample = 0; sample < 54; ++sample) {
        int n = lengths[sample%9];
        int64_t g[259],u[259],out[259],expected[259];
        int64_t beta = INT64_C(4)<<32, lb = INT64_C(25)<<32;
        if (sample/9 == 1) { beta = INT64_C(1)<<32; lb = INT64_C(255)<<32; }
        if (sample/9 == 2) { beta = INT64_C(2)<<32; lb = INT64_C(3)<<32; }
        if (sample/9 == 3) { beta += 1; }
        if (sample/9 == 4) { beta = INT64_C(1)<<31; }
        if (sample/9 == 5) { beta = INT64_C(256)<<32; }
        for (int i = 0; i < 259; ++i) {
            g[i] = i < 12 ? edges[i] : (int64_t)(next()%((UINT64_C(1)<<40)+1))-(INT64_C(1)<<39);
            u[i] = i < 12 ? edges[11-i] : (int64_t)next(); out[i] = 567891;
            if(i>=12 && i%2==0) u[i]=(int64_t)(next()%(UINT64_C(1)<<40))-(INT64_C(1)<<39);
            expected[i] = situ(g[i],u[i],beta,lb);
        }
        expert_situ(g,u,out+1,n,beta,lb);
        assert(out[0] == 567891 && out[n+1] == 567891);
        for (int i = 0; i < n; ++i) {
            if (out[i+1] != expected[i]) { fprintf(stderr,"SiTU sample %d lane %d: %lld != %lld\n",sample,i,(long long)out[i+1],(long long)expected[i]); abort(); }
        }
        expert_situ(g,u,g,n,beta,lb);
        assert(!memcmp(g,expected,n*8));
    }
    puts("CPU SiTU oracle PASS: IFMA arithmetic, RNE, tails, exceptional lanes, fallback, aliases");
    assert(expert_full(NULL,NULL,NULL,1,65,128)==1);
    assert(expert_full(NULL,NULL,NULL,INT64_MAX,64,128)==1);
    for(int m=1;m<=4;m*=4) {
        const int d=64,f=128,part=d*f/2+d*f/32;
        uint8_t rec[3*part]; int64_t z[m*d],y[m*d],expected[m*d],g[m*f],u[m*f],a[m*f],ze[m*d/32],ae[m*f/32];
        int32_t zm[m*d],am[m*f];
        for(int matrix=0;matrix<3;++matrix) {
            for(int j=0;j<d*f/2;++j) rec[matrix*part+j]=next();
            for(int j=0;j<d*f/32;++j) rec[matrix*part+d*f/2+j]=115+next()%16;
        }
        for(int i=0;i<m*d;++i) z[i]=(int64_t)(next()%(UINT64_C(1)<<33))-(INT64_C(1)<<32);
        memset(z,0,32*sizeof *z); z[32]=(INT64_C(1)<<40)+3*(INT64_C(1)<<22);
        block(z,zm,ze,m*d/32); rows(zm,ze,rec,g,m,f,d); rows(zm,ze,rec+part,u,m,f,d);
        for(int i=0;i<m*f;++i)a[i]=situ(g[i],u[i],INT64_C(4)<<32,INT64_C(25)<<32);
        block(a,am,ae,m*f/32); rows(am,ae,rec+2*part,expected,m,d,f);
        int nonzero=0; for(int i=0;i<m*d;++i)nonzero|=expected[i]!=0; assert(nonzero);
        assert(expert_full(rec,z,y,m,d,f)==0); assert(!memcmp(y,expected,sizeof y));
    }
    puts("Complete routed expert PASS: mismatched_words 0, synthetic records, one/four tokens");
    return 0;
}
