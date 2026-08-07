/*
 * tvc.c — Traveler Language Compiler
 *
 * Single-file compiler: .tv -> LLVM IR (.ll)
 * Pipeline: tvc input.tv -o output.ll
 *           llc -filetype=obj output.ll -o output.o
 *           clang output.o -o output
 *
 * Supports: Field<p> arithmetic, functions, let bindings,
 *           if/else, for loops, arrays, print, field builtins.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <ctype.h>
#include <stdarg.h>

/* ============================================================
 * Limits
 * ============================================================ */

/*
 * Hardcoded limits: necessary in the C bootstrap because C has no generics
 * or growable containers. The self-hosting compiler (tvc_self.tv) should use
 * Traveler's generic types and alloc-based growable buffers instead of
 * replicating these limits. The bootstrap's limits exist to bootstrap out
 * of them.
 */
#define MAX_IDENT    256
/* 131072 -> 262144 (2026-07-07, eval-engine E1): tvc_self.tv crossed 131072
 * tokens (~143k) when the evaluator landed; the static token array has no
 * overflow guard, so the old cap was silent memory corruption. A capacity
 * constant only — the sanctioned "guard needed to keep Stage 1 building"
 * class of seed edit. PROOF1 crossed 262144; the self compiler grows its pool. */
#define MAX_TOKENS   524288
#define MAX_AST      131072
#define MAX_SYMBOLS  4096
#define MAX_FIELDS   64
#define MAX_FUNCS    1024       /* bumped from 512 as tvc_self.tv grew (420 source fns); guarded by cap_overflow */
#define MAX_PARAMS   64
#define MAX_VARIANT_FIELDS 16   /* max payload fields per enum variant / match bindings */
#define MAX_IR       (1 << 24)  /* 16 MB IR buffer (bumped from 4 MB as tvc_self.tv grew; guarded by cap_overflow) */
#define MAX_GENERICS 4          /* max type params per generic function */
#define MAX_MONO     256        /* max monomorphized instantiations */
#define MAX_NTT_LOG  64         /* max 2-adic valuation of p-1 for NTT roots */
#define MAX_ZK_GATES  4096
#define MAX_ZK_COPIES 8192
/* 1024 -> 4096 + LOUD GUARD (2026-07-21, eval-engine E2i): tvc_self.tv crossed
 * 1024 TOP-LEVEL DECLS (980 -> 1025) and parse_program's decl array was an
 * unguarded malloc(1024) — silent heap overrun (the MAX_TOKENS incident class,
 * one arena over). Capacity constant + cap_overflow guard only. */
#define MAX_DECLS    4096

/* ============================================================
 * Token types
 * ============================================================ */

typedef enum {
    /* Literals */
    TOK_INT_LIT, TOK_BOOL_LIT, TOK_STR_LIT,
    /* Identifiers and keywords */
    TOK_IDENT,
    TOK_KW_FIELD, TOK_KW_FN, TOK_KW_LET, TOK_KW_MUT, TOK_KW_IF,
    TOK_KW_ELSE, TOK_KW_FOR, TOK_KW_IN, TOK_KW_WHILE, TOK_KW_RETURN,
    TOK_KW_TRUE, TOK_KW_FALSE, TOK_KW_AS, TOK_KW_CONST, TOK_KW_PUB,
    TOK_KW_STRUCT, TOK_KW_ENUM, TOK_KW_MATCH,
    TOK_KW_NULL, TOK_KW_UNSAFE, TOK_KW_EXTERN,
    TOK_KW_PRINT, TOK_KW_BREAK, TOK_KW_CONTINUE,
    TOK_KW_BINFIELD,
    TOK_KW_EXTFIELD,
    TOK_KW_INSTANTIATE,
    TOK_KW_VAR,      /* `var` == `let mut` (surface alias, plan-syntax-modernization) */
    TOK_KW_TYPE,     /* `type N = <FieldTy>;` == field/binfield/extfield decl */
    /* Type keywords */
    TOK_TY_U8, TOK_TY_U16, TOK_TY_U32, TOK_TY_U64,
    TOK_TY_I8, TOK_TY_I16, TOK_TY_I32, TOK_TY_I64,
    TOK_TY_USIZE, TOK_TY_BOOL, TOK_TY_FIELD, TOK_TY_BINFIELD, TOK_TY_EXTFIELD, TOK_TY_POLY,
    TOK_TY_REGISTER,
    /* Operators */
    TOK_PLUS, TOK_MINUS, TOK_STAR, TOK_SLASH, TOK_PERCENT,
    TOK_STARSTAR,   /* ** */
    TOK_EQ, TOK_NEQ, TOK_LT, TOK_GT, TOK_LE, TOK_GE,
    TOK_AND, TOK_OR, TOK_NOT,
    TOK_AMPERSAND,   /* &  — bitwise AND / address-of */
    TOK_PIPE,        /* |  — bitwise OR */
    TOK_CARET,       /* ^  — bitwise XOR */
    TOK_TILDE,       /* ~  — bitwise NOT */
    TOK_SHL,         /* << — shift left */
    TOK_SHR,         /* >> — shift right */
    TOK_ASSIGN,      /* = */
    TOK_OP_ASSIGN,   /* compound assign += -= *= /= %= &= |= ^= <<= >>=;
                      * the binary OpKind rides in int_val (parse-desugared to
                      * `x = x op e` — plan-syntax-modernization Phase 1) */
    /* Punctuation */
    TOK_LPAREN, TOK_RPAREN, TOK_LBRACE, TOK_RBRACE,
    TOK_LBRACKET, TOK_RBRACKET,
    TOK_SEMI, TOK_COLON, TOK_COMMA, TOK_DOT,
    TOK_ARROW,       /* -> */
    TOK_FAT_ARROW,   /* => */
    TOK_COLONCOLON,  /* :: */
    TOK_DOTDOT,      /* .. */
    TOK_HASH_LBRACKET, /* #[ */
    /* Special */
    TOK_EOF,
} TokenKind;

typedef struct {
    TokenKind kind;
    int       line, col;
    char      text[MAX_IDENT];
    uint64_t  int_val;
    bool      bool_val;
} Token;

/* Field kind constants */
#define FIELD_KIND_PRIME     0
#define FIELD_KIND_BINARY    1
#define FIELD_KIND_EXTENSION 2

/* ============================================================
 * AST types
 * ============================================================ */

typedef enum {
    /* Top-level */
    AST_PROGRAM, AST_FIELD_DECL, AST_FN_DECL, AST_EXTERN_FN, AST_STRUCT_DECL, AST_ENUM_DECL,
    /* Statements */
    AST_LET, AST_ASSIGN, AST_RETURN, AST_EXPR_STMT,
    AST_IF, AST_FOR, AST_BLOCK, AST_WHILE,
    AST_MEMBER_ASSIGN, AST_MATCH,
    /* Expressions */
    AST_BINARY, AST_UNARY, AST_CALL, AST_LIT_INT, AST_LIT_BOOL,
    AST_IDENT, AST_FIELD_ACCESS, AST_CAST, AST_INDEX, AST_ARRAY,
    AST_PROJECT, AST_BREAK, AST_CONTINUE, AST_INDEX_ASSIGN,
    AST_STRUCT_LIT, AST_MEMBER_ACCESS, AST_ENUM_CONSTRUCT,
    AST_NULL, AST_STR_LIT,
    AST_INSTANTIATE,
} ASTKind;

typedef enum {
    OP_ADD, OP_SUB, OP_MUL, OP_DIV, OP_MOD, OP_POW,
    OP_EQ, OP_NEQ, OP_LT, OP_GT, OP_LE, OP_GE,
    OP_AND, OP_OR, OP_NEG, OP_NOT,
    OP_BITAND, OP_BITOR, OP_BITXOR, OP_BITNOT,
    OP_SHL, OP_SHR,
    OP_ADDR,    /* &var — address-of */
} OpKind;

/* Forward declare */
typedef struct ASTNode ASTNode;

struct ASTNode {
    ASTKind    kind;
    int        line, col;

    union {
        /* AST_PROGRAM */
        struct { ASTNode **decls; int ndecls; } program;

        /* AST_FIELD_DECL: field F = Field<251>; binfield G = BinField<8, 0x11B>;
         * extfield E = ExtField<Field<p>, 2>; */
        struct { char name[MAX_IDENT]; uint64_t prime; uint64_t poly; int degree; int field_kind; } field_decl;

        /* AST_FN_DECL */
        struct {
            char     name[MAX_IDENT];
            char     params[MAX_PARAMS][MAX_IDENT];
            char     param_types[MAX_PARAMS][MAX_IDENT];
            int      nparams;
            char     ret_type[MAX_IDENT];
            ASTNode *body;
            char     attrs[MAX_IDENT];  /* #[allow(...)] */
            /* Generic type parameters: fn name<F: Field>(...) */
            int      ngen;
            char     gen_names[MAX_GENERICS][MAX_IDENT];
            char     gen_bounds[MAX_GENERICS][MAX_IDENT];
        } fn_decl;

        /* AST_LET: let [mut] name: type = expr; */
        struct {
            char     name[MAX_IDENT];
            char     type_name[MAX_IDENT];
            ASTNode *init;
            bool     is_mut;
            bool     is_global;
        } let_stmt;

        /* AST_ASSIGN */
        struct { char name[MAX_IDENT]; ASTNode *value; } assign;

        /* AST_RETURN */
        struct { ASTNode *value; } ret;

        /* AST_EXPR_STMT */
        struct { ASTNode *expr; } expr_stmt;

        /* AST_IF */
        struct { ASTNode *cond; ASTNode *then_b; ASTNode *else_b; } if_expr;

        /* AST_FOR: for i in start..end { body } */
        struct {
            char name[MAX_IDENT];
            ASTNode *start; ASTNode *end; ASTNode *body;
        } for_stmt;

        /* AST_WHILE */
        struct { ASTNode *cond; ASTNode *body; uint64_t fuel; } while_stmt;

        /* AST_BLOCK */
        struct { ASTNode **stmts; int nstmts; } block;

        /* AST_BINARY */
        struct { OpKind op; ASTNode *lhs; ASTNode *rhs; } binary;

        /* AST_UNARY */
        struct { OpKind op; ASTNode *operand; } unary;

        /* AST_CALL */
        struct { char name[MAX_IDENT]; ASTNode **args; int nargs; } call;

        /* AST_LIT_INT */
        struct { uint64_t value; } lit_int;

        /* AST_LIT_BOOL */
        struct { bool value; } lit_bool;

        /* AST_IDENT */
        struct { char name[MAX_IDENT]; } ident;

        /* AST_FIELD_ACCESS: F::ZERO */
        struct { char obj[MAX_IDENT]; char member[MAX_IDENT]; } field_access;

        /* AST_CAST: expr as type */
        struct { ASTNode *expr; char type_name[MAX_IDENT]; } cast;

        /* AST_PROJECT: project(expr) */
        struct { ASTNode *expr; } project;

        /* AST_INDEX: arr[idx] or expr[idx] (when obj_expr != NULL) */
        struct { char name[MAX_IDENT]; ASTNode *index; ASTNode *obj_expr; int arr_alloca_reg; } index_expr;

        /* AST_STRUCT_DECL: struct Name { field: Type, ... } */
        struct {
            char name[MAX_IDENT];
            char field_names[MAX_PARAMS][MAX_IDENT];
            char field_types[MAX_PARAMS][MAX_IDENT];
            int  nfields;
        } struct_decl;

        /* AST_STRUCT_LIT: Name { field: expr, ... } */
        struct {
            char     type_name[MAX_IDENT];
            char     field_names[MAX_PARAMS][MAX_IDENT];
            ASTNode *field_values[MAX_PARAMS];
            int      nfields;
        } struct_lit;

        /* AST_MEMBER_ACCESS: obj.member (or chained: expr.member) */
        struct { char obj_name[MAX_IDENT]; char member[MAX_IDENT]; ASTNode *obj_expr; } member_access;

        /* AST_MEMBER_ASSIGN: obj.member = expr (or chained: expr.member = val) */
        struct { char obj_name[MAX_IDENT]; char member[MAX_IDENT]; ASTNode *value; ASTNode *obj_expr; } member_assign;

        /* AST_ENUM_DECL: lightweight — full data in g_enums registry */
        struct { char name[MAX_IDENT]; } enum_decl;

        /* AST_ENUM_CONSTRUCT: Enum::Variant(args...) */
        struct {
            char     enum_name[MAX_IDENT];
            char     variant_name[MAX_IDENT];
            ASTNode *args[MAX_PARAMS];
            int      nargs;
        } enum_construct;

        /* AST_MATCH: lightweight — arm data in g_match_arms registry */
        struct {
            ASTNode *scrutinee;
            int      arms_start;  /* index into g_match_arms */
            int      narms;
        } match_expr;
    };
};

/* ============================================================
 * Type system
 * ============================================================ */

typedef enum {
    TYPE_VOID, TYPE_BOOL,
    TYPE_U8, TYPE_U16, TYPE_U32, TYPE_U64,
    TYPE_I8, TYPE_I16, TYPE_I32, TYPE_I64,
    TYPE_USIZE,
    TYPE_FIELD,
    TYPE_DYNFIELD,      /* element of a runtime-determined prime field (i64-backed) */
    TYPE_FIELD_VALUE,   /* the %__Field carrier struct (the runtime Field object) */
    TYPE_ARRAY,
    TYPE_POLY,
    TYPE_REGISTER,
    TYPE_STRUCT,
    TYPE_ENUM,
    TYPE_PTR,
} TypeKind;

/* Repr tags for Poly<F, d> values */
#define REPR_UNKNOWN  0
#define REPR_NEWTON   1
#define REPR_STANDARD 2

typedef struct {
    TypeKind kind;
    uint64_t field_prime;   /* for TYPE_FIELD, TYPE_POLY, and TYPE_REGISTER */
    int      array_size;    /* for TYPE_ARRAY */
    int      poly_degree;   /* for TYPE_POLY */
    int      register_degree; /* for TYPE_REGISTER */
    TypeKind elem_kind;     /* for TYPE_ARRAY */
    int      struct_id;     /* for TYPE_STRUCT: index into g_structs */
    int      enum_id;       /* for TYPE_ENUM: index into g_enums */
    int      field_idx;      /* index into g_fields (-1 if not a field type) */
    /* For TYPE_PTR: store what it points to */
    TypeKind ptr_pointee_kind; /* pointee's TypeKind */
    int      ptr_struct_id;    /* pointee's struct_id (if struct) */
    int      ptr_enum_id;      /* pointee's enum_id (if enum) */
    uint64_t ptr_field_prime;  /* pointee's field_prime (if field) */
    int      ptr_field_idx;    /* pointee's field_idx (if field) */
} Type;

/* Field registry */
typedef struct {
    char     alias[MAX_IDENT];   /* e.g. "F" or "G" */
    uint64_t prime;              /* e.g. 251 (for prime fields); base prime (for extension) */
    uint64_t poly;               /* e.g. 0x11B (for binary fields) */
    int      degree;             /* e.g. 8 for GF(2^8) */
    int      field_kind;         /* FIELD_KIND_PRIME, FIELD_KIND_BINARY, FIELD_KIND_EXTENSION */
    char     elem_ir[16];        /* "i8", "i16", "i32" (base element for extension) */
    char     wide_ir[16];        /* "i16", "i32", "i64" */
    int      elem_bits;          /* 8, 16, 32 (base element bits) */
    /* Extension field specific */
    int      base_field_idx;     /* index into g_fields for base field (-1 if not extension) */
    uint64_t nonresidue;         /* quadratic non-residue nr: i^2 = nr in GF(p^2) */
    char     ext_elem_ir[32];    /* "{i64, i64}" — LLVM IR type for extension element */
    /* NTT root chain (computed at field registration for prime fields) */
    int      ntt_max_log;        /* largest k where 2^k | (p-1); 0 if not useful for NTT */
    uint64_t ntt_generator;      /* generator of Z/pZ* (0 if not computed) */
    uint64_t ntt_roots[MAX_NTT_LOG];     /* roots[k] = g^((p-1)/2^(k+1)) mod p, primitive 2^(k+1)-th root */
    uint64_t ntt_inv_roots[MAX_NTT_LOG]; /* inv_roots[k] = roots[k]^(p-2) mod p */
    uint64_t ntt_n_inv[MAX_NTT_LOG];     /* n_inv[k] = (2^(k+1))^(p-2) mod p */
} FieldInfo;

/* Symbol table */
typedef struct {
    char name[MAX_IDENT];
    Type type;
    int  ir_reg;        /* LLVM SSA register number */
    bool is_param;
    bool is_alloca;     /* stored via alloca */
    int  repr_tag;      /* REPR_NEWTON, REPR_STANDARD, or REPR_UNKNOWN (for TYPE_POLY) */
} Symbol;

typedef struct {
    Symbol entries[MAX_SYMBOLS];
    int    count;
    int    scope_start[64];
    int    scope_depth;
} SymbolTable;

/* ============================================================
 * Global compiler state
 * ============================================================ */

static const char *g_source    = NULL;
static const char *g_filename  = NULL;
static Token       g_tokens[MAX_TOKENS];
static int         g_ntokens   = 0;
static int         g_tok_pos   = 0;

static FieldInfo   g_fields[MAX_FIELDS];
static int         g_nfields   = 0;

/* Struct registry */
typedef struct {
    char name[MAX_IDENT];       /* field name */
    char type_name[MAX_IDENT];  /* type string for resolve_type */
    Type type;                  /* resolved type */
} StructField;

typedef struct {
    char        name[MAX_IDENT];             /* struct name: "Point" */
    StructField fields[MAX_PARAMS];          /* up to MAX_PARAMS fields */
    int         nfields;
    char        ir_name[MAX_IDENT];          /* LLVM type name: "%Point" */
    int         byte_size;                   /* total size in bytes */
} StructInfo;

static StructInfo  g_structs[MAX_FIELDS];    /* reuse MAX_FIELDS (64) */
static int         g_nstructs = 0;

static StructInfo *find_struct(const char *name) {
    for (int i = 0; i < g_nstructs; i++) {
        if (!strcmp(g_structs[i].name, name)) return &g_structs[i];
    }
    return NULL;
}

static int struct_field_index(StructInfo *si, const char *field_name) {
    for (int i = 0; i < si->nfields; i++) {
        if (!strcmp(si->fields[i].name, field_name)) return i;
    }
    return -1;
}

/* Enum registry */
typedef struct {
    char name[MAX_IDENT];                     /* variant name */
    char field_types[MAX_VARIANT_FIELDS][MAX_IDENT]; /* payload type strings */
    Type fields[MAX_VARIANT_FIELDS];               /* resolved payload types */
    int  nfields;                             /* 0 = unit variant */
    int  tag;                                 /* discriminant: 0, 1, 2, ... */
    int  payload_size;                        /* bytes for this variant's payload */
} EnumVariant;

typedef struct {
    char         name[MAX_IDENT];             /* enum name */
    EnumVariant  variants[64];                /* up to 64 variants */
    int          nvariants;
    char         ir_name[MAX_IDENT];          /* "%Expr" */
    int          total_size;                  /* 8 + max payload, rounded to 8 */
} EnumInfo;

static EnumInfo g_enums[MAX_FIELDS];
static int      g_nenums = 0;

static EnumInfo *find_enum(const char *name) {
    for (int i = 0; i < g_nenums; i++) {
        if (!strcmp(g_enums[i].name, name)) return &g_enums[i];
    }
    return NULL;
}

static int enum_variant_index(EnumInfo *ei, const char *variant_name) {
    for (int i = 0; i < ei->nvariants; i++) {
        if (!strcmp(ei->variants[i].name, variant_name)) return i;
    }
    return -1;
}

/* Match arm registry (Option B: separate from AST) */
typedef struct {
    char     enum_name[MAX_IDENT];
    char     variant_name[MAX_IDENT];
    char     bindings[MAX_VARIANT_FIELDS][MAX_IDENT]; /* destructured binding names */
    int      nbindings;
    ASTNode *body;
    bool     is_wildcard;                     /* true if arm is `_` */
    bool     is_int_lit;                      /* true if arm is an integer literal */
    int64_t  int_val;                         /* value for integer literal arm */
} MatchArmInfo;

#define MAX_MATCH_ARMS 1024
static MatchArmInfo g_match_arms[MAX_MATCH_ARMS];
static int          g_nmatch_arms = 0;

/* IR output buffer */
static char        g_ir[MAX_IR];
static int         g_ir_len    = 0;
static int         g_tmp       = 0;   /* SSA counter */
static int         g_label     = 0;   /* label counter */

/* Alloca prelude buffer — collects allocas during body codegen
 * so they can be emitted in the entry block (required for LLVM mem2reg).
 * Without this, allocas inside loop bodies cause per-iteration stack growth. */
#define MAX_PRELUDE (MAX_IR / 4)
static char        g_prelude[MAX_PRELUDE];
static int         g_prelude_len = 0;
static bool        g_use_prelude = false;  /* when true, ir_emit_alloca goes to prelude */

static SymbolTable g_syms;
static bool        g_block_terminated = false;  /* set after ret/unreachable */

/* Loop label stack for break/continue */
static int         g_loop_end_labels[64];    /* break targets */
static int         g_loop_cond_labels[64];   /* continue targets */
static int         g_loop_depth = 0;

/* ============================================================
 * Parallel dispatch infrastructure
 *
 * The compiler detects algebraically independent loop iterations
 * and emits parallel dispatch via pthreads.  The independence
 * proof comes from the type system: Field element operations
 * have no carry between elements (field axioms), and array
 * accesses indexed only by the loop variable touch disjoint
 * memory.  The compiler proves WHAT is safe at compile time;
 * the runtime decides HOW MANY threads at execution time.
 * ============================================================ */

#define MAX_PFOR      64
#define MAX_PFOR_CAP  16   /* max captured variables per worker */
#define PFOR_THRESHOLD 1024 /* min trip count for parallel dispatch */

typedef struct {
    char  name[MAX_IDENT];
    Type  type;
    int   alloca_reg;   /* register of the alloca (or value) in the CALLING function */
    bool  is_alloca;    /* true = load from alloca; false = register IS the value */
} PForCapture;

typedef struct {
    int          id;                        /* unique worker id */
    ASTNode     *body;                      /* loop body AST (block node) */
    char         loop_var[MAX_IDENT];       /* loop variable name */
    Type         iter_ty;                   /* loop var type (i32) */
    char         fn_ret_type[MAX_IDENT];    /* enclosing fn return type (for codegen_stmt) */
    Type         fn_ret;                    /* resolved return type */
    PForCapture  caps[MAX_PFOR_CAP];        /* captured variables */
    int          ncaps;
} PForWorker;

static PForWorker g_pfor_workers[MAX_PFOR];
static int        g_npfor_workers = 0;
static int        g_pfor_id       = 0;
static bool       g_pfor_emitted  = false; /* has runtime been emitted? */

/* Target triple — configurable via -target flag.
 * Affects: IR target triple directive, sysconf constant for core detection.
 * Default: arm64-apple-darwin (macOS ARM). */
static char g_target_triple[128] = "arm64-apple-darwin";
static int  g_sysconf_nproc = 58;  /* _SC_NPROCESSORS_ONLN: macOS=58, Linux=84 */

/* Function registry — tracks declared functions and their types */
typedef struct {
    char name[MAX_IDENT];
    char ret_type[MAX_IDENT];
    char param_types[MAX_PARAMS][MAX_IDENT];
    int  nparams;
    bool is_extern;     /* extern "C" — declaration only, no body */
    /* Generic function support */
    bool     is_generic;
    int      ngen;
    char     gen_names[MAX_GENERICS][MAX_IDENT];
    char     gen_bounds[MAX_GENERICS][MAX_IDENT];
    ASTNode *ast;       /* pointer to AST node (for monomorphization re-codegen) */
    /* Purity analysis: a pure function has no side effects (no I/O,
     * no heap mutation, no global writes, only calls other pure functions).
     * Used by auto-parallelization to allow function calls in loop bodies. */
    bool is_pure;
    bool purity_computed;
    /* Array-write analysis: true if the body (transitively) contains any
     * indexed store.  In a pure function (no alloc), every local pointer
     * derives from a parameter or global, so an indexed store may mutate
     * CALLER memory — e.g. fn bump(acc: *F, v: F) { acc[0] = acc[0] + v; }
     * is "pure" by the side-effect rules above but is NOT safe to call
     * from a parallel loop (hidden single-cell reduction race).  Parallel
     * dispatch requires is_pure && !writes_arrays. */
    bool writes_arrays;
} FuncInfo;

static FuncInfo    g_funcs[MAX_FUNCS];
static int         g_nfuncs = 0;

static FuncInfo *find_func(const char *name) {
    for (int i = 0; i < g_nfuncs; i++) {
        if (!strcmp(g_funcs[i].name, name)) return &g_funcs[i];
    }
    return NULL;
}

/* Monomorphization cache — tracks which (generic_fn, concrete_types) are instantiated */
typedef struct {
    char base_name[MAX_IDENT];                      /* original generic function name */
    char concrete_types[MAX_GENERICS][MAX_IDENT];   /* e.g. "Field<251>" */
    int  ngen;
    char mangled_name[MAX_IDENT];                   /* e.g. "generic_add_Field251" */
    bool is_explicit;                               /* true if from 'instantiate' decl */
} MonoEntry;

static MonoEntry   g_mono[MAX_MONO];
static int         g_nmono = 0;

/* Active generic substitution context (set during monomorphized codegen) */
static char        g_gen_sub_name[MAX_GENERICS][MAX_IDENT];  /* generic param names */
static char        g_gen_sub_type[MAX_GENERICS][MAX_IDENT];  /* concrete type strings */
static int         g_gen_nsub = 0;
static char        g_gen_mangled[MAX_IDENT];                 /* mangled fn name */
static bool        g_mono_explicit = false;                  /* true during explicit instantiation codegen */

/* Look up an already-monomorphized instantiation */
static MonoEntry *find_mono(const char *base_name, const char concrete[][MAX_IDENT], int ngen) {
    for (int i = 0; i < g_nmono; i++) {
        if (strcmp(g_mono[i].base_name, base_name) != 0) continue;
        if (g_mono[i].ngen != ngen) continue;
        bool match = true;
        for (int j = 0; j < ngen; j++) {
            if (strcmp(g_mono[i].concrete_types[j], concrete[j]) != 0) {
                match = false; break;
            }
        }
        if (match) return &g_mono[i];
    }
    return NULL;
}

/* Build a mangled function name: "generic_add_Field251" */
static void mangle_name(char *out, const char *base, const char concrete[][MAX_IDENT], int ngen) {
    strcpy(out, base);
    for (int i = 0; i < ngen; i++) {
        strcat(out, "_");
        /* Sanitize: Field<251> -> Field251, BinField<8, 0x11B> -> BinField8_0x11B */
        for (const char *p = concrete[i]; *p; p++) {
            char c = *p;
            if (c == '<' || c == '>' || c == ' ') continue;
            if (c == ',') c = '_';
            int len = strlen(out);
            out[len] = c; out[len + 1] = 0;
        }
    }
}

static bool        g_has_errors = false;

/* Repr tag communicated from codegen_expr (poly/analyze/+) to AST_LET handler */
static int         g_last_repr = REPR_UNKNOWN;

/* String constant registry — collected during codegen, emitted at end.
 * One entry per string-literal OCCURRENCE (not deduplicated). tvc_self.tv
 * grew past 4096 occurrences; this seed only needs to keep building Stage 1,
 * so the cap is a capacity constant, not a feature (consistent with the
 * Phase 6 freeze). Headroom to 16384 leaves ample room for further growth. */
#define MAX_STRINGS 16384
static struct {
    char data[MAX_IDENT];  /* raw string content (may contain \0) */
    int  len;              /* actual length (excluding null terminator) */
    int  id;               /* @.str.<id> */
} g_strings[MAX_STRINGS];
static int g_nstrings = 0;

/* ============================================================
 * Utility
 * ============================================================ */

static void error(int line, int col, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "error: %s:%d:%d: ", g_filename, line, col);
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
    g_has_errors = true;
}

static void warn(int line, int col, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "warning: %s:%d:%d: ", g_filename, line, col);
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
}

/* Fatal capacity-overflow abort. The seed uses FIXED static arrays; once the
 * self-hosting source (tvc_self.tv) grows past one, writing further silently
 * corrupts adjacent memory (the MAX_TOKENS incident — see the MAX_TOKENS note
 * near the top). Guarding every compiler-size-scaling write with this makes an
 * overflow LOUD, not silent — the sanctioned "guard needed to keep Stage 1
 * building" class of frozen-seed edit. */
static void cap_overflow(const char *what, int cap) {
    fprintf(stderr, "FATAL: %s capacity exceeded (limit %d). "
                    "Bump the corresponding MAX_* in src-legacy/tvc.c.\n",
            what, cap);
    exit(1);
}

static void ir_emit(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    /* vsnprintf returns the INTENDED length; without this guard g_ir_len would
     * overshoot MAX_IR on truncation, and the next call would compute a
     * negative (-> huge) size bound with an out-of-bounds base. */
    int n = vsnprintf(g_ir + g_ir_len, MAX_IR - g_ir_len, fmt, ap);
    va_end(ap);
    if (n < 0 || g_ir_len + n >= MAX_IR) cap_overflow("IR output buffer (g_ir)", MAX_IR);
    g_ir_len += n;
}

/* Emit an alloca instruction. When g_use_prelude is true (during body codegen),
 * writes to the prelude buffer for later insertion into the entry block.
 * When false (during param spills or helper function emission), writes inline. */
static void ir_emit_alloca(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    if (g_use_prelude) {
        int n = vsnprintf(g_prelude + g_prelude_len,
                          MAX_PRELUDE - g_prelude_len, fmt, ap);
        if (n < 0 || g_prelude_len + n >= MAX_PRELUDE)
            cap_overflow("alloca prelude buffer (g_prelude)", MAX_PRELUDE);
        g_prelude_len += n;
    } else {
        int n = vsnprintf(g_ir + g_ir_len, MAX_IR - g_ir_len, fmt, ap);
        if (n < 0 || g_ir_len + n >= MAX_IR)
            cap_overflow("IR output buffer (g_ir)", MAX_IR);
        g_ir_len += n;
    }
    va_end(ap);
}

static int ir_tmp(void) { return g_tmp++; }
static int ir_label(void) { return g_label++; }

/* Use named temporaries (%t0, %t1, ...) to avoid LLVM's
 * sequential numbering requirement for unnamed values. */
#define T "t"  /* prefix for temporaries */
#define L "L"  /* prefix for labels */

/* Modular exponentiation: base^exp mod m, using 128-bit intermediate.
 * Used by Miller-Rabin primality test. */
static uint64_t mod_pow64(uint64_t base, uint64_t exp, uint64_t m) {
    __uint128_t result = 1;
    __uint128_t b = base % m;
    while (exp > 0) {
        if (exp & 1) result = (result * b) % m;
        b = (b * b) % m;
        exp >>= 1;
    }
    return (uint64_t)result;
}

/* Deterministic Miller-Rabin primality test for all n < 2^64.
 * With witnesses {2,3,5,7,11,13,17,19,23,29,31,37}, this is exact
 * (no false positives) for n up to at least 3.3 × 10^24. */
static bool is_prime(uint64_t n) {
    if (n < 2) return false;
    if (n < 4) return true;
    if (n % 2 == 0 || n % 3 == 0) return false;
    /* Small primes fast path */
    if (n < 49) return true;  /* 2..47 are prime if they passed above checks */

    /* Write n-1 = d * 2^r */
    uint64_t d = n - 1;
    int r = 0;
    while ((d & 1) == 0) { d >>= 1; r++; }

    /* Test with known witnesses sufficient for all 64-bit integers */
    static const uint64_t witnesses[] = {2,3,5,7,11,13,17,19,23,29,31,37};
    for (int w = 0; w < 12; w++) {
        uint64_t a = witnesses[w];
        if (a >= n) continue;  /* skip witnesses >= n */
        uint64_t x = mod_pow64(a, d, n);
        if (x == 1 || x == n - 1) continue;
        bool found = false;
        for (int i = 0; i < r - 1; i++) {
            x = (uint64_t)(((__uint128_t)x * x) % n);
            if (x == n - 1) { found = true; break; }
        }
        if (!found) return false;
    }
    return true;
}

static uint64_t gcd64(uint64_t a, uint64_t b) {
    while (b) { uint64_t t = b; b = a % b; a = t; }
    return a;
}

/* ---- NTT infrastructure: factoring, generator search, root chain ---- */

/* Factor out all powers of 2 from n.  Returns the 2-adic valuation k
 * and sets *odd_part = n / 2^k.  Returns 0 if n == 0. */
static int two_adic_val(uint64_t n, uint64_t *odd_part) {
    if (n == 0) { *odd_part = 0; return 0; }
    int k = 0;
    while ((n & 1) == 0) { n >>= 1; k++; }
    *odd_part = n;
    return k;
}

/* Trial-division factoring of an odd number n.
 * Writes distinct prime factors to factors[] and returns the count.
 * For p-1 of a 64-bit prime, the odd part is at most ~2^63 and typically
 * factors quickly (Goldilocks odd part = 4294967295 = 3*5*17*257*65537). */
static int trial_factor_odd(uint64_t n, uint64_t *factors, int max_factors) {
    int nf = 0;
    for (uint64_t d = 3; d * d <= n && nf < max_factors; d += 2) {
        if (n % d == 0) {
            factors[nf++] = d;
            while (n % d == 0) n /= d;
        }
    }
    if (n > 1 && nf < max_factors) factors[nf++] = n;
    return nf;
}

/* Find the smallest generator of Z/pZ*.
 * prime_factors[] must contain ALL distinct prime factors of p-1 (including 2).
 * Verifies g^((p-1)/q) != 1 for each prime factor q. */
static uint64_t find_generator(uint64_t p, const uint64_t *prime_factors, int nfactors) {
    for (uint64_t g = 2; g < p; g++) {
        bool is_gen = true;
        for (int i = 0; i < nfactors; i++) {
            if (mod_pow64(g, (p - 1) / prime_factors[i], p) == 1) {
                is_gen = false;
                break;
            }
        }
        if (is_gen) return g;
    }
    return 0;  /* unreachable for p > 2 */
}

/* Compute NTT root chain for a prime field.
 * Sets fi->ntt_max_log, ntt_generator, ntt_roots[], ntt_inv_roots[], ntt_n_inv[].
 * Called from register_field() after basic field setup.
 * Only computes if the 2-adic valuation is >= 4 (NTT of size >= 16). */
static void compute_ntt_info(FieldInfo *fi) {
    fi->ntt_max_log = 0;
    fi->ntt_generator = 0;

    if (fi->field_kind != FIELD_KIND_PRIME || fi->prime < 3) return;

    uint64_t p = fi->prime;
    uint64_t odd_part;
    int k = two_adic_val(p - 1, &odd_part);

    /* Require 2-adic valuation >= 4 for useful NTT (size >= 16) */
    if (k < 4) return;
    if (k > MAX_NTT_LOG) k = MAX_NTT_LOG;

    /* Collect ALL distinct prime factors of p-1 (including 2) */
    uint64_t factors[64];
    int nf = 0;
    factors[nf++] = 2;  /* always a factor of p-1 for odd prime p */
    nf += trial_factor_odd(odd_part, factors + nf, 63);

    /* Find generator */
    uint64_t g = find_generator(p, factors, nf);
    if (g == 0) return;  /* shouldn't happen */

    fi->ntt_max_log = k;
    fi->ntt_generator = g;

    /* Compute root chain:
     * roots[s] = g^((p-1) / 2^(s+1)) mod p  — primitive 2^(s+1)-th root of unity
     * inv_roots[s] = roots[s]^(p-2) mod p    — Fermat inverse
     * For s = 0..k-1. */
    for (int s = 0; s < k; s++) {
        /* exponent = (p-1) / 2^(s+1).  Careful: (p-1) can be up to ~2^64,
         * but p-1 is divisible by 2^k >= 2^(s+1), so the shift is exact. */
        uint64_t exp = (p - 1) >> (s + 1);
        fi->ntt_roots[s] = mod_pow64(g, exp, p);
        fi->ntt_inv_roots[s] = mod_pow64(fi->ntt_roots[s], p - 2, p);
    }

    /* Compute n_inv table:
     * n_inv[s] = (2^(s+1))^(-1) mod p = (inv(2))^(s+1) mod p */
    uint64_t inv2 = mod_pow64(2, p - 2, p);
    __uint128_t acc = 1;
    for (int s = 0; s < k; s++) {
        acc = (acc * inv2) % p;
        fi->ntt_n_inv[s] = (uint64_t)acc;
    }
}

static ASTNode *ast_alloc(void) {
    ASTNode *n = calloc(1, sizeof(ASTNode));
    if (!n) { fprintf(stderr, "out of memory\n"); exit(1); }
    return n;
}

/* ============================================================
 * Lexer
 * ============================================================ */

static void lex(const char *src) {
    int pos = 0, line = 1, col = 1;
    int len = (int)strlen(src);

    while (pos < len) {
        /* Skip whitespace */
        if (isspace(src[pos])) {
            if (src[pos] == '\n') { line++; col = 1; } else { col++; }
            pos++;
            continue;
        }

        /* Line comment */
        if (pos + 1 < len && src[pos] == '/' && src[pos+1] == '/') {
            while (pos < len && src[pos] != '\n') pos++;
            continue;
        }

        /* Block comment (nestable) */
        if (pos + 1 < len && src[pos] == '/' && src[pos+1] == '*') {
            int depth = 1;
            pos += 2; col += 2;
            while (pos < len && depth > 0) {
                if (src[pos] == '\n') { line++; col = 1; pos++; continue; }
                if (pos + 1 < len && src[pos] == '/' && src[pos+1] == '*') {
                    depth++; pos += 2; col += 2; continue;
                }
                if (pos + 1 < len && src[pos] == '*' && src[pos+1] == '/') {
                    depth--; pos += 2; col += 2; continue;
                }
                pos++; col++;
            }
            continue;
        }

        /* Reserve one slot for the trailing EOF token (emitted after the loop). */
        if (g_ntokens >= MAX_TOKENS - 1) cap_overflow("token buffer (g_tokens)", MAX_TOKENS);
        Token *t = &g_tokens[g_ntokens];
        t->line = line;
        t->col = col;

        /* #[ attribute */
        if (src[pos] == '#' && pos + 1 < len && src[pos+1] == '[') {
            t->kind = TOK_HASH_LBRACKET;
            t->text[0] = '#'; t->text[1] = '['; t->text[2] = 0;
            pos += 2; col += 2;
            g_ntokens++;
            continue;
        }

        /* Number */
        if (isdigit(src[pos])) {
            t->kind = TOK_INT_LIT;
            uint64_t val = 0;
            int start = pos;
            int base = 10;

            if (src[pos] == '0' && pos + 1 < len) {
                if (src[pos+1] == 'x' || src[pos+1] == 'X') {
                    base = 16; pos += 2; col += 2;
                } else if (src[pos+1] == 'b' || src[pos+1] == 'B') {
                    base = 2; pos += 2; col += 2;
                } else if (src[pos+1] == 'o' || src[pos+1] == 'O') {
                    base = 8; pos += 2; col += 2;
                }
            }

            bool lit_ovf = false;
            while (pos < len && (isxdigit(src[pos]) || src[pos] == '_')) {
                if (src[pos] == '_') { pos++; col++; continue; }
                int d = 0;
                if (isdigit(src[pos])) d = src[pos] - '0';
                else if (src[pos] >= 'a' && src[pos] <= 'f') d = src[pos] - 'a' + 10;
                else if (src[pos] >= 'A' && src[pos] <= 'F') d = src[pos] - 'A' + 10;
                /* #56: u64 wrap here silently becomes a DIFFERENT number in
                 * every downstream position (value, Field<p>, match arm). The
                 * seed has NO wide-integer surface, so overflow is always an
                 * error; wide literals are tvc_self's i128..u256 feature. */
                if (val > (UINT64_MAX - (uint64_t)d) / (uint64_t)base) lit_ovf = true;
                val = val * base + d;
                pos++; col++;
            }
            if (lit_ovf) {
                error(line, col, "integer literal exceeds 64 bits (wide "
                      "literals require the self-hosted compiler tvc_self)");
                exit(1);
            }

            t->int_val = val;
            int tlen = pos - start;
            if (tlen >= MAX_IDENT) tlen = MAX_IDENT - 1;
            memcpy(t->text, src + start, tlen);
            t->text[tlen] = 0;
            g_ntokens++;
            continue;
        }

        /* String literal */
        if (src[pos] == '"') {
            t->kind = TOK_STR_LIT;
            pos++; col++; /* skip opening quote */
            int j = 0;
            while (pos < len && src[pos] != '"') {
                if (src[pos] == '\\' && pos + 1 < len) {
                    pos++; col++;
                    switch (src[pos]) {
                        case 'n': t->text[j++] = '\n'; break;
                        case 't': t->text[j++] = '\t'; break;
                        case 'r': t->text[j++] = '\r'; break;
                        case '0': t->text[j++] = '\0'; break;
                        case '\\': t->text[j++] = '\\'; break;
                        case '"': t->text[j++] = '"'; break;
                        default: t->text[j++] = src[pos]; break;
                    }
                } else {
                    t->text[j++] = src[pos];
                }
                pos++; col++;
                if (j >= MAX_IDENT - 1) break;
            }
            t->text[j] = 0;
            if (pos < len && src[pos] == '"') { pos++; col++; } /* skip closing quote */
            g_ntokens++;
            continue;
        }

        /* Character literal: 'a', '\n', '\0', etc. → TOK_INT_LIT with byte value */
        if (src[pos] == '\'') {
            t->kind = TOK_INT_LIT;
            pos++; col++; /* skip opening quote */
            uint64_t ch;
            if (src[pos] == '\\' && pos + 1 < len) {
                pos++; col++;
                switch (src[pos]) {
                    case 'n':  ch = '\n'; break;
                    case 't':  ch = '\t'; break;
                    case 'r':  ch = '\r'; break;
                    case '0':  ch = '\0'; break;
                    case '\\': ch = '\\'; break;
                    case '\'': ch = '\''; break;
                    default:   ch = (unsigned char)src[pos]; break;
                }
            } else {
                ch = (unsigned char)src[pos];
            }
            pos++; col++;
            if (pos < len && src[pos] == '\'') { pos++; col++; } /* skip closing quote */
            t->int_val = ch;
            snprintf(t->text, MAX_IDENT, "%llu", (unsigned long long)ch);
            g_ntokens++;
            continue;
        }

        /* Identifier or keyword */
        if (isalpha(src[pos]) || src[pos] == '_') {
            int start = pos;
            while (pos < len && (isalnum(src[pos]) || src[pos] == '_')) {
                pos++; col++;
            }
            int slen = pos - start;
            if (slen >= MAX_IDENT) slen = MAX_IDENT - 1;
            memcpy(t->text, src + start, slen);
            t->text[slen] = 0;

            /* Keywords */
            if      (!strcmp(t->text, "field"))  t->kind = TOK_KW_FIELD;
            else if (!strcmp(t->text, "fn"))     t->kind = TOK_KW_FN;
            else if (!strcmp(t->text, "let"))    t->kind = TOK_KW_LET;
            else if (!strcmp(t->text, "mut"))    t->kind = TOK_KW_MUT;
            else if (!strcmp(t->text, "var"))    t->kind = TOK_KW_VAR;  /* == let mut */
            else if (!strcmp(t->text, "type"))   t->kind = TOK_KW_TYPE; /* field-alias decl */
            else if (!strcmp(t->text, "if"))     t->kind = TOK_KW_IF;
            else if (!strcmp(t->text, "else"))   t->kind = TOK_KW_ELSE;
            else if (!strcmp(t->text, "for"))    t->kind = TOK_KW_FOR;
            else if (!strcmp(t->text, "in"))     t->kind = TOK_KW_IN;
            else if (!strcmp(t->text, "while"))  t->kind = TOK_KW_WHILE;
            else if (!strcmp(t->text, "return")) t->kind = TOK_KW_RETURN;
            else if (!strcmp(t->text, "true"))   { t->kind = TOK_KW_TRUE; t->bool_val = true; }
            else if (!strcmp(t->text, "false"))  { t->kind = TOK_KW_FALSE; t->bool_val = false; }
            else if (!strcmp(t->text, "as"))     t->kind = TOK_KW_AS;
            else if (!strcmp(t->text, "const"))  t->kind = TOK_KW_CONST;
            else if (!strcmp(t->text, "pub"))    t->kind = TOK_KW_PUB;
            else if (!strcmp(t->text, "struct")) t->kind = TOK_KW_STRUCT;
            else if (!strcmp(t->text, "enum"))   t->kind = TOK_KW_ENUM;
            else if (!strcmp(t->text, "match"))  t->kind = TOK_KW_MATCH;
            else if (!strcmp(t->text, "null"))   t->kind = TOK_KW_NULL;
            else if (!strcmp(t->text, "unsafe")) t->kind = TOK_KW_UNSAFE;
            else if (!strcmp(t->text, "extern")) t->kind = TOK_KW_EXTERN;
            else if (!strcmp(t->text, "break"))  t->kind = TOK_KW_BREAK;
            else if (!strcmp(t->text, "continue")) t->kind = TOK_KW_CONTINUE;
            else if (!strcmp(t->text, "binfield")) t->kind = TOK_KW_BINFIELD;
            else if (!strcmp(t->text, "extfield")) t->kind = TOK_KW_EXTFIELD;
            else if (!strcmp(t->text, "instantiate")) t->kind = TOK_KW_INSTANTIATE;
            /* `print` is DE-RESERVED (plan-syntax-modernization Phase 1): it lexes
             * as a plain identifier and routes through the ordinary ident-call
             * path (codegen dispatches to the stdout builtin by name). Keeping the
             * keyword out here is the whole change; TOK_KW_PRINT is now unused. */
            /* Type keywords */
            else if (!strcmp(t->text, "u8"))     t->kind = TOK_TY_U8;
            else if (!strcmp(t->text, "u16"))    t->kind = TOK_TY_U16;
            else if (!strcmp(t->text, "u32"))    t->kind = TOK_TY_U32;
            else if (!strcmp(t->text, "u64"))    t->kind = TOK_TY_U64;
            else if (!strcmp(t->text, "i8"))     t->kind = TOK_TY_I8;
            else if (!strcmp(t->text, "i16"))    t->kind = TOK_TY_I16;
            else if (!strcmp(t->text, "i32"))    t->kind = TOK_TY_I32;
            else if (!strcmp(t->text, "i64"))    t->kind = TOK_TY_I64;
            else if (!strcmp(t->text, "usize"))  t->kind = TOK_TY_USIZE;
            else if (!strcmp(t->text, "bool"))   t->kind = TOK_TY_BOOL;
            else if (!strcmp(t->text, "Field"))  t->kind = TOK_TY_FIELD;
            else if (!strcmp(t->text, "BinField")) t->kind = TOK_TY_BINFIELD;
            else if (!strcmp(t->text, "ExtField")) t->kind = TOK_TY_EXTFIELD;
             else if (!strcmp(t->text, "Poly"))     t->kind = TOK_TY_POLY;
             else if (!strcmp(t->text, "Register")) t->kind = TOK_TY_REGISTER;
             else if (!strcmp(t->text, "project")) { t->kind = TOK_IDENT; } /* builtin fn */
            else    t->kind = TOK_IDENT;

            g_ntokens++;
            continue;
        }

        /* Multi-char operators */
        char c = src[pos];
        char c2 = (pos + 1 < len) ? src[pos+1] : 0;
        char c3 = (pos + 2 < len) ? src[pos+2] : 0;

        if (c == '*' && c2 == '*') { t->kind = TOK_STARSTAR; strcpy(t->text, "**"); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == '-' && c2 == '>') { t->kind = TOK_ARROW; strcpy(t->text, "->"); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == '=' && c2 == '>') { t->kind = TOK_FAT_ARROW; strcpy(t->text, "=>"); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == ':' && c2 == ':') { t->kind = TOK_COLONCOLON; strcpy(t->text, "::"); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == '.' && c2 == '.') { t->kind = TOK_DOTDOT; strcpy(t->text, ".."); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == '=' && c2 == '=') { t->kind = TOK_EQ; strcpy(t->text, "=="); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == '!' && c2 == '=') { t->kind = TOK_NEQ; strcpy(t->text, "!="); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == '<' && c2 == '=') { t->kind = TOK_LE; strcpy(t->text, "<="); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == '>' && c2 == '=') { t->kind = TOK_GE; strcpy(t->text, ">="); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == '&' && c2 == '&') { t->kind = TOK_AND; strcpy(t->text, "&&"); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == '|' && c2 == '|') { t->kind = TOK_OR; strcpy(t->text, "||"); pos += 2; col += 2; g_ntokens++; continue; }

        /* Compound assignment: `x op= e`. Longest-match — the 3-char shift forms
         * (<<= >>=) MUST precede the plain << >> below; every 2-char form has a
         * distinct c2=='=' that no earlier operator claims. The binary OpKind
         * rides in int_val; the parser desugars to `x = x op e`. */
        {
            int cmpop = -1, cmplen = 2;
            if (c == '<' && c2 == '<' && c3 == '=') { cmpop = OP_SHL; cmplen = 3; }
            else if (c == '>' && c2 == '>' && c3 == '=') { cmpop = OP_SHR; cmplen = 3; }
            else if (c2 == '=') {
                if      (c == '+') cmpop = OP_ADD;
                else if (c == '-') cmpop = OP_SUB;
                else if (c == '*') cmpop = OP_MUL;
                else if (c == '/') cmpop = OP_DIV;
                else if (c == '%') cmpop = OP_MOD;
                else if (c == '&') cmpop = OP_BITAND;
                else if (c == '|') cmpop = OP_BITOR;
                else if (c == '^') cmpop = OP_BITXOR;
            }
            if (cmpop >= 0) {
                t->kind = TOK_OP_ASSIGN;
                t->int_val = (uint64_t)cmpop;
                t->text[0] = c; t->text[1] = c2;
                if (cmplen == 3) { t->text[2] = c3; t->text[3] = 0; } else { t->text[2] = 0; }
                pos += cmplen; col += cmplen; g_ntokens++; continue;
            }
        }

        if (c == '<' && c2 == '<') { t->kind = TOK_SHL; strcpy(t->text, "<<"); pos += 2; col += 2; g_ntokens++; continue; }
        if (c == '>' && c2 == '>') { t->kind = TOK_SHR; strcpy(t->text, ">>"); pos += 2; col += 2; g_ntokens++; continue; }

        /* Single-char */
        switch (c) {
            case '+': t->kind = TOK_PLUS; break;
            case '-': t->kind = TOK_MINUS; break;
            case '*': t->kind = TOK_STAR; break;
            case '/': t->kind = TOK_SLASH; break;
            case '%': t->kind = TOK_PERCENT; break;
            case '=': t->kind = TOK_ASSIGN; break;
            case '<': t->kind = TOK_LT; break;
            case '>': t->kind = TOK_GT; break;
            case '!': t->kind = TOK_NOT; break;
            case '&': t->kind = TOK_AMPERSAND; break;
            case '|': t->kind = TOK_PIPE; break;
            case '^': t->kind = TOK_CARET; break;
            case '~': t->kind = TOK_TILDE; break;
            case '(': t->kind = TOK_LPAREN; break;
            case ')': t->kind = TOK_RPAREN; break;
            case '{': t->kind = TOK_LBRACE; break;
            case '}': t->kind = TOK_RBRACE; break;
            case '[': t->kind = TOK_LBRACKET; break;
            case ']': t->kind = TOK_RBRACKET; break;
            case ';': t->kind = TOK_SEMI; break;
            case ':': t->kind = TOK_COLON; break;
            case ',': t->kind = TOK_COMMA; break;
            case '.': t->kind = TOK_DOT; break;
            default:
                error(line, col, "unexpected character '%c' (0x%02x)", c, (unsigned char)c);
                pos++; col++;
                continue;
        }
        t->text[0] = c; t->text[1] = 0;
        pos++; col++;
        g_ntokens++;
    }

    /* EOF token */
    Token *eof = &g_tokens[g_ntokens];
    eof->kind = TOK_EOF;
    eof->line = line; eof->col = col;
    strcpy(eof->text, "<eof>");
    g_ntokens++;
}

/* ============================================================
 * Parser helpers
 * ============================================================ */

static Token *peek(void) { return &g_tokens[g_tok_pos]; }
static Token *advance(void) { return &g_tokens[g_tok_pos++]; }

static bool check(TokenKind k) { return peek()->kind == k; }

static Token *expect(TokenKind k, const char *msg) {
    Token *t = peek();
    if (t->kind != k) {
        error(t->line, t->col, "expected %s, got '%s'", msg, t->text);
        return t;
    }
    return advance();
}

static bool match(TokenKind k) {
    if (check(k)) { advance(); return true; }
    return false;
}

/* ============================================================
 * Parser
 * ============================================================ */

static ASTNode *parse_expr(void);
static ASTNode *parse_block(void);
static ASTNode *parse_stmt(void);

/* parse_primary: literals, identifiers, calls, field access, parens */
static ASTNode *parse_primary(void) {
    Token *t = peek();

    /* String literal */
    if (t->kind == TOK_STR_LIT) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_STR_LIT;
        n->line = t->line; n->col = t->col;
        strcpy(n->ident.name, t->text); /* reuse ident.name for the string data */
        return n;
    }

    /* Integer literal */
    if (t->kind == TOK_INT_LIT) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_LIT_INT;
        n->line = t->line; n->col = t->col;
        n->lit_int.value = t->int_val;
        return n;
    }

    /* Bool literal */
    if (t->kind == TOK_KW_TRUE || t->kind == TOK_KW_FALSE) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_LIT_BOOL;
        n->line = t->line; n->col = t->col;
        n->lit_bool.value = (t->kind == TOK_KW_TRUE);
        return n;
    }

    /* null literal */
    if (t->kind == TOK_KW_NULL) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_NULL;
        n->line = t->line; n->col = t->col;
        return n;
    }

    /* field(p) — runtime Field construction builtin.  'field' lexes as
     * TOK_KW_FIELD (the top-level alias keyword), so expression position
     * needs this special case.  Only fires when followed by '(' — bare
     * 'field' elsewhere stays a syntax error as before. */
    if (t->kind == TOK_KW_FIELD && g_tok_pos + 1 < g_ntokens &&
        g_tokens[g_tok_pos + 1].kind == TOK_LPAREN) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_CALL;
        n->line = t->line; n->col = t->col;
        strcpy(n->call.name, "field");
        expect(TOK_LPAREN, "'('");
        n->call.args = malloc(sizeof(ASTNode*) * MAX_PARAMS);
        n->call.nargs = 0;
        if (!check(TOK_RPAREN)) {
            n->call.args[n->call.nargs++] = parse_expr();
            while (match(TOK_COMMA)) {
                if (n->call.nargs >= MAX_PARAMS) {
                    error(peek()->line, peek()->col, "too many arguments (max %d)", MAX_PARAMS);
                    break;
                }
                n->call.args[n->call.nargs++] = parse_expr();
            }
        }
        expect(TOK_RPAREN, "')'");
        return n;
    }

    /* print(expr) — treated as builtin */
    if (t->kind == TOK_KW_PRINT) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_CALL;
        n->line = t->line; n->col = t->col;
        strcpy(n->call.name, "print");
        expect(TOK_LPAREN, "'('");
        n->call.args = malloc(sizeof(ASTNode*) * MAX_PARAMS);
        n->call.nargs = 0;
        if (!check(TOK_RPAREN)) {
            n->call.args[n->call.nargs++] = parse_expr();
            while (match(TOK_COMMA)) {
                if (n->call.nargs >= MAX_PARAMS) {
                    error(peek()->line, peek()->col, "too many arguments (max %d)", MAX_PARAMS);
                    break;
                }
                n->call.args[n->call.nargs++] = parse_expr();
            }
        }
        expect(TOK_RPAREN, "')'");
        return n;
    }

    /* Identifier, call, or field access */
    if (t->kind == TOK_IDENT) {
        advance();

        /* project(expr) builtin */
        if (!strcmp(t->text, "project") && check(TOK_LPAREN)) {
            ASTNode *n = ast_alloc();
            n->kind = AST_PROJECT;
            n->line = t->line; n->col = t->col;
            expect(TOK_LPAREN, "'('");
            n->project.expr = parse_expr();
            expect(TOK_RPAREN, "')'");
            return n;
        }

        /* Ident::Member — enum construction OR field constant */
        if (check(TOK_COLONCOLON)) {
            advance();
            Token *member = expect(TOK_IDENT, "member name");

            /* Check enum variant first (enum takes priority over field) */
            EnumInfo *ei = find_enum(t->text);
            if (ei) {
                ASTNode *n = ast_alloc();
                n->kind = AST_ENUM_CONSTRUCT;
                n->line = t->line; n->col = t->col;
                strcpy(n->enum_construct.enum_name, t->text);
                strcpy(n->enum_construct.variant_name, member->text);
                n->enum_construct.nargs = 0;
                /* Optional tuple payload: Variant(arg, arg, ...) */
                if (match(TOK_LPAREN)) {
                    while (!check(TOK_RPAREN)) {
                        if (n->enum_construct.nargs > 0) expect(TOK_COMMA, "','");
                        if (n->enum_construct.nargs >= MAX_PARAMS) {
                            error(peek()->line, peek()->col, "too many enum arguments (max %d)", MAX_PARAMS);
                            break;
                        }
                        n->enum_construct.args[n->enum_construct.nargs++] = parse_expr();
                    }
                    expect(TOK_RPAREN, "')'");
                }
                return n;
            }

            /* F::from(expr) */
            if (!strcmp(member->text, "from") && check(TOK_LPAREN)) {
                advance();
                ASTNode *n = ast_alloc();
                n->kind = AST_CAST;
                n->line = t->line; n->col = t->col;
                n->cast.expr = parse_expr();
                strcpy(n->cast.type_name, t->text);
                expect(TOK_RPAREN, "')'");
                return n;
            }

            /* F::ZERO, F::ONE, F::PRIME */
            ASTNode *n = ast_alloc();
            n->kind = AST_FIELD_ACCESS;
            n->line = t->line; n->col = t->col;
            strcpy(n->field_access.obj, t->text);
            strcpy(n->field_access.member, member->text);
            return n;
        }

        /* Function call */
        if (check(TOK_LPAREN)) {
            advance();
            ASTNode *n = ast_alloc();
            n->kind = AST_CALL;
            n->line = t->line; n->col = t->col;
            strcpy(n->call.name, t->text);
            n->call.args = malloc(sizeof(ASTNode*) * MAX_PARAMS);
            n->call.nargs = 0;
            if (!check(TOK_RPAREN)) {
                n->call.args[n->call.nargs++] = parse_expr();
                while (match(TOK_COMMA)) {
                    if (n->call.nargs >= MAX_PARAMS) {
                        error(peek()->line, peek()->col, "too many arguments (max %d)", MAX_PARAMS);
                        break;
                    }
                    n->call.args[n->call.nargs++] = parse_expr();
                }
            }
            expect(TOK_RPAREN, "')'");
            return n;
        }

        /* Array index: ident[expr] with optional .member chaining */
        if (check(TOK_LBRACKET)) {
            advance();
            ASTNode *n = ast_alloc();
            n->kind = AST_INDEX;
            n->line = t->line; n->col = t->col;
            strcpy(n->index_expr.name, t->text);
            n->index_expr.index = parse_expr();
            expect(TOK_RBRACKET, "']'");

            /* Postfix .member and [index] chaining: tokens[i].kind, tokens[i].text[j] */
            for (;;) {
                if (check(TOK_DOT)) {
                    advance();
                    Token *member = expect(TOK_IDENT, "member name");
                    /* Method call: expr.method(args) -> method(&expr, args) */
                    if (check(TOK_LPAREN)) {
                        advance(); /* consume ( */
                        ASTNode *call = ast_alloc();
                        call->kind = AST_CALL;
                        call->line = n->line; call->col = n->col;
                        strcpy(call->call.name, member->text);
                        call->call.args = malloc(sizeof(ASTNode*) * MAX_PARAMS);
                        /* First arg: &receiver */
                        ASTNode *addr = ast_alloc();
                        addr->kind = AST_UNARY;
                        addr->line = n->line; addr->col = n->col;
                        addr->unary.op = OP_ADDR;
                        addr->unary.operand = n;
                        call->call.args[0] = addr;
                        call->call.nargs = 1;
                        /* Parse remaining args */
                        if (!check(TOK_RPAREN)) {
                            do {
                                call->call.args[call->call.nargs++] = parse_expr();
                            } while (check(TOK_COMMA) && advance());
                        }
                        expect(TOK_RPAREN, "')'");
                        n = call;
                        continue;
                    }
                    ASTNode *acc = ast_alloc();
                    acc->kind = AST_MEMBER_ACCESS;
                    acc->line = n->line; acc->col = n->col;
                    acc->member_access.obj_name[0] = 0;
                    strcpy(acc->member_access.member, member->text);
                    acc->member_access.obj_expr = n;
                    n = acc;
                } else if (check(TOK_LBRACKET)) {
                    advance();
                    ASTNode *idx_node = ast_alloc();
                    idx_node->kind = AST_INDEX;
                    idx_node->line = n->line; idx_node->col = n->col;
                    idx_node->index_expr.name[0] = 0;
                    idx_node->index_expr.obj_expr = n;
                    idx_node->index_expr.index = parse_expr();
                    expect(TOK_RBRACKET, "']'");
                    n = idx_node;
                } else {
                    break;
                }
            }
            return n;
        }

        /* Struct literal: Name { field: expr, ... }
         * Disambiguate from block: check if name is a registered struct. */
        if (check(TOK_LBRACE) && find_struct(t->text)) {
            advance(); /* consume { */
            ASTNode *n = ast_alloc();
            n->kind = AST_STRUCT_LIT;
            n->line = t->line; n->col = t->col;
            strcpy(n->struct_lit.type_name, t->text);
            n->struct_lit.nfields = 0;
            while (!check(TOK_RBRACE)) {
                if (n->struct_lit.nfields >= MAX_PARAMS) {
                    error(peek()->line, peek()->col, "too many struct fields (max %d)", MAX_PARAMS);
                    break;
                }
                int i = n->struct_lit.nfields;
                Token *fname = expect(TOK_IDENT, "field name");
                strcpy(n->struct_lit.field_names[i], fname->text);
                expect(TOK_COLON, "':'");
                n->struct_lit.field_values[i] = parse_expr();
                n->struct_lit.nfields++;
                if (!check(TOK_RBRACE)) expect(TOK_COMMA, "','");
            }
            expect(TOK_RBRACE, "'}'");
            return n;
        }

        /* Dot access: ident.member with chaining (b.next.value) */
        if (check(TOK_DOT)) {
            advance();
            Token *member = expect(TOK_IDENT, "member name");

            /* Method call: ident.method(args) -> method(&ident, args) */
            if (check(TOK_LPAREN)) {
                advance(); /* consume ( */
                ASTNode *call = ast_alloc();
                call->kind = AST_CALL;
                call->line = t->line; call->col = t->col;
                strcpy(call->call.name, member->text);
                call->call.args = malloc(sizeof(ASTNode*) * MAX_PARAMS);
                /* First arg: &receiver (the simple identifier) */
                ASTNode *ident = ast_alloc();
                ident->kind = AST_IDENT;
                ident->line = t->line; ident->col = t->col;
                strcpy(ident->ident.name, t->text);
                ASTNode *addr = ast_alloc();
                addr->kind = AST_UNARY;
                addr->line = t->line; addr->col = t->col;
                addr->unary.op = OP_ADDR;
                addr->unary.operand = ident;
                call->call.args[0] = addr;
                call->call.nargs = 1;
                /* Parse remaining args */
                if (!check(TOK_RPAREN)) {
                    do {
                        call->call.args[call->call.nargs++] = parse_expr();
                    } while (check(TOK_COMMA) && advance());
                }
                expect(TOK_RPAREN, "')'");
                return call;
            }

            ASTNode *n = ast_alloc();
            n->kind = AST_MEMBER_ACCESS;
            n->line = t->line; n->col = t->col;
            strcpy(n->member_access.obj_name, t->text);
            strcpy(n->member_access.member, member->text);
            n->member_access.obj_expr = NULL;

            /* Chain additional .member and [index] accesses */
            for (;;) {
                if (check(TOK_DOT)) {
                    advance();
                    Token *next_member = expect(TOK_IDENT, "member name");
                    ASTNode *chained = ast_alloc();
                    chained->kind = AST_MEMBER_ACCESS;
                    chained->line = n->line; chained->col = n->col;
                    chained->member_access.obj_name[0] = 0; /* no name — use obj_expr */
                    strcpy(chained->member_access.member, next_member->text);
                    chained->member_access.obj_expr = n;
                    n = chained;
                } else if (check(TOK_LBRACKET)) {
                    /* b.data[0] — index into member-access result */
                    advance();
                    ASTNode *idx_node = ast_alloc();
                    idx_node->kind = AST_INDEX;
                    idx_node->line = n->line; idx_node->col = n->col;
                    idx_node->index_expr.name[0] = 0; /* no name — use obj_expr */
                    idx_node->index_expr.obj_expr = n;
                    idx_node->index_expr.index = parse_expr();
                    expect(TOK_RBRACKET, "']'");
                    n = idx_node;
                } else {
                    break;
                }
            }
            return n;
        }

        /* Simple identifier */
        ASTNode *n = ast_alloc();
        n->kind = AST_IDENT;
        n->line = t->line; n->col = t->col;
        strcpy(n->ident.name, t->text);
        return n;
    }

    /* Parenthesized expression */
    if (t->kind == TOK_LPAREN) {
        advance();
        ASTNode *n = parse_expr();
        expect(TOK_RPAREN, "')'");
        return n;
    }

    /* If expression */
    if (t->kind == TOK_KW_IF) {
        return parse_expr(); /* handled in parse_unary->parse_primary chain */
    }

    error(t->line, t->col, "unexpected token '%s' in expression", t->text);
    advance();
    ASTNode *n = ast_alloc();
    n->kind = AST_LIT_INT;
    n->lit_int.value = 0;
    return n;
}

/* parse_unary: -, ! */
static ASTNode *parse_unary(void) {
    Token *t = peek();

    /* If expression (also valid as unary-level) */
    if (t->kind == TOK_KW_IF) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_IF;
        n->line = t->line; n->col = t->col;
        n->if_expr.cond = parse_expr();
        n->if_expr.then_b = parse_block();
        if (match(TOK_KW_ELSE)) {
            if (check(TOK_KW_IF)) {
                /* else if chain */
                ASTNode *blk = ast_alloc();
                blk->kind = AST_BLOCK;
                blk->block.stmts = malloc(sizeof(ASTNode*));
                ASTNode *inner_if = parse_unary(); /* parse the if expression */
                ASTNode *stmt = ast_alloc();
                stmt->kind = AST_EXPR_STMT;
                stmt->expr_stmt.expr = inner_if;
                blk->block.stmts[0] = stmt;
                blk->block.nstmts = 1;
                n->if_expr.else_b = blk;
            } else {
                n->if_expr.else_b = parse_block();
            }
        }
        return n;
    }

    if (t->kind == TOK_MINUS) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_UNARY;
        n->line = t->line; n->col = t->col;
        n->unary.op = OP_NEG;
        n->unary.operand = parse_unary();
        return n;
    }
    if (t->kind == TOK_NOT) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_UNARY;
        n->line = t->line; n->col = t->col;
        n->unary.op = OP_NOT;
        n->unary.operand = parse_unary();
        return n;
    }
    if (t->kind == TOK_TILDE) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_UNARY;
        n->line = t->line; n->col = t->col;
        n->unary.op = OP_BITNOT;
        n->unary.operand = parse_unary();
        return n;
    }
    if (t->kind == TOK_AMPERSAND) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_UNARY;
        n->line = t->line; n->col = t->col;
        n->unary.op = OP_ADDR;
        n->unary.operand = parse_unary();
        return n;
    }

    ASTNode *result = parse_primary();

    /* Postfix 'as' cast: expr as Type.
     * High precedence — binds tighter than all binary operators. */
    while (check(TOK_KW_AS)) {
        advance();
        ASTNode *cast = ast_alloc();
        cast->kind = AST_CAST;
        cast->line = result->line; cast->col = result->col;
        cast->cast.expr = result;
        /* Parse the target type — must handle *T, u8, i32, usize, etc. */
        Token *ty_tok = peek();
        if (ty_tok->kind == TOK_STAR) {
            /* *T pointer type */
            advance();
            Token *pointee = advance();
            char type_buf[MAX_IDENT];
            snprintf(type_buf, sizeof(type_buf), "*%s", pointee->text);
            strcpy(cast->cast.type_name, type_buf);
        } else {
            Token *ty = advance();
            strcpy(cast->cast.type_name, ty->text);
        }
        result = cast;
    }
    return result;
}

/* parse_power: right-associative ** */
static ASTNode *parse_power(void) {
    ASTNode *lhs = parse_unary();
    if (check(TOK_STARSTAR)) {
        Token *op = advance();
        ASTNode *rhs = parse_power(); /* right-assoc */
        ASTNode *n = ast_alloc();
        n->kind = AST_BINARY;
        n->line = op->line; n->col = op->col;
        n->binary.op = OP_POW;
        n->binary.lhs = lhs;
        n->binary.rhs = rhs;
        return n;
    }
    return lhs;
}

/* parse_mul: *, /, % */
static ASTNode *parse_mul(void) {
    ASTNode *lhs = parse_power();
    while (check(TOK_STAR) || check(TOK_SLASH) || check(TOK_PERCENT)) {
        Token *op = advance();
        OpKind ok = (op->kind == TOK_STAR) ? OP_MUL :
                    (op->kind == TOK_SLASH) ? OP_DIV : OP_MOD;
        ASTNode *rhs = parse_power();
        ASTNode *n = ast_alloc();
        n->kind = AST_BINARY;
        n->line = op->line; n->col = op->col;
        n->binary.op = ok;
        n->binary.lhs = lhs;
        n->binary.rhs = rhs;
        lhs = n;
    }
    return lhs;
}

/* parse_add: +, - */
static ASTNode *parse_add(void) {
    ASTNode *lhs = parse_mul();
    while (check(TOK_PLUS) || check(TOK_MINUS)) {
        Token *op = advance();
        OpKind ok = (op->kind == TOK_PLUS) ? OP_ADD : OP_SUB;
        ASTNode *rhs = parse_mul();
        ASTNode *n = ast_alloc();
        n->kind = AST_BINARY;
        n->line = op->line; n->col = op->col;
        n->binary.op = ok;
        n->binary.lhs = lhs;
        n->binary.rhs = rhs;
        lhs = n;
    }
    return lhs;
}

/* parse_shift: <<, >> */
static ASTNode *parse_shift(void) {
    ASTNode *lhs = parse_add();
    while (check(TOK_SHL) || check(TOK_SHR)) {
        Token *op = advance();
        OpKind ok = (op->kind == TOK_SHL) ? OP_SHL : OP_SHR;
        ASTNode *rhs = parse_add();
        ASTNode *n = ast_alloc();
        n->kind = AST_BINARY;
        n->line = op->line; n->col = op->col;
        n->binary.op = ok;
        n->binary.lhs = lhs;
        n->binary.rhs = rhs;
        lhs = n;
    }
    return lhs;
}

/* parse_comparison: ==, !=, <, >, <=, >= */
static ASTNode *parse_comparison(void) {
    ASTNode *lhs = parse_shift();
    while (check(TOK_EQ) || check(TOK_NEQ) || check(TOK_LT) ||
           check(TOK_GT) || check(TOK_LE) || check(TOK_GE)) {
        Token *op = advance();
        OpKind ok;
        switch (op->kind) {
            case TOK_EQ:  ok = OP_EQ; break;
            case TOK_NEQ: ok = OP_NEQ; break;
            case TOK_LT:  ok = OP_LT; break;
            case TOK_GT:  ok = OP_GT; break;
            case TOK_LE:  ok = OP_LE; break;
            case TOK_GE:  ok = OP_GE; break;
            default:      ok = OP_EQ; break;
        }
        ASTNode *rhs = parse_add();
        ASTNode *n = ast_alloc();
        n->kind = AST_BINARY;
        n->line = op->line; n->col = op->col;
        n->binary.op = ok;
        n->binary.lhs = lhs;
        n->binary.rhs = rhs;
        lhs = n;
    }
    return lhs;
}

/* parse_bitand: & */
static ASTNode *parse_bitand(void) {
    ASTNode *lhs = parse_comparison();
    while (check(TOK_AMPERSAND)) {
        Token *op = advance();
        ASTNode *rhs = parse_comparison();
        ASTNode *n = ast_alloc();
        n->kind = AST_BINARY;
        n->line = op->line; n->col = op->col;
        n->binary.op = OP_BITAND;
        n->binary.lhs = lhs;
        n->binary.rhs = rhs;
        lhs = n;
    }
    return lhs;
}

/* parse_bitxor: ^ */
static ASTNode *parse_bitxor(void) {
    ASTNode *lhs = parse_bitand();
    while (check(TOK_CARET)) {
        Token *op = advance();
        ASTNode *rhs = parse_bitand();
        ASTNode *n = ast_alloc();
        n->kind = AST_BINARY;
        n->line = op->line; n->col = op->col;
        n->binary.op = OP_BITXOR;
        n->binary.lhs = lhs;
        n->binary.rhs = rhs;
        lhs = n;
    }
    return lhs;
}

/* parse_bitor: | */
static ASTNode *parse_bitor(void) {
    ASTNode *lhs = parse_bitxor();
    while (check(TOK_PIPE)) {
        Token *op = advance();
        ASTNode *rhs = parse_bitxor();
        ASTNode *n = ast_alloc();
        n->kind = AST_BINARY;
        n->line = op->line; n->col = op->col;
        n->binary.op = OP_BITOR;
        n->binary.lhs = lhs;
        n->binary.rhs = rhs;
        lhs = n;
    }
    return lhs;
}

/* parse_logic_and: && */
static ASTNode *parse_logic_and(void) {
    ASTNode *lhs = parse_bitor();
    while (check(TOK_AND)) {
        Token *op = advance();
        ASTNode *rhs = parse_comparison();
        ASTNode *n = ast_alloc();
        n->kind = AST_BINARY;
        n->line = op->line; n->col = op->col;
        n->binary.op = OP_AND;
        n->binary.lhs = lhs;
        n->binary.rhs = rhs;
        lhs = n;
    }
    return lhs;
}

/* parse_logic_or: || */
static ASTNode *parse_expr(void) {
    ASTNode *lhs = parse_logic_and();
    while (check(TOK_OR)) {
        Token *op = advance();
        ASTNode *rhs = parse_logic_and();
        ASTNode *n = ast_alloc();
        n->kind = AST_BINARY;
        n->line = op->line; n->col = op->col;
        n->binary.op = OP_OR;
        n->binary.lhs = lhs;
        n->binary.rhs = rhs;
        lhs = n;
    }
    return lhs;
}

static ASTNode *parse_block(void) {
    expect(TOK_LBRACE, "'{'");
    ASTNode *blk = ast_alloc();
    blk->kind = AST_BLOCK;
    blk->line = peek()->line; blk->col = peek()->col;
    blk->block.stmts = malloc(sizeof(ASTNode*) * 1024);
    blk->block.nstmts = 0;

    while (!check(TOK_RBRACE) && !check(TOK_EOF)) {
        if (blk->block.nstmts >= 1024) {
            error(peek()->line, peek()->col, "too many statements in block (max 1024)");
            break;
        }
        blk->block.stmts[blk->block.nstmts++] = parse_stmt();
    }
    expect(TOK_RBRACE, "'}'");
    return blk;
}

/* Expect '>' but handle '>>' (TOK_SHR) gracefully for nested generics.
 * In `Field<18446744069414584321>>`, the lexer produces >> as TOK_SHR.
 * When parse_type expects the inner >, we convert >> to > in place
 * (consuming the first >) and leave the second > for the outer parser. */
static void expect_gt_generic(void) {
    if (check(TOK_GT)) {
        advance();
    } else if (check(TOK_SHR)) {
        /* >> seen — consume first >, leave second as > */
        g_tokens[g_tok_pos].kind = TOK_GT;
    } else {
        Token *t = peek();
        error(t->line, t->col, "expected '>', got '%s'", t->text);
        advance();
    }
}

/* Parse a type annotation: F, u8, bool, Field<251>, [u8; 10] */
static void parse_type(char *out) {
    Token *t = peek();
    /* *T — pointer type */
    if (t->kind == TOK_STAR) {
        advance();
        char inner[MAX_IDENT];
        parse_type(inner);
        snprintf(out, MAX_IDENT, "*%s", inner);
        return;
    }
    /* **T — double pointer (lexer produces TOK_STARSTAR for "**") */
    if (t->kind == TOK_STARSTAR) {
        advance();
        char inner[MAX_IDENT];
        parse_type(inner);
        snprintf(out, MAX_IDENT, "**%s", inner);
        return;
    }
    if (t->kind == TOK_TY_FIELD) {
        advance();
        /* Bare 'Field' (no <p>) — the runtime Field carrier value type */
        if (!check(TOK_LT)) {
            strcpy(out, "Field");
            return;
        }
        /* Field<251> */
        expect(TOK_LT, "'<'");
        Token *p = expect(TOK_INT_LIT, "prime");
        expect_gt_generic();  /* handles >> for nested generics */
        snprintf(out, MAX_IDENT, "Field<%llu>", (unsigned long long)p->int_val);
    } else if (t->kind == TOK_TY_POLY) {
        /* Poly<F, d> */
        advance();
        expect(TOK_LT, "'<'");
        char field_name[MAX_IDENT];
        parse_type(field_name);
        expect(TOK_COMMA, "','");
        Token *deg = expect(TOK_INT_LIT, "polynomial degree");
        expect_gt_generic();  /* handles >> for nested generics */
        snprintf(out, MAX_IDENT, "Poly<%s;%llu>", field_name, (unsigned long long)deg->int_val);
    } else if (t->kind == TOK_TY_REGISTER) {
        /* Register<F, d> */
        advance();
        expect(TOK_LT, "'<'");
        char field_name[MAX_IDENT];
        parse_type(field_name);
        expect(TOK_COMMA, "','");
        Token *deg = expect(TOK_INT_LIT, "register degree");
        expect_gt_generic();
        snprintf(out, MAX_IDENT, "Register<%s;%llu>", field_name, (unsigned long long)deg->int_val);
    } else if (t->kind == TOK_LBRACKET) {
        /* [elem_type; size] */
        advance();
        char elem[MAX_IDENT];
        parse_type(elem);
        expect(TOK_SEMI, "';'");
        Token *sz = expect(TOK_INT_LIT, "array size");
        expect(TOK_RBRACKET, "']'");
        snprintf(out, MAX_IDENT, "[%s;%llu]", elem, (unsigned long long)sz->int_val);
    } else if (t->kind >= TOK_TY_U8 && t->kind <= TOK_TY_BOOL) {
        advance();
        strcpy(out, t->text);
    } else if (t->kind == TOK_IDENT) {
        advance();
        strcpy(out, t->text);
    } else {
        error(t->line, t->col, "expected type, got '%s'", t->text);
        advance();
        strcpy(out, "void");
    }
}

/* Compound-assignment desugar (plan-syntax-modernization Phase 1). The caller
 * has an lvalue and is positioned on `=` or `op=`. Consumes the operator and
 * the rhs; returns the value expression: for `op=` it is the binary
 * `lvalue op rhs` (reusing the lvalue node as the read operand — so an index /
 * member lvalue is evaluated twice, by design); for plain `=` it is just rhs.
 * Produces AST byte-identical to the hand-written `lvalue = lvalue op rhs`. */
static ASTNode *parse_assign_value(ASTNode *lvalue) {
    int cop = -1;
    if (check(TOK_OP_ASSIGN)) cop = (int)peek()->int_val;
    advance();  /* consume '=' or the compound 'op=' */
    ASTNode *rhs = parse_expr();
    if (cop >= 0) {
        ASTNode *bin = ast_alloc();
        bin->kind = AST_BINARY;
        bin->line = lvalue->line; bin->col = lvalue->col;
        bin->binary.op = (OpKind)cop;
        bin->binary.lhs = lvalue;
        bin->binary.rhs = rhs;
        return bin;
    }
    return rhs;
}

static ASTNode *parse_stmt(void) {
    Token *t = peek();

    /* let [mut] name: type = expr;   (`var` is an alias of `let mut`) */
    if (t->kind == TOK_KW_LET || t->kind == TOK_KW_VAR) {
        bool is_var = (t->kind == TOK_KW_VAR);
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_LET;
        n->line = t->line; n->col = t->col;
        n->let_stmt.is_mut = is_var;

        if (check(TOK_KW_MUT)) {
            advance();
            n->let_stmt.is_mut = true;
        }

        Token *name = expect(TOK_IDENT, "variable name");
        strcpy(n->let_stmt.name, name->text);

        if (match(TOK_COLON)) {
            parse_type(n->let_stmt.type_name);
        } else {
            strcpy(n->let_stmt.type_name, "");  /* infer */
        }

        expect(TOK_ASSIGN, "'='");
        n->let_stmt.init = parse_expr();
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* break; */
    if (t->kind == TOK_KW_BREAK) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_BREAK;
        n->line = t->line; n->col = t->col;
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* continue; */
    if (t->kind == TOK_KW_CONTINUE) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_CONTINUE;
        n->line = t->line; n->col = t->col;
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* return expr; */
    if (t->kind == TOK_KW_RETURN) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_RETURN;
        n->line = t->line; n->col = t->col;
        if (!check(TOK_SEMI)) {
            n->ret.value = parse_expr();
        }
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* for i in start..end { body } */
    if (t->kind == TOK_KW_FOR) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_FOR;
        n->line = t->line; n->col = t->col;

        Token *var = expect(TOK_IDENT, "loop variable");
        strcpy(n->for_stmt.name, var->text);
        expect(TOK_KW_IN, "'in'");
        n->for_stmt.start = parse_expr();
        expect(TOK_DOTDOT, "'..'");
        n->for_stmt.end = parse_expr();
        n->for_stmt.body = parse_block();
        return n;
    }

    /* match expr { Enum::Variant(bindings) => { body }, ... } */
    if (t->kind == TOK_KW_MATCH) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_MATCH;
        n->line = t->line; n->col = t->col;
        n->match_expr.scrutinee = parse_expr();
        n->match_expr.arms_start = g_nmatch_arms;
        n->match_expr.narms = 0;

        /* #31 mirror: a nested match inside an arm body appends ITS arms to
           g_match_arms between this match's arms, so the outer window
           arms_start..+narms would read nested arm records as its own.
           Record each outer arm's index and compact to a contiguous run
           after the closing brace (no-nesting case: pool layout unchanged). */
        int *arm_indices = malloc(sizeof(int) * MAX_MATCH_ARMS);
        int nrecorded = 0;

        expect(TOK_LBRACE, "'{'");
        while (!check(TOK_RBRACE)) {
            if (g_nmatch_arms >= MAX_MATCH_ARMS) {
                error(t->line, t->col, "too many match arms (max %d)", MAX_MATCH_ARMS);
                break;
            }
            MatchArmInfo *arm = &g_match_arms[g_nmatch_arms++];
            arm_indices[nrecorded++] = (int)(arm - g_match_arms);
            arm->nbindings = 0;
            arm->is_wildcard = false;
            arm->is_int_lit = false;
            arm->int_val = 0;
            arm->enum_name[0] = 0;
            arm->variant_name[0] = 0;

            Token *pat = peek();
            if (pat->kind == TOK_IDENT && !strcmp(pat->text, "_")) {
                /* Wildcard arm: _ => { body } */
                advance();
                arm->is_wildcard = true;
            } else if (pat->kind == TOK_INT_LIT) {
                /* Integer literal arm: 42 => { body } */
                advance();
                arm->is_int_lit = true;
                arm->int_val = (int64_t)pat->int_val;
            } else if (pat->kind == TOK_MINUS && g_tokens[g_tok_pos + 1].kind == TOK_INT_LIT) {
                /* Negative integer literal arm: -1 => { body } */
                advance(); /* consume - */
                Token *num = advance(); /* consume number */
                arm->is_int_lit = true;
                arm->int_val = -(int64_t)num->int_val;
            } else {
                /* Enum::Variant(binding, ...) => { body } */
                Token *ename = expect(TOK_IDENT, "enum name or '_'");
                strcpy(arm->enum_name, ename->text);
                expect(TOK_COLONCOLON, "'::'");
                Token *vname = expect(TOK_IDENT, "variant name");
                strcpy(arm->variant_name, vname->text);

                /* Optional destructuring: (binding, binding, ...) */
                if (match(TOK_LPAREN)) {
                    while (!check(TOK_RPAREN)) {
                        if (arm->nbindings > 0) expect(TOK_COMMA, "','");
                        if (arm->nbindings >= MAX_VARIANT_FIELDS) {
                            error(peek()->line, peek()->col, "too many bindings (max %d)", MAX_VARIANT_FIELDS);
                            break;
                        }
                        Token *bname = expect(TOK_IDENT, "binding name");
                        strcpy(arm->bindings[arm->nbindings++], bname->text);
                    }
                    expect(TOK_RPAREN, "')'");
                }
            }

            expect(TOK_FAT_ARROW, "'=>'");
            arm->body = parse_block();
            n->match_expr.narms++;
            match(TOK_COMMA); /* optional trailing comma between arms */
        }
        expect(TOK_RBRACE, "'}'");
        /* Compact if a nested match interleaved arms into the pool. */
        if (g_nmatch_arms != n->match_expr.arms_start + n->match_expr.narms) {
            int cbase = g_nmatch_arms;
            for (int a = 0; a < n->match_expr.narms; a++) {
                if (g_nmatch_arms >= MAX_MATCH_ARMS) {
                    error(t->line, t->col, "too many match arms (max %d)", MAX_MATCH_ARMS);
                    break;
                }
                g_match_arms[g_nmatch_arms++] = g_match_arms[arm_indices[a]];
            }
            n->match_expr.arms_start = cbase;
        }
        free(arm_indices);
        return n;
    }

    /* while cond [fuel(n)] { body } — native loop or ZK circuit unrolling */
    if (t->kind == TOK_KW_WHILE) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_WHILE;
        n->line = t->line; n->col = t->col;
        n->while_stmt.cond = parse_expr();
        n->while_stmt.fuel = 0;
        /* Check for optional fuel(n) after the condition */
        {
            Token *ft = peek();
            if (ft->kind == TOK_IDENT && !strcmp(ft->text, "fuel")) {
                advance();
                expect(TOK_LPAREN, "'(' after fuel");
                ASTNode *fuel_expr = parse_expr();
                if (fuel_expr->kind != AST_LIT_INT) {
                    error(ft->line, ft->col,
                          "fuel() requires a constant integer");
                } else {
                    n->while_stmt.fuel = fuel_expr->lit_int.value;
                }
                expect(TOK_RPAREN, "')' after fuel value");
            }
        }
        n->while_stmt.body = parse_block();
        return n;
    }

    /* unsafe { block } — parsed but not enforced (Option C) */
    if (t->kind == TOK_KW_UNSAFE) {
        advance();
        return parse_block(); /* transparent passthrough */
    }

    /* if expr { } [else { }] — as statement */
    if (t->kind == TOK_KW_IF) {
        ASTNode *if_expr = parse_unary(); /* parses the if expression */
        ASTNode *n = ast_alloc();
        n->kind = AST_EXPR_STMT;
        n->line = t->line; n->col = t->col;
        n->expr_stmt.expr = if_expr;
        match(TOK_SEMI); /* optional semicolon after if-stmt */
        return n;
    }

    /* Expression statement or assignment */
    ASTNode *expr = parse_expr();

    /* Check for assignment: ident = expr; or arr[i] = expr; (plain or compound
     * `op=` — parse_assign_value consumes the operator and desugars compounds). */
    if (expr->kind == AST_IDENT && (check(TOK_ASSIGN) || check(TOK_OP_ASSIGN))) {
        ASTNode *n = ast_alloc();
        n->kind = AST_ASSIGN;
        n->line = expr->line; n->col = expr->col;
        strcpy(n->assign.name, expr->ident.name);
        n->assign.value = parse_assign_value(expr);
        expect(TOK_SEMI, "';'");
        return n;
    }
    /* Member assignment: obj.field = expr; (supports chained: a.b.c = val) */
    if (expr->kind == AST_MEMBER_ACCESS && (check(TOK_ASSIGN) || check(TOK_OP_ASSIGN))) {
        ASTNode *n = ast_alloc();
        n->kind = AST_MEMBER_ASSIGN;
        n->line = expr->line; n->col = expr->col;
        strcpy(n->member_assign.obj_name, expr->member_access.obj_name);
        strcpy(n->member_assign.member, expr->member_access.member);
        n->member_assign.obj_expr = expr->member_access.obj_expr;
        n->member_assign.value = parse_assign_value(expr);
        expect(TOK_SEMI, "';'");
        return n;
    }
    if (expr->kind == AST_INDEX && (check(TOK_ASSIGN) || check(TOK_OP_ASSIGN))) {
        ASTNode *val_expr = parse_assign_value(expr);
        ASTNode *n = ast_alloc();
        n->kind = AST_INDEX_ASSIGN;
        n->line = expr->line; n->col = expr->col;

        if (expr->index_expr.obj_expr) {
            /* Expression-based index assignment: b.data[0] = val */
            n->call.name[0] = 0; /* no direct name — use obj_expr in args[2] */
            n->call.args = malloc(sizeof(ASTNode*) * 3);
            n->call.args[0] = expr->index_expr.index;     /* index expression */
            n->call.args[1] = val_expr;                    /* value expression */
            n->call.args[2] = expr->index_expr.obj_expr;  /* base expression */
            n->call.nargs = 3;
        } else {
            /* Name-based index assignment: arr[i] = val */
            strcpy(n->call.name, expr->index_expr.name);
            n->call.args = malloc(sizeof(ASTNode*) * 2);
            n->call.args[0] = expr->index_expr.index;  /* index expression */
            n->call.args[1] = val_expr;                 /* value expression */
            n->call.nargs = 2;
        }
        expect(TOK_SEMI, "';'");
        return n;
    }

    ASTNode *n = ast_alloc();
    n->kind = AST_EXPR_STMT;
    n->line = expr->line; n->col = expr->col;
    n->expr_stmt.expr = expr;
    expect(TOK_SEMI, "';'");
    return n;
}

/* Parse attribute: #[allow(field_ord)] */
static void parse_attribute(char *out) {
    /* We're already past #[ */
    int depth = 1;
    (void)g_tok_pos; /* start position available if needed for diagnostics */
    out[0] = 0;
    while (!check(TOK_EOF) && depth > 0) {
        Token *t = peek();
        if (t->kind == TOK_LBRACKET) depth++;
        if (t->kind == TOK_RBRACKET) {
            depth--;
            if (depth == 0) { advance(); break; }
        }
        /* Accumulate attribute text */
        if (out[0]) strcat(out, " ");
        strcat(out, t->text);
        advance();
    }
}

/* Top-level parsing */
static ASTNode *parse_top_level(void) {
    Token *t = peek();

    /* field F = Field<251>; */
    if (t->kind == TOK_KW_FIELD) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_FIELD_DECL;
        n->line = t->line; n->col = t->col;
        n->field_decl.field_kind = FIELD_KIND_PRIME;

        Token *name = expect(TOK_IDENT, "field alias");
        if (!strcmp(name->text, "dyn"))
            error(name->line, name->col,
                  "'dyn' is a reserved type name (runtime field instantiation)");
        strcpy(n->field_decl.name, name->text);
        expect(TOK_ASSIGN, "'='");
        expect(TOK_TY_FIELD, "'Field'");
        expect(TOK_LT, "'<'");
        Token *prime = expect(TOK_INT_LIT, "prime");
        n->field_decl.prime = prime->int_val;
        expect(TOK_GT, "'>'");
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* binfield G = BinField<8, 0x11B>; */
    if (t->kind == TOK_KW_BINFIELD) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_FIELD_DECL;
        n->line = t->line; n->col = t->col;
        n->field_decl.field_kind = FIELD_KIND_BINARY;

        Token *name = expect(TOK_IDENT, "binfield alias");
        if (!strcmp(name->text, "dyn"))
            error(name->line, name->col,
                  "'dyn' is a reserved type name (runtime field instantiation)");
        strcpy(n->field_decl.name, name->text);
        expect(TOK_ASSIGN, "'='");
        expect(TOK_TY_BINFIELD, "'BinField'");
        expect(TOK_LT, "'<'");
        Token *deg = expect(TOK_INT_LIT, "field degree");
        n->field_decl.degree = (int)deg->int_val;
        expect(TOK_COMMA, "','");
        Token *poly = expect(TOK_INT_LIT, "irreducible polynomial");
        n->field_decl.poly = poly->int_val;
        n->field_decl.prime = 2;  /* characteristic 2 */
        expect(TOK_GT, "'>'");
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* type NAME = <FieldTy>;  — field-alias unification (== field/binfield/
     * extfield). Same AST_FIELD_DECL; the kind is derived from the RHS type
     * keyword. General type aliasing (primitives/structs) is a future feature. */
    if (t->kind == TOK_KW_TYPE) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_FIELD_DECL;
        n->line = t->line; n->col = t->col;

        Token *name = expect(TOK_IDENT, "type alias");
        if (!strcmp(name->text, "dyn"))
            error(name->line, name->col,
                  "'dyn' is a reserved type name (runtime field instantiation)");
        strcpy(n->field_decl.name, name->text);
        expect(TOK_ASSIGN, "'='");

        if (check(TOK_TY_FIELD)) {
            n->field_decl.field_kind = FIELD_KIND_PRIME;
            advance();
            expect(TOK_LT, "'<'");
            Token *prime = expect(TOK_INT_LIT, "prime");
            n->field_decl.prime = prime->int_val;
            expect(TOK_GT, "'>'");
        } else if (check(TOK_TY_BINFIELD)) {
            n->field_decl.field_kind = FIELD_KIND_BINARY;
            advance();
            expect(TOK_LT, "'<'");
            Token *deg = expect(TOK_INT_LIT, "field degree");
            n->field_decl.degree = (int)deg->int_val;
            expect(TOK_COMMA, "','");
            Token *poly = expect(TOK_INT_LIT, "irreducible polynomial");
            n->field_decl.poly = poly->int_val;
            n->field_decl.prime = 2;  /* characteristic 2 */
            expect(TOK_GT, "'>'");
        } else if (check(TOK_TY_EXTFIELD)) {
            n->field_decl.field_kind = FIELD_KIND_EXTENSION;
            advance();
            expect(TOK_LT, "'<'");
            expect(TOK_TY_FIELD, "'Field'");
            expect(TOK_LT, "'<'");
            Token *prime = expect(TOK_INT_LIT, "base field prime");
            n->field_decl.prime = prime->int_val;
            expect(TOK_GT, "'>'");
            expect(TOK_COMMA, "','");
            Token *deg = expect(TOK_INT_LIT, "extension degree");
            n->field_decl.degree = (int)deg->int_val;
            expect(TOK_GT, "'>'");
        } else {
            Token *bad = peek();
            error(bad->line, bad->col,
                  "'type' alias RHS must be a field type (Field/BinField/ExtField); general type aliasing is a separate future feature");
        }
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* extfield E = ExtField<Field<p>, 2>; */
    if (t->kind == TOK_KW_EXTFIELD) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_FIELD_DECL;
        n->line = t->line; n->col = t->col;
        n->field_decl.field_kind = FIELD_KIND_EXTENSION;

        Token *name = expect(TOK_IDENT, "extfield alias");
        if (!strcmp(name->text, "dyn"))
            error(name->line, name->col,
                  "'dyn' is a reserved type name (runtime field instantiation)");
        strcpy(n->field_decl.name, name->text);
        expect(TOK_ASSIGN, "'='");
        expect(TOK_TY_EXTFIELD, "'ExtField'");
        expect(TOK_LT, "'<'");
        expect(TOK_TY_FIELD, "'Field'");
        expect(TOK_LT, "'<'");
        Token *prime = expect(TOK_INT_LIT, "base field prime");
        n->field_decl.prime = prime->int_val;
        expect(TOK_GT, "'>'");
        expect(TOK_COMMA, "','");
        Token *deg = expect(TOK_INT_LIT, "extension degree");
        n->field_decl.degree = (int)deg->int_val;
        expect(TOK_GT, "'>'");
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* instantiate ntt_forward<Field<18446744069414584321>>;
     * Explicit monomorphization: emits the function with external linkage. */
    if (t->kind == TOK_KW_INSTANTIATE) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_INSTANTIATE;
        n->line = t->line; n->col = t->col;

        Token *fname = expect(TOK_IDENT, "function name");
        strcpy(n->fn_decl.name, fname->text);
        expect(TOK_LT, "'<'");

        /* Parse concrete type arguments.  parse_type uses expect_gt_generic()
         * to handle >> when Field<p> is the last type arg before outer >. */
        n->fn_decl.ngen = 0;
        while (!check(TOK_GT)) {
            if (n->fn_decl.ngen > 0) expect(TOK_COMMA, "','");
            char type_buf[MAX_IDENT] = {0};
            parse_type(type_buf);
            strcpy(n->fn_decl.gen_names[n->fn_decl.ngen], type_buf);
            n->fn_decl.ngen++;
        }
        expect(TOK_GT, "'>'");
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* struct Name { field: Type, field: Type, } */
    if (t->kind == TOK_KW_STRUCT) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_STRUCT_DECL;
        n->line = t->line; n->col = t->col;

        Token *name = expect(TOK_IDENT, "struct name");
        if (!strcmp(name->text, "dyn") || !strcmp(name->text, "Field"))
            error(name->line, name->col,
                  "'%s' is a reserved type name", name->text);
        strcpy(n->struct_decl.name, name->text);

        /* Pre-register struct name so find_struct() works during parsing
         * of struct literals in subsequent expressions. Full type resolution
         * happens in codegen_program's first pass. */
        if (!find_struct(name->text) && g_nstructs < MAX_FIELDS) {
            strcpy(g_structs[g_nstructs].name, name->text);
            g_structs[g_nstructs].nfields = 0;
            snprintf(g_structs[g_nstructs].ir_name, MAX_IDENT, "%%%s", name->text);
            g_nstructs++;
        }

        expect(TOK_LBRACE, "'{'");

        n->struct_decl.nfields = 0;
        while (!check(TOK_RBRACE)) {
            if (n->struct_decl.nfields >= MAX_PARAMS) {
                error(peek()->line, peek()->col, "too many struct fields (max %d)", MAX_PARAMS);
                break;
            }
            int i = n->struct_decl.nfields;
            Token *fname = expect(TOK_IDENT, "field name");
            strcpy(n->struct_decl.field_names[i], fname->text);
            expect(TOK_COLON, "':'");
            parse_type(n->struct_decl.field_types[i]);
            n->struct_decl.nfields++;
            if (!check(TOK_RBRACE)) expect(TOK_COMMA, "','");
        }
        expect(TOK_RBRACE, "'}'");
        return n;
    }

    /* enum Name { Variant(Type, Type), Variant, } */
    if (t->kind == TOK_KW_ENUM) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_ENUM_DECL;
        n->line = t->line; n->col = t->col;

        Token *name = expect(TOK_IDENT, "enum name");
        strcpy(n->enum_decl.name, name->text);

        /* Pre-register enum in g_enums so construction works during parsing */
        EnumInfo *ei = find_enum(name->text);
        if (!ei && g_nenums < MAX_FIELDS) {
            ei = &g_enums[g_nenums++];
            strcpy(ei->name, name->text);
            snprintf(ei->ir_name, MAX_IDENT, "%%%s", name->text);
            ei->nvariants = 0;
        }

        expect(TOK_LBRACE, "'{'");
        while (!check(TOK_RBRACE)) {
            Token *vname = expect(TOK_IDENT, "variant name");
            if (ei && ei->nvariants < 64) {
                EnumVariant *v = &ei->variants[ei->nvariants];
                strcpy(v->name, vname->text);
                v->tag = ei->nvariants;
                v->nfields = 0;

                /* Optional tuple payload: (Type, Type, ...) */
                if (match(TOK_LPAREN)) {
                    while (!check(TOK_RPAREN)) {
                        if (v->nfields > 0) expect(TOK_COMMA, "','");
                        if (v->nfields >= MAX_VARIANT_FIELDS) {
                            error(peek()->line, peek()->col, "too many variant fields (max %d)", MAX_VARIANT_FIELDS);
                            break;
                        }
                        parse_type(v->field_types[v->nfields]);
                        v->nfields++;
                    }
                    expect(TOK_RPAREN, "')'");
                }
                ei->nvariants++;
            }
            if (!check(TOK_RBRACE)) expect(TOK_COMMA, "','");
        }
        expect(TOK_RBRACE, "'}'");
        return n;
    }

    /* Parse optional attributes before fn */
    char attrs[MAX_IDENT] = {0};
    if (t->kind == TOK_HASH_LBRACKET) {
        advance();
        parse_attribute(attrs);
        t = peek();
    }

    /* extern "C" fn name(params) [-> type]; */
    if (t->kind == TOK_KW_EXTERN) {
        advance();
        /* Skip the "C" language specifier (string literal or bare identifier) */
        Token *lang = peek();
        if ((lang->kind == TOK_STR_LIT && !strcmp(lang->text, "C")) ||
            (lang->kind == TOK_IDENT && !strcmp(lang->text, "C"))) {
            advance();
        }
        expect(TOK_KW_FN, "'fn'");

        ASTNode *n = ast_alloc();
        n->kind = AST_EXTERN_FN;
        n->line = t->line; n->col = t->col;

        Token *name = expect(TOK_IDENT, "function name");
        strcpy(n->fn_decl.name, name->text);

        expect(TOK_LPAREN, "'('");
        n->fn_decl.nparams = 0;
        if (!check(TOK_RPAREN)) {
            do {
                if (n->fn_decl.nparams >= MAX_PARAMS) {
                    error(peek()->line, peek()->col, "too many parameters (max %d)", MAX_PARAMS);
                    break;
                }
                int i = n->fn_decl.nparams;
                Token *pname = expect(TOK_IDENT, "parameter name");
                strcpy(n->fn_decl.params[i], pname->text);
                expect(TOK_COLON, "':'");
                parse_type(n->fn_decl.param_types[i]);
                n->fn_decl.nparams++;
            } while (match(TOK_COMMA));
        }
        expect(TOK_RPAREN, "')'");

        /* Return type */
        if (match(TOK_ARROW)) {
            parse_type(n->fn_decl.ret_type);
        } else {
            strcpy(n->fn_decl.ret_type, "void");
        }
        n->fn_decl.body = NULL; /* no body */
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* let [mut] name: Type = expr;  — global variable  (`var` == `let mut`) */
    if (t->kind == TOK_KW_LET || t->kind == TOK_KW_VAR) {
        bool is_var = (t->kind == TOK_KW_VAR);
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_LET;
        n->line = t->line; n->col = t->col;
        n->let_stmt.is_mut = is_var;
        n->let_stmt.is_global = true;

        if (check(TOK_KW_MUT)) {
            advance();
            n->let_stmt.is_mut = true;
        }

        Token *name = expect(TOK_IDENT, "variable name");
        strcpy(n->let_stmt.name, name->text);

        expect(TOK_COLON, "':'");
        parse_type(n->let_stmt.type_name);
        expect(TOK_ASSIGN, "'='");
        n->let_stmt.init = parse_expr();
        expect(TOK_SEMI, "';'");
        return n;
    }

    /* fn name(params) [-> type] { body } */
    /* fn name<F: Field>(params) [-> type] { body } — generic function */
    if (t->kind == TOK_KW_FN) {
        advance();
        ASTNode *n = ast_alloc();
        n->kind = AST_FN_DECL;
        n->line = t->line; n->col = t->col;
        strcpy(n->fn_decl.attrs, attrs);

        Token *name = expect(TOK_IDENT, "function name");
        strcpy(n->fn_decl.name, name->text);

        /* Parse generic type parameters: <F: Field, G: Field> */
        n->fn_decl.ngen = 0;
        if (check(TOK_LT)) {
            advance();  /* consume '<' */
            do {
                int gi = n->fn_decl.ngen;
                Token *gname = expect(TOK_IDENT, "generic parameter name");
                strcpy(n->fn_decl.gen_names[gi], gname->text);
                expect(TOK_COLON, "':'");
                /* Trait bound: accept both TOK_IDENT and TOK_TY_FIELD/TOK_TY_BINFIELD
                 * since "Field" is lexed as a type keyword, not an identifier */
                Token *bound = peek();
                if (bound->kind == TOK_IDENT || bound->kind == TOK_TY_FIELD ||
                    bound->kind == TOK_TY_BINFIELD) {
                    strcpy(n->fn_decl.gen_bounds[gi], bound->text);
                    advance();
                } else {
                    error(bound->line, bound->col, "expected trait bound, got '%s'", bound->text);
                    strcpy(n->fn_decl.gen_bounds[gi], "Field");
                }
                n->fn_decl.ngen++;
            } while (match(TOK_COMMA));
            expect(TOK_GT, "'>'");
        }

        expect(TOK_LPAREN, "'('");
        n->fn_decl.nparams = 0;
        if (!check(TOK_RPAREN)) {
            do {
                if (n->fn_decl.nparams >= MAX_PARAMS) {
                    error(peek()->line, peek()->col, "too many parameters (max %d)", MAX_PARAMS);
                    break;
                }
                int i = n->fn_decl.nparams;
                Token *pname = expect(TOK_IDENT, "parameter name");
                strcpy(n->fn_decl.params[i], pname->text);
                expect(TOK_COLON, "':'");
                parse_type(n->fn_decl.param_types[i]);
                n->fn_decl.nparams++;
            } while (match(TOK_COMMA));
        }
        expect(TOK_RPAREN, "')'");

        /* Return type */
        if (match(TOK_ARROW)) {
            parse_type(n->fn_decl.ret_type);
        } else {
            strcpy(n->fn_decl.ret_type, "void");
        }

        n->fn_decl.body = parse_block();
        return n;
    }

    error(t->line, t->col, "expected top-level declaration, got '%s'", t->text);
    advance();
    return NULL;
}

static ASTNode *parse_program(void) {
    ASTNode *prog = ast_alloc();
    prog->kind = AST_PROGRAM;
    prog->program.decls = malloc(sizeof(ASTNode*) * MAX_DECLS);
    prog->program.ndecls = 0;

    while (!check(TOK_EOF)) {
        ASTNode *d = parse_top_level();
        if (d) {
            if (prog->program.ndecls >= MAX_DECLS)
                cap_overflow("top-level decl array (program.decls)", MAX_DECLS);
            prog->program.decls[prog->program.ndecls++] = d;
        }
    }
    return prog;
}

/* ============================================================
 * Type resolution & field registry
 * ============================================================ */

static FieldInfo *find_field(const char *name) {
    for (int i = 0; i < g_nfields; i++) {
        if (!strcmp(g_fields[i].alias, name)) return &g_fields[i];
    }
    return NULL;
}

static FieldInfo *find_field_by_prime(uint64_t p) {
    for (int i = 0; i < g_nfields; i++) {
        if (g_fields[i].prime == p) return &g_fields[i];
    }
    return NULL;
}

static void register_field(const char *alias, uint64_t prime, int line, int col) {
    if (!is_prime(prime)) {
        error(line, col, "%llu is not prime", (unsigned long long)prime);
        return;
    }
    if (g_nfields >= MAX_FIELDS) {
        error(line, col, "too many field declarations");
        return;
    }
    FieldInfo *f = &g_fields[g_nfields++];
    strcpy(f->alias, alias);
    f->prime = prime;
    f->field_kind = FIELD_KIND_PRIME;
    f->poly = 0;
    f->degree = 0;
    f->base_field_idx = -1;
    f->nonresidue = 0;
    f->ext_elem_ir[0] = 0;

    if (prime <= 255) {
        strcpy(f->elem_ir, "i8");
        strcpy(f->wide_ir, "i16");
        f->elem_bits = 8;
    } else if (prime <= 65535) {
        strcpy(f->elem_ir, "i16");
        strcpy(f->wide_ir, "i32");
        f->elem_bits = 16;
    } else if (prime <= 4294967295ULL) {
        strcpy(f->elem_ir, "i32");
        strcpy(f->wide_ir, "i64");
        f->elem_bits = 32;
    } else {
        strcpy(f->elem_ir, "i64");
        strcpy(f->wide_ir, "i128");
        f->elem_bits = 64;
    }

    /* Compute NTT root chain if the prime is NTT-friendly */
    compute_ntt_info(f);
}

static void register_binfield(const char *alias, int degree, uint64_t poly, int line, int col) {
    if (g_nfields >= MAX_FIELDS) {
        error(line, col, "too many field declarations");
        return;
    }
    if (degree != 8) {
        error(line, col, "only BinField<8, poly> (GF(2^8)) is currently supported");
        return;
    }
    FieldInfo *f = &g_fields[g_nfields++];
    strcpy(f->alias, alias);
    f->prime = (uint64_t)1 << degree;  /* field order: 2^k (e.g., 256 for k=8) */
    f->poly = poly;
    f->degree = degree;
    f->field_kind = FIELD_KIND_BINARY;
    f->base_field_idx = -1;
    f->nonresidue = 0;
    f->ext_elem_ir[0] = 0;
    strcpy(f->elem_ir, "i8");
    strcpy(f->wide_ir, "i16");  /* not used for binary, but set for consistency */
    f->elem_bits = 8;
}

/* Find the quadratic non-residue for a prime p.
 * Scan nr = 2, 3, 5, 6, 7, ... until Legendre symbol is -1:
 * nr^((p-1)/2) = p-1 mod p.
 * Returns 0 on failure (shouldn't happen for p > 2). */
static uint64_t find_nonresidue(uint64_t p) {
    uint64_t exp = (p - 1) / 2;
    for (uint64_t nr = 2; nr < p; nr++) {
        if (mod_pow64(nr, exp, p) == p - 1) return nr;
    }
    return 0;  /* unreachable for p > 2 */
}

static void register_extfield(const char *alias, uint64_t base_prime, int ext_degree,
                               int line, int col) {
    if (ext_degree != 2) {
        error(line, col, "only quadratic extensions (degree 2) are supported");
        return;
    }
    /* Find the base field */
    int base_idx = -1;
    for (int i = 0; i < g_nfields; i++) {
        if (g_fields[i].prime == base_prime && g_fields[i].field_kind == FIELD_KIND_PRIME) {
            base_idx = i;
            break;
        }
    }
    if (base_idx < 0) {
        error(line, col, "base field Field<%llu> not declared before ExtField",
              (unsigned long long)base_prime);
        return;
    }
    if (g_nfields >= MAX_FIELDS) {
        error(line, col, "too many field declarations");
        return;
    }
    FieldInfo *base = &g_fields[base_idx];
    FieldInfo *f = &g_fields[g_nfields++];
    strcpy(f->alias, alias);
    f->prime = base_prime;
    f->poly = 0;
    f->degree = ext_degree;
    f->field_kind = FIELD_KIND_EXTENSION;
    f->base_field_idx = base_idx;
    /* Compute quadratic non-residue at compile time */
    f->nonresidue = find_nonresidue(base_prime);
    /* Inherit base field IR types */
    strcpy(f->elem_ir, base->elem_ir);
    strcpy(f->wide_ir, base->wide_ir);
    f->elem_bits = base->elem_bits;
    /* Extension element IR type: {base_ir, base_ir} */
    snprintf(f->ext_elem_ir, sizeof(f->ext_elem_ir), "{%s, %s}",
             base->elem_ir, base->elem_ir);
}

static Type resolve_type(const char *name) {
    Type t = {0};

    /* Generic substitution: if we're inside monomorphized codegen and
     * the name matches a generic parameter, substitute the concrete type */
    for (int gi = 0; gi < g_gen_nsub; gi++) {
        if (!strcmp(name, g_gen_sub_name[gi])) {
            return resolve_type(g_gen_sub_type[gi]);  /* recurse with concrete type */
        }
    }

    if (!strcmp(name, "void"))  { t.kind = TYPE_VOID; return t; }
    if (!strcmp(name, "bool"))  { t.kind = TYPE_BOOL; return t; }
    if (!strcmp(name, "u8"))    { t.kind = TYPE_U8; return t; }
    if (!strcmp(name, "u16"))   { t.kind = TYPE_U16; return t; }
    if (!strcmp(name, "u32"))   { t.kind = TYPE_U32; return t; }
    if (!strcmp(name, "u64"))   { t.kind = TYPE_U64; return t; }
    if (!strcmp(name, "i8"))    { t.kind = TYPE_I8; return t; }
    if (!strcmp(name, "i16"))   { t.kind = TYPE_I16; return t; }
    if (!strcmp(name, "i32"))   { t.kind = TYPE_I32; return t; }
    if (!strcmp(name, "i64"))   { t.kind = TYPE_I64; return t; }
    if (!strcmp(name, "usize")) { t.kind = TYPE_USIZE; return t; }

    /* dyn — element of a runtime-determined prime field (i64-backed).
     * Only meaningful inside a dyn-instantiated generic body, where the
     * generic substitution maps F -> "dyn". */
    if (!strcmp(name, "dyn")) { t.kind = TYPE_DYNFIELD; return t; }

    /* Field (bare, no <p>) — the runtime Field carrier value.
     * Constructed by the field(p) builtin; passed as ptr to %__Field. */
    if (!strcmp(name, "Field")) { t.kind = TYPE_FIELD_VALUE; return t; }

    /* Field alias? (matches field, binfield, and extfield aliases) */
    FieldInfo *fi = find_field(name);
    if (fi) {
        t.kind = TYPE_FIELD;
        t.field_prime = fi->prime;
        t.field_idx = (int)(fi - g_fields);
        return t;
    }

    /* ExtField<Field<p>,2> direct */
    if (!strncmp(name, "ExtField<", 9)) {
        /* Parse: ExtField<Field<p>,2> */
        const char *rest = name + 9;
        if (!strncmp(rest, "Field<", 6)) {
            uint64_t base_p = strtoull(rest + 6, NULL, 10);
            /* Find the extension field entry */
            for (int i = 0; i < g_nfields; i++) {
                if (g_fields[i].field_kind == FIELD_KIND_EXTENSION &&
                    g_fields[i].prime == base_p) {
                    t.kind = TYPE_FIELD;
                    t.field_prime = base_p;
                    t.field_idx = i;
                    return t;
                }
            }
        }
        t.kind = TYPE_VOID;
        return t;
    }

    /* Field<p> direct */
    if (!strncmp(name, "Field<", 6)) {
        uint64_t p = strtoull(name + 6, NULL, 10);
        t.kind = TYPE_FIELD;
        t.field_prime = p;
        /* Find field_idx for this prime field */
        for (int i = 0; i < g_nfields; i++) {
            if (g_fields[i].prime == p && g_fields[i].field_kind == FIELD_KIND_PRIME) {
                t.field_idx = i;
                break;
            }
        }
        return t;
    }

    /* BinField<d,poly> direct — look up by poly value in field registry.
     * Binary fields store prime = 2^degree (e.g. 256 for GF(2^8)). */
    if (!strncmp(name, "BinField<", 9)) {
        const char *rest = name + 9;
        int degree = atoi(rest);
        const char *comma = strchr(rest, ',');
        if (comma) {
            uint64_t poly_val = strtoull(comma + 1, NULL, 0); /* handles 0x prefix */
            /* Look up in field registry by degree and poly */
            for (int i = 0; i < g_nfields; i++) {
                if (g_fields[i].field_kind == FIELD_KIND_BINARY && g_fields[i].degree == degree &&
                    g_fields[i].poly == poly_val) {
                    t.kind = TYPE_FIELD;
                    t.field_prime = g_fields[i].prime; /* 2^degree, e.g. 256 */
                    t.field_idx = i;
                    return t;
                }
            }
        }
        /* Fallback: compute from degree if not registered */
        t.kind = TYPE_FIELD;
        t.field_prime = (uint64_t)1 << degree;
        return t;
    }

    /* Poly type: Poly<F;d> */
    if (!strncmp(name, "Poly<", 5)) {
        char field_name[MAX_IDENT];
        int i = 5, j = 0;
        while (name[i] && name[i] != ';') field_name[j++] = name[i++];
        field_name[j] = 0;
        if (name[i] == ';') i++;
        int deg = atoi(name + i);

        Type field_type = resolve_type(field_name);
        t.kind = TYPE_POLY;
        t.field_prime = field_type.field_prime;
        t.poly_degree = deg;
        return t;
    }

    /* Register type: Register<F;d> */
    if (!strncmp(name, "Register<", 9)) {
        char field_name[MAX_IDENT];
        int i = 9, j = 0;
        while (name[i] && name[i] != ';') field_name[j++] = name[i++];
        field_name[j] = 0;
        if (name[i] == ';') i++;
        int deg = atoi(name + i);

        Type field_type = resolve_type(field_name);
        t.kind = TYPE_REGISTER;
        t.field_prime = field_type.field_prime;
        t.register_degree = deg;
        /* Dyn register: field resolves to TYPE_DYNFIELD (from F->dyn
         * substitution or literal Register<dyn,d>).  Mark via elem_kind
         * so codegen can distinguish from prime==0 ambiguity. */
        if (field_type.kind == TYPE_DYNFIELD) {
            t.elem_kind = TYPE_DYNFIELD;
            t.field_prime = 0;
        }
        return t;
    }

    /* Array type: [elem;size] */
    if (name[0] == '[') {
        char elem_name[MAX_IDENT];
        int i = 1, j = 0;
        while (name[i] && name[i] != ';') elem_name[j++] = name[i++];
        elem_name[j] = 0;
        if (name[i] == ';') i++;
        int sz = atoi(name + i);

        Type elem = resolve_type(elem_name);
        t.kind = TYPE_ARRAY;
        t.array_size = sz;
        t.elem_kind = elem.kind;
        t.field_prime = elem.field_prime; /* propagate if element is a field */
        return t;
    }

    /* Pointer type: *T */
    if (name[0] == '*') {
        Type inner = resolve_type(name + 1);
        t.kind = TYPE_PTR;
        t.ptr_pointee_kind = inner.kind;
        t.ptr_struct_id = inner.struct_id;
        t.ptr_enum_id = inner.enum_id;
        t.ptr_field_prime = inner.field_prime;
        t.ptr_field_idx = inner.field_idx;
        return t;
    }

    /* Struct type? */
    StructInfo *si = find_struct(name);
    if (si) {
        t.kind = TYPE_STRUCT;
        t.struct_id = (int)(si - g_structs);
        return t;
    }

    /* Enum type? */
    EnumInfo *ei_res = find_enum(name);
    if (ei_res) {
        t.kind = TYPE_ENUM;
        t.enum_id = (int)(ei_res - g_enums);
        return t;
    }

    t.kind = TYPE_VOID; /* fallback */
    return t;
}

/* Reconstruct the pointee Type from a TYPE_PTR */
static Type type_pointee(Type t) {
    Type inner = {0};
    if (t.kind != TYPE_PTR) return inner;
    inner.kind = t.ptr_pointee_kind;
    inner.struct_id = t.ptr_struct_id;
    inner.enum_id = t.ptr_enum_id;
    inner.field_prime = t.ptr_field_prime;
    inner.field_idx = t.ptr_field_idx;
    return inner;
}

/* Compute byte size of a type (for alloc) */
static int type_sizeof(Type t) {
    switch (t.kind) {
        case TYPE_BOOL: case TYPE_U8: case TYPE_I8: return 1;
        case TYPE_U16: case TYPE_I16: return 2;
        case TYPE_U32: case TYPE_I32: return 4;
        case TYPE_U64: case TYPE_I64: case TYPE_USIZE: return 8;
        case TYPE_PTR: return 8;
        case TYPE_FIELD: {
            if (t.field_idx >= 0 && t.field_idx < g_nfields &&
                g_fields[t.field_idx].field_kind == FIELD_KIND_EXTENSION) {
                return 2 * (g_fields[t.field_idx].elem_bits / 8);
            }
            FieldInfo *fi = find_field_by_prime(t.field_prime);
            return fi ? fi->elem_bits / 8 : 1;
        }
        case TYPE_DYNFIELD: return 8;     /* i64-backed regardless of actual prime */
        case TYPE_FIELD_VALUE: return 8;  /* ptr */
        case TYPE_STRUCT: {
            if (t.struct_id >= 0 && t.struct_id < g_nstructs) {
                StructInfo *si = &g_structs[t.struct_id];
                int total = 0;
                for (int i = 0; i < si->nfields; i++) {
                    int fsz = type_sizeof(si->fields[i].type);
                    int align = fsz > 8 ? 8 : (fsz < 1 ? 1 : fsz);
                    total = (total + align - 1) & ~(align - 1);
                    total += fsz;
                }
                /* Round up to 8-byte alignment */
                total = (total + 7) & ~7;
                return total > 0 ? total : 8;
            }
            return 8;
        }
        case TYPE_ENUM: {
            if (t.enum_id >= 0 && t.enum_id < g_nenums) {
                return g_enums[t.enum_id].total_size;
            }
            return 8;
        }
        case TYPE_ARRAY: {
            Type elem_t = {0};
            elem_t.kind = t.elem_kind;
            elem_t.field_prime = t.field_prime;
            elem_t.struct_id = t.struct_id;
            return t.array_size * type_sizeof(elem_t);
        }
        case TYPE_POLY: {
            /* Poly<F, d> stores d+1 coefficients */
            FieldInfo *fi = find_field_by_prime(t.field_prime);
            int coeff_size = fi ? fi->elem_bits / 8 : 1;
            return (t.poly_degree + 1) * coeff_size;
        }
        case TYPE_REGISTER: {
            /* Dyn register: { ptr, [d+1 x i64] } */
            if (t.elem_kind == TYPE_DYNFIELD) {
                return 8 + (t.register_degree + 1) * 8;
            }
            /* Register<F, d> stores d+1 field elements (same layout as Poly) */
            FieldInfo *fi = find_field_by_prime(t.field_prime);
            int coeff_size = fi ? fi->elem_bits / 8 : 1;
            return (t.register_degree + 1) * coeff_size;
        }
        default: return 8;
    }
}

/* Bit width of a type's LLVM IR representation.
 * Used for implicit widening: when two integer operands differ in width,
 * the narrower one must be extended before the instruction. */
static int type_ir_bitwidth(Type t) {
    switch (t.kind) {
        case TYPE_BOOL:                           return 1;
        case TYPE_U8:  case TYPE_I8:              return 8;
        case TYPE_U16: case TYPE_I16:             return 16;
        case TYPE_U32: case TYPE_I32:             return 32;
        case TYPE_U64: case TYPE_I64: case TYPE_USIZE: return 64;
        case TYPE_FIELD: {
            /* Extension fields are pairs, not scalars — no widening */
            if (t.field_idx >= 0 && t.field_idx < g_nfields &&
                g_fields[t.field_idx].field_kind == FIELD_KIND_EXTENSION) {
                return 0;
            }
            FieldInfo *fi = find_field_by_prime(t.field_prime);
            return fi ? fi->elem_bits : 8;
        }
        case TYPE_DYNFIELD: return 64;  /* i64-backed */
        default: return 0; /* non-integer types: 0 signals "don't widen" */
    }
}

static bool type_is_signed(Type t) {
    return (t.kind == TYPE_I8 || t.kind == TYPE_I16 ||
            t.kind == TYPE_I32 || t.kind == TYPE_I64);
}

/* ============================================================
 * Symbol table
 * ============================================================ */

static void sym_push_scope(void) {
    g_syms.scope_start[g_syms.scope_depth++] = g_syms.count;
}

static void sym_pop_scope(void) {
    g_syms.count = g_syms.scope_start[--g_syms.scope_depth];
}

static Symbol *sym_add(const char *name, Type type, int reg, bool is_alloca) {
    if (g_syms.count >= MAX_SYMBOLS) cap_overflow("local symbol table (g_syms)", MAX_SYMBOLS);
    Symbol *s = &g_syms.entries[g_syms.count++];
    strcpy(s->name, name);
    s->type = type;
    s->ir_reg = reg;
    s->is_alloca = is_alloca;
    return s;
}

/* Global symbol table — persists across all functions */
#define MAX_GLOBALS 1024
static struct {
    Symbol entries[MAX_GLOBALS];
    int count;
} g_globals;

static Symbol *sym_find(const char *name) {
    /* Search local/function scope first */
    for (int i = g_syms.count - 1; i >= 0; i--) {
        if (!strcmp(g_syms.entries[i].name, name))
            return &g_syms.entries[i];
    }
    /* Then search global scope */
    for (int i = g_globals.count - 1; i >= 0; i--) {
        if (!strcmp(g_globals.entries[i].name, name))
            return &g_globals.entries[i];
    }
    return NULL;
}

static Symbol *sym_add_global(const char *name, Type type, int reg, bool is_alloca) {
    if (g_globals.count >= MAX_GLOBALS) cap_overflow("global symbol table (g_globals)", MAX_GLOBALS);
    Symbol *s = &g_globals.entries[g_globals.count++];
    strcpy(s->name, name);
    s->type = type;
    s->ir_reg = reg;
    s->is_alloca = is_alloca;
    return s;
}

/* ============================================================
 * LLVM IR Code Generation
 * ============================================================ */

/* Track which field arithmetic functions we need to emit */
static bool g_field_funcs_emitted[MAX_FIELDS] = {0};

static const char *field_elem_ir(uint64_t prime) {
    FieldInfo *f = find_field_by_prime(prime);
    return f ? f->elem_ir : "i8";
}

static const char *field_wide_ir(uint64_t prime) {
    FieldInfo *f = find_field_by_prime(prime);
    return f ? f->wide_ir : "i16";
}

static char g_arr_ir_buf[64]; /* static buffer for array type strings */

static const char *type_to_ir(Type t) {
    switch (t.kind) {
        case TYPE_VOID:  return "void";
        case TYPE_BOOL:  return "i1";
        case TYPE_U8:  case TYPE_I8:  return "i8";
        case TYPE_U16: case TYPE_I16: return "i16";
        case TYPE_U32: case TYPE_I32: return "i32";
        case TYPE_U64: case TYPE_I64: return "i64";
        case TYPE_USIZE: return "i64";
        case TYPE_FIELD: {
            /* Extension fields use pair type */
            if (t.field_idx >= 0 && t.field_idx < g_nfields &&
                g_fields[t.field_idx].field_kind == FIELD_KIND_EXTENSION) {
                return g_fields[t.field_idx].ext_elem_ir;
            }
            return field_elem_ir(t.field_prime);
        }
        case TYPE_DYNFIELD: return "i64";     /* runtime field element: i64-backed */
        case TYPE_FIELD_VALUE: return "ptr";  /* ptr to %__Field carrier */
        case TYPE_ARRAY: {
            Type elem = {.kind = t.elem_kind, .field_prime = t.field_prime};
            snprintf(g_arr_ir_buf, sizeof(g_arr_ir_buf), "[%d x %s]",
                     t.array_size, type_to_ir(elem));
            return g_arr_ir_buf;
        }
        case TYPE_POLY: {
            snprintf(g_arr_ir_buf, sizeof(g_arr_ir_buf), "[%d x %s]",
                     t.poly_degree + 1, field_elem_ir(t.field_prime));
            return g_arr_ir_buf;
        }
        case TYPE_REGISTER: {
            if (t.elem_kind == TYPE_DYNFIELD) {
                /* Dyn register: { ptr (carrier), [d+1 x i64] (coeffs) } */
                snprintf(g_arr_ir_buf, sizeof(g_arr_ir_buf),
                         "{ ptr, [%d x i64] }", t.register_degree + 1);
                return g_arr_ir_buf;
            }
            snprintf(g_arr_ir_buf, sizeof(g_arr_ir_buf), "[%d x %s]",
                     t.register_degree + 1, field_elem_ir(t.field_prime));
            return g_arr_ir_buf;
        }
        case TYPE_STRUCT: {
            if (t.struct_id >= 0 && t.struct_id < g_nstructs) {
                return g_structs[t.struct_id].ir_name;
            }
            return "i32";
        }
        case TYPE_ENUM: {
            if (t.enum_id >= 0 && t.enum_id < g_nenums) {
                return g_enums[t.enum_id].ir_name;
            }
            return "i32";
        }
        case TYPE_PTR: return "ptr";
        default: return "i32";
    }
}

/* ============================================================
 * GF(2^8) tables and functions
 * ============================================================ */

/* Precomputed GF(2^8) log/exp tables with generator 0x03, polynomial 0x11B */
static const uint8_t gf256_exp[256] = {
    0x01,0x03,0x05,0x0F,0x11,0x33,0x55,0xFF,0x1A,0x2E,0x72,0x96,0xA1,0xF8,0x13,0x35,
    0x5F,0xE1,0x38,0x48,0xD8,0x73,0x95,0xA4,0xF7,0x02,0x06,0x0A,0x1E,0x22,0x66,0xAA,
    0xE5,0x34,0x5C,0xE4,0x37,0x59,0xEB,0x26,0x6A,0xBE,0xD9,0x70,0x90,0xAB,0xE6,0x31,
    0x53,0xF5,0x04,0x0C,0x14,0x3C,0x44,0xCC,0x4F,0xD1,0x68,0xB8,0xD3,0x6E,0xB2,0xCD,
    0x4C,0xD4,0x67,0xA9,0xE0,0x3B,0x4D,0xD7,0x62,0xA6,0xF1,0x08,0x18,0x28,0x78,0x88,
    0x83,0x9E,0xB9,0xD0,0x6B,0xBD,0xDC,0x7F,0x81,0x98,0xB3,0xCE,0x49,0xDB,0x76,0x9A,
    0xB5,0xC4,0x57,0xF9,0x10,0x30,0x50,0xF0,0x0B,0x1D,0x27,0x69,0xBB,0xD6,0x61,0xA3,
    0xFE,0x19,0x2B,0x7D,0x87,0x92,0xAD,0xEC,0x2F,0x71,0x93,0xAE,0xE9,0x20,0x60,0xA0,
    0xFB,0x16,0x3A,0x4E,0xD2,0x6D,0xB7,0xC2,0x5D,0xE7,0x32,0x56,0xFA,0x15,0x3F,0x41,
    0xC3,0x5E,0xE2,0x3D,0x47,0xC9,0x40,0xC0,0x5B,0xED,0x2C,0x74,0x9C,0xBF,0xDA,0x75,
    0x9F,0xBA,0xD5,0x64,0xAC,0xEF,0x2A,0x7E,0x82,0x9D,0xBC,0xDF,0x7A,0x8E,0x89,0x80,
    0x9B,0xB6,0xC1,0x58,0xE8,0x23,0x65,0xAF,0xEA,0x25,0x6F,0xB1,0xC8,0x43,0xC5,0x54,
    0xFC,0x1F,0x21,0x63,0xA5,0xF4,0x07,0x09,0x1B,0x2D,0x77,0x99,0xB0,0xCB,0x46,0xCA,
    0x45,0xCF,0x4A,0xDE,0x79,0x8B,0x86,0x91,0xA8,0xE3,0x3E,0x42,0xC6,0x51,0xF3,0x0E,
    0x12,0x36,0x5A,0xEE,0x29,0x7B,0x8D,0x8C,0x8F,0x8A,0x85,0x94,0xA7,0xF2,0x0D,0x17,
    0x39,0x4B,0xDD,0x7C,0x84,0x97,0xA2,0xFD,0x1C,0x24,0x6C,0xB4,0xC7,0x52,0xF6,0x01,
};

static const uint8_t gf256_log[256] = {
    0x00,0x00,0x19,0x01,0x32,0x02,0x1A,0xC6,0x4B,0xC7,0x1B,0x68,0x33,0xEE,0xDF,0x03,
    0x64,0x04,0xE0,0x0E,0x34,0x8D,0x81,0xEF,0x4C,0x71,0x08,0xC8,0xF8,0x69,0x1C,0xC1,
    0x7D,0xC2,0x1D,0xB5,0xF9,0xB9,0x27,0x6A,0x4D,0xE4,0xA6,0x72,0x9A,0xC9,0x09,0x78,
    0x65,0x2F,0x8A,0x05,0x21,0x0F,0xE1,0x24,0x12,0xF0,0x82,0x45,0x35,0x93,0xDA,0x8E,
    0x96,0x8F,0xDB,0xBD,0x36,0xD0,0xCE,0x94,0x13,0x5C,0xD2,0xF1,0x40,0x46,0x83,0x38,
    0x66,0xDD,0xFD,0x30,0xBF,0x06,0x8B,0x62,0xB3,0x25,0xE2,0x98,0x22,0x88,0x91,0x10,
    0x7E,0x6E,0x48,0xC3,0xA3,0xB6,0x1E,0x42,0x3A,0x6B,0x28,0x54,0xFA,0x85,0x3D,0xBA,
    0x2B,0x79,0x0A,0x15,0x9B,0x9F,0x5E,0xCA,0x4E,0xD4,0xAC,0xE5,0xF3,0x73,0xA7,0x57,
    0xAF,0x58,0xA8,0x50,0xF4,0xEA,0xD6,0x74,0x4F,0xAE,0xE9,0xD5,0xE7,0xE6,0xAD,0xE8,
    0x2C,0xD7,0x75,0x7A,0xEB,0x16,0x0B,0xF5,0x59,0xCB,0x5F,0xB0,0x9C,0xA9,0x51,0xA0,
    0x7F,0x0C,0xF6,0x6F,0x17,0xC4,0x49,0xEC,0xD8,0x43,0x1F,0x2D,0xA4,0x76,0x7B,0xB7,
    0xCC,0xBB,0x3E,0x5A,0xFB,0x60,0xB1,0x86,0x3B,0x52,0xA1,0x6C,0xAA,0x55,0x29,0x9D,
    0x97,0xB2,0x87,0x90,0x61,0xBE,0xDC,0xFC,0xBC,0x95,0xCF,0xCD,0x37,0x3F,0x5B,0xD1,
    0x53,0x39,0x84,0x3C,0x41,0xA2,0x6D,0x47,0x14,0x2A,0x9E,0x5D,0x56,0xF2,0xD3,0xAB,
    0x44,0x11,0x92,0xD9,0x23,0x20,0x2E,0x89,0xB4,0x7C,0xB8,0x26,0x77,0x99,0xE3,0xA5,
    0x67,0x4A,0xED,0xDE,0xC5,0x31,0xFE,0x18,0x0D,0x63,0x8C,0x80,0xC0,0xF7,0x70,0x07,
};

static bool g_gf256_emitted = false;

static void emit_gf256_tables(void) {
    if (g_gf256_emitted) return;
    g_gf256_emitted = true;

    /* Emit exp table */
    ir_emit("\n@gf256_exp = private constant [256 x i8] [");
    for (int i = 0; i < 256; i++) {
        if (i > 0) ir_emit(",");
        if (i % 16 == 0) ir_emit("\n  ");
        ir_emit("i8 %d", (int)gf256_exp[i]);
    }
    ir_emit("\n]\n");

    /* Emit log table */
    ir_emit("\n@gf256_log = private constant [256 x i8] [");
    for (int i = 0; i < 256; i++) {
        if (i > 0) ir_emit(",");
        if (i % 16 == 0) ir_emit("\n  ");
        ir_emit("i8 %d", (int)gf256_log[i]);
    }
    ir_emit("\n]\n");
}

static void emit_gf256_funcs(void) {
    /* add: XOR */
    ir_emit("\ndefine internal i8 @gf256_add(i8 %%a, i8 %%b) alwaysinline {\n");
    ir_emit("  %%out = xor i8 %%a, %%b\n");
    ir_emit("  ret i8 %%out\n");
    ir_emit("}\n");

    /* sub: XOR (same as add in characteristic 2) */
    ir_emit("\ndefine internal i8 @gf256_sub(i8 %%a, i8 %%b) alwaysinline {\n");
    ir_emit("  %%out = xor i8 %%a, %%b\n");
    ir_emit("  ret i8 %%out\n");
    ir_emit("}\n");

    /* mul: log/exp table lookup */
    ir_emit("\ndefine internal i8 @gf256_mul(i8 %%a, i8 %%b) alwaysinline {\n");
    ir_emit("entry:\n");
    ir_emit("  %%a.zero = icmp eq i8 %%a, 0\n");
    ir_emit("  %%b.zero = icmp eq i8 %%b, 0\n");
    ir_emit("  %%either = or i1 %%a.zero, %%b.zero\n");
    ir_emit("  br i1 %%either, label %%ret.zero, label %%do.mul\n");
    ir_emit("do.mul:\n");
    ir_emit("  %%ai = zext i8 %%a to i64\n");
    ir_emit("  %%bi = zext i8 %%b to i64\n");
    ir_emit("  %%la.ptr = getelementptr [256 x i8], ptr @gf256_log, i64 0, i64 %%ai\n");
    ir_emit("  %%lb.ptr = getelementptr [256 x i8], ptr @gf256_log, i64 0, i64 %%bi\n");
    ir_emit("  %%la = load i8, ptr %%la.ptr\n");
    ir_emit("  %%lb = load i8, ptr %%lb.ptr\n");
    /* log sum in i16 to handle overflow: max log = 254, so max sum = 508 */
    ir_emit("  %%la16 = zext i8 %%la to i16\n");
    ir_emit("  %%lb16 = zext i8 %%lb to i16\n");
    ir_emit("  %%lsum = add i16 %%la16, %%lb16\n");
    /* mod 255 */
    ir_emit("  %%ge = icmp uge i16 %%lsum, 255\n");
    ir_emit("  %%sub = sub i16 %%lsum, 255\n");
    ir_emit("  %%lmod = select i1 %%ge, i16 %%sub, i16 %%lsum\n");
    ir_emit("  %%li = zext i16 %%lmod to i64\n");
    ir_emit("  %%e.ptr = getelementptr [256 x i8], ptr @gf256_exp, i64 0, i64 %%li\n");
    ir_emit("  %%result = load i8, ptr %%e.ptr\n");
    ir_emit("  ret i8 %%result\n");
    ir_emit("ret.zero:\n");
    ir_emit("  ret i8 0\n");
    ir_emit("}\n");

    /* pow: repeated squaring using gf256_mul */
    ir_emit("\ndefine internal i8 @gf256_pow(i8 %%base_in, i32 %%exp_in) alwaysinline {\n");
    ir_emit("entry:\n");
    ir_emit("  br label %%loop\n");
    ir_emit("loop:\n");
    ir_emit("  %%result = phi i8 [1, %%entry], [%%result.next, %%loop.end]\n");
    ir_emit("  %%base = phi i8 [%%base_in, %%entry], [%%base.next, %%loop.end]\n");
    ir_emit("  %%exp = phi i32 [%%exp_in, %%entry], [%%exp.next, %%loop.end]\n");
    ir_emit("  %%done = icmp eq i32 %%exp, 0\n");
    ir_emit("  br i1 %%done, label %%exit, label %%body\n");
    ir_emit("body:\n");
    ir_emit("  %%odd = and i32 %%exp, 1\n");
    ir_emit("  %%is_odd = icmp ne i32 %%odd, 0\n");
    ir_emit("  %%mul_res = call i8 @gf256_mul(i8 %%result, i8 %%base)\n");
    ir_emit("  %%result.maybe = select i1 %%is_odd, i8 %%mul_res, i8 %%result\n");
    ir_emit("  %%base.sq = call i8 @gf256_mul(i8 %%base, i8 %%base)\n");
    ir_emit("  %%exp.shr = lshr i32 %%exp, 1\n");
    ir_emit("  br label %%loop.end\n");
    ir_emit("loop.end:\n");
    ir_emit("  %%result.next = phi i8 [%%result.maybe, %%body]\n");
    ir_emit("  %%base.next = phi i8 [%%base.sq, %%body]\n");
    ir_emit("  %%exp.next = phi i32 [%%exp.shr, %%body]\n");
    ir_emit("  br label %%loop\n");
    ir_emit("exit:\n");
    ir_emit("  ret i8 %%result\n");
    ir_emit("}\n");

    /* inv: a^(254) = a^(2^8 - 2) */
    ir_emit("\ndefine internal i8 @gf256_inv(i8 %%a) alwaysinline {\n");
    ir_emit("  %%r = call i8 @gf256_pow(i8 %%a, i32 254)\n");
    ir_emit("  ret i8 %%r\n");
    ir_emit("}\n");

    /* div: a * inv(b) */
    ir_emit("\ndefine internal i8 @gf256_div(i8 %%a, i8 %%b) alwaysinline {\n");
    ir_emit("  %%inv = call i8 @gf256_inv(i8 %%b)\n");
    ir_emit("  %%r = call i8 @gf256_mul(i8 %%a, i8 %%inv)\n");
    ir_emit("  ret i8 %%r\n");
    ir_emit("}\n");
}

/* Emit NTT root chain tables as LLVM IR global constant arrays.
 * Three tables per NTT-friendly field:
 *   @ntt_roots_<p>      — forward roots of unity
 *   @ntt_inv_roots_<p>  — inverse roots (for INTT)
 *   @ntt_n_inv_<p>      — scaling factors (for INTT normalization)
 * Same pattern as emit_gf256_tables: private constant arrays, once per field. */
static bool g_ntt_emitted[MAX_FIELDS] = {false};

static void emit_ntt_tables(FieldInfo *fi) {
    /* Find field index for guard */
    int fi_idx = -1;
    for (int i = 0; i < g_nfields; i++) {
        if (&g_fields[i] == fi) { fi_idx = i; break; }
    }
    if (fi_idx < 0 || fi->ntt_max_log == 0) return;
    if (g_ntt_emitted[fi_idx]) return;
    g_ntt_emitted[fi_idx] = true;

    uint64_t p = fi->prime;
    const char *el = fi->elem_ir;
    int n = fi->ntt_max_log;

    /* Forward roots */
    ir_emit("\n@ntt_roots_%llu = private constant [%d x %s] [",
            (unsigned long long)p, n, el);
    for (int i = 0; i < n; i++) {
        if (i > 0) ir_emit(",");
        if (i % 4 == 0) ir_emit("\n  ");
        ir_emit("%s %llu", el, (unsigned long long)fi->ntt_roots[i]);
    }
    ir_emit("\n]\n");

    /* Inverse roots */
    ir_emit("\n@ntt_inv_roots_%llu = private constant [%d x %s] [",
            (unsigned long long)p, n, el);
    for (int i = 0; i < n; i++) {
        if (i > 0) ir_emit(",");
        if (i % 4 == 0) ir_emit("\n  ");
        ir_emit("%s %llu", el, (unsigned long long)fi->ntt_inv_roots[i]);
    }
    ir_emit("\n]\n");

    /* Inverse-of-n scaling factors */
    ir_emit("\n@ntt_n_inv_%llu = private constant [%d x %s] [",
            (unsigned long long)p, n, el);
    for (int i = 0; i < n; i++) {
        if (i > 0) ir_emit(",");
        if (i % 4 == 0) ir_emit("\n  ");
        ir_emit("%s %llu", el, (unsigned long long)fi->ntt_n_inv[i]);
    }
    ir_emit("\n]\n");
}

/* Emit field arithmetic functions for a given prime */
static void emit_field_funcs(uint64_t prime) {
    /* Check if already emitted */
    for (int i = 0; i < g_nfields; i++) {
        if (g_fields[i].prime == prime && g_field_funcs_emitted[i]) return;
        if (g_fields[i].prime == prime) { g_field_funcs_emitted[i] = true; break; }
    }

    const char *el = field_elem_ir(prime);
    const char *wi = field_wide_ir(prime);

    /* add */
    ir_emit("\ndefine internal %s @field%llu_add(%s %%a, %s %%b) alwaysinline {\n", el, (unsigned long long)prime, el, el);
    ir_emit("  %%a.w = zext %s %%a to %s\n", el, wi);
    ir_emit("  %%b.w = zext %s %%b to %s\n", el, wi);
    ir_emit("  %%sum = add %s %%a.w, %%b.w\n", wi);
    ir_emit("  %%ge = icmp uge %s %%sum, %llu\n", wi, (unsigned long long)prime);
    ir_emit("  %%sub = sub %s %%sum, %llu\n", wi, (unsigned long long)prime);
    ir_emit("  %%res = select i1 %%ge, %s %%sub, %s %%sum\n", wi, wi);
    ir_emit("  %%out = trunc %s %%res to %s\n", wi, el);
    ir_emit("  ret %s %%out\n", el);
    ir_emit("}\n");

    /* sub */
    ir_emit("\ndefine internal %s @field%llu_sub(%s %%a, %s %%b) alwaysinline {\n", el, (unsigned long long)prime, el, el);
    ir_emit("  %%a.w = zext %s %%a to %s\n", el, wi);
    ir_emit("  %%b.w = zext %s %%b to %s\n", el, wi);
    ir_emit("  %%d = add %s %%a.w, %llu\n", wi, (unsigned long long)prime);
    ir_emit("  %%d2 = sub %s %%d, %%b.w\n", wi);
    ir_emit("  %%ge = icmp uge %s %%d2, %llu\n", wi, (unsigned long long)prime);
    ir_emit("  %%sub = sub %s %%d2, %llu\n", wi, (unsigned long long)prime);
    ir_emit("  %%res = select i1 %%ge, %s %%sub, %s %%d2\n", wi, wi);
    ir_emit("  %%out = trunc %s %%res to %s\n", wi, el);
    ir_emit("  ret %s %%out\n", el);
    ir_emit("}\n");

    /* mul */
    FieldInfo *fi_mul = find_field_by_prime(prime);
    if (fi_mul && fi_mul->elem_bits == 64) {
        /* Barrett reduction for 64-bit fields: replaces urem i128 (which
         * compiles to a __udivti3 software division call) with a multiply-high
         * sequence that LLVM lowers to inline instructions on all targets.
         *
         * Barrett constant: m = floor(2^128 / p), stored as two u64 halves.
         * Reduction: q = (prod * m) >> 128; r = prod - q * p; if r >= p: r -= p.
         */
        __uint128_t max128 = ~((__uint128_t)0);  /* 2^128 - 1 */
        __uint128_t m128 = max128 / (__uint128_t)prime;
        uint64_t rem_chk = (uint64_t)(max128 % (__uint128_t)prime);
        if (rem_chk == prime - 1) m128 += 1;     /* exact: 2^128 / p */
        uint64_t m_lo = (uint64_t)m128;
        uint64_t m_hi = (uint64_t)(m128 >> 64);

        ir_emit("\ndefine internal i64 @field%llu_mul(i64 %%a, i64 %%b) alwaysinline {\n",
                (unsigned long long)prime);
        /* prod = a * b in i128 */
        ir_emit("  %%a128 = zext i64 %%a to i128\n");
        ir_emit("  %%b128 = zext i64 %%b to i128\n");
        ir_emit("  %%prod = mul i128 %%a128, %%b128\n");
        /* Reconstruct Barrett constant m in i128 from two u64 halves */
        ir_emit("  %%m_lo = zext i64 %llu to i128\n", (unsigned long long)m_lo);
        ir_emit("  %%m_hi = zext i64 %llu to i128\n", (unsigned long long)m_hi);
        ir_emit("  %%m_sh = shl i128 %%m_hi, 64\n");
        ir_emit("  %%m = or i128 %%m_sh, %%m_lo\n");
        /* q = (prod * m) >> 128  via i256 */
        ir_emit("  %%p256 = zext i128 %%prod to i256\n");
        ir_emit("  %%m256 = zext i128 %%m to i256\n");
        ir_emit("  %%qfull = mul i256 %%p256, %%m256\n");
        ir_emit("  %%q256 = lshr i256 %%qfull, 128\n");
        ir_emit("  %%q = trunc i256 %%q256 to i128\n");
        /* r = prod - q * p */
        ir_emit("  %%qp = mul i128 %%q, %llu\n", (unsigned long long)prime);
        ir_emit("  %%r = sub i128 %%prod, %%qp\n");
        /* Correction: if r >= p, r -= p */
        ir_emit("  %%ge = icmp uge i128 %%r, %llu\n", (unsigned long long)prime);
        ir_emit("  %%rsub = sub i128 %%r, %llu\n", (unsigned long long)prime);
        ir_emit("  %%rfin = select i1 %%ge, i128 %%rsub, i128 %%r\n");
        ir_emit("  %%out = trunc i128 %%rfin to i64\n");
        ir_emit("  ret i64 %%out\n");
        ir_emit("}\n");
    } else {
        /* Standard urem for fields up to 32-bit (hardware division) */
        ir_emit("\ndefine internal %s @field%llu_mul(%s %%a, %s %%b) alwaysinline {\n",
                el, (unsigned long long)prime, el, el);
        ir_emit("  %%a.w = zext %s %%a to %s\n", el, wi);
        ir_emit("  %%b.w = zext %s %%b to %s\n", el, wi);
        ir_emit("  %%prod = mul %s %%a.w, %%b.w\n", wi);
        ir_emit("  %%rem = urem %s %%prod, %llu\n", wi, (unsigned long long)prime);
        ir_emit("  %%out = trunc %s %%rem to %s\n", wi, el);
        ir_emit("  ret %s %%out\n", el);
        ir_emit("}\n");
    }

    /* pow: a^n mod p by repeated squaring.
     * Exponent type: i64 for 64-bit fields (p-2 doesn't fit i32), i32 otherwise. */
    FieldInfo *fi_pow = find_field_by_prime(prime);
    const char *exp_ty = (fi_pow && fi_pow->elem_bits == 64) ? "i64" : "i32";

    ir_emit("\ndefine internal %s @field%llu_pow(%s %%base_in, %s %%exp_in) alwaysinline {\n",
            el, (unsigned long long)prime, el, exp_ty);
    ir_emit("entry:\n");
    ir_emit("  br label %%loop\n");
    ir_emit("loop:\n");
    ir_emit("  %%result = phi %s [1, %%entry], [%%result.next, %%loop.end]\n", el);
    ir_emit("  %%base = phi %s [%%base_in, %%entry], [%%base.next, %%loop.end]\n", el);
    ir_emit("  %%exp = phi %s [%%exp_in, %%entry], [%%exp.next, %%loop.end]\n", exp_ty);
    ir_emit("  %%done = icmp eq %s %%exp, 0\n", exp_ty);
    ir_emit("  br i1 %%done, label %%exit, label %%body\n");
    ir_emit("body:\n");
    ir_emit("  %%odd = and %s %%exp, 1\n", exp_ty);
    ir_emit("  %%is_odd = icmp ne %s %%odd, 0\n", exp_ty);
    ir_emit("  %%mul_res = call %s @field%llu_mul(%s %%result, %s %%base)\n",
            el, (unsigned long long)prime, el, el);
    ir_emit("  %%result.maybe = select i1 %%is_odd, %s %%mul_res, %s %%result\n", el, el);
    ir_emit("  %%base.sq = call %s @field%llu_mul(%s %%base, %s %%base)\n",
            el, (unsigned long long)prime, el, el);
    ir_emit("  %%exp.shr = lshr %s %%exp, 1\n", exp_ty);
    ir_emit("  br label %%loop.end\n");
    ir_emit("loop.end:\n");
    ir_emit("  %%result.next = phi %s [%%result.maybe, %%body]\n", el);
    ir_emit("  %%base.next = phi %s [%%base.sq, %%body]\n", el);
    ir_emit("  %%exp.next = phi %s [%%exp.shr, %%body]\n", exp_ty);
    ir_emit("  br label %%loop\n");
    ir_emit("exit:\n");
    ir_emit("  ret %s %%result\n", el);
    ir_emit("}\n");

    /* inv: a^(p-2) mod p */
    ir_emit("\ndefine internal %s @field%llu_inv(%s %%a) alwaysinline {\n", el, (unsigned long long)prime, el);
    ir_emit("  %%r = call %s @field%llu_pow(%s %%a, %s %llu)\n",
            el, (unsigned long long)prime, el, exp_ty, (unsigned long long)(prime - 2));
    ir_emit("  ret %s %%r\n", el);
    ir_emit("}\n");

    /* div: a * inv(b) */
    ir_emit("\ndefine internal %s @field%llu_div(%s %%a, %s %%b) alwaysinline {\n",
            el, (unsigned long long)prime, el, el);
    ir_emit("  %%inv = call %s @field%llu_inv(%s %%b)\n",
            el, (unsigned long long)prime, el);
    ir_emit("  %%r = call %s @field%llu_mul(%s %%a, %s %%inv)\n",
            el, (unsigned long long)prime, el, el);
    ir_emit("  ret %s %%r\n", el);
    ir_emit("}\n");
}

/* ============================================================
 * Dynamic field arithmetic: runtime-prime operations.
 *
 * The modulus is NOT a compile-time immediate — it is loaded at
 * runtime from a %__Field carrier object passed by pointer. The
 * algorithm is identical to the baked path; only the constants are
 * late-bound. This is dictionary-passing where the dictionary
 * degenerated to data (no code pointers): see plan-dynfield-unification.
 *
 * %__Field layout: { i64 p, i64 half_p, i32 elem_bytes, i32 data_bytes,
 *                    i64 barrett_lo, i64 barrett_hi }
 * All elements are i64-backed (storage uniform; elem_bytes governs
 * truncation for narrow primes). Six functions, emitted once per module.
 *
 * add/sub: load p, widen to i128, branchless conditional subtract.
 * mul: load Barrett constants (barrett_lo/hi), i256 multiply-high.
 *   This is the same Barrett as the baked 64-bit path (tvc.c emit
 *   above) but with runtime-loaded m instead of a baked literal —
 *   works for ANY prime up to 2^63, no urem, no __udivti3.
 * pow/inv/div: repeated-squaring / Fermat over dyn_mul.
 * ============================================================ */

#define DYNFIELD_IR_NAME "%__Field"

static bool g_dynfield_emitted = false;

/* Emit the %__Field struct type into the module preamble. */
static void emit_dynfield_type(void) {
    ir_emit("\n; --- dynamic field carrier ---\n");
    ir_emit("%%__Field = type { i64, i64, i32, i32, i64, i64 }\n");
}

static void emit_dynfield_funcs(void) {
    if (g_dynfield_emitted) return;
    g_dynfield_emitted = true;

    /* GEP helper indices into %__Field:
     *   0 = p, 1 = half_p, 2 = elem_bytes, 3 = data_bytes,
     *   4 = barrett_lo, 5 = barrett_hi */

    /* add: (a + b) mod p, branchless. p loaded at runtime. */
    ir_emit("\ndefine internal i64 @field_dyn_add(i64 %%a, i64 %%b, ptr %%f) alwaysinline {\n");
    ir_emit("  %%pp = getelementptr %%__Field, ptr %%f, i32 0, i32 0\n");
    ir_emit("  %%p = load i64, ptr %%pp\n");
    ir_emit("  %%aw = zext i64 %%a to i128\n");
    ir_emit("  %%bw = zext i64 %%b to i128\n");
    ir_emit("  %%pw = zext i64 %%p to i128\n");
    ir_emit("  %%sum = add i128 %%aw, %%bw\n");
    ir_emit("  %%ge = icmp uge i128 %%sum, %%pw\n");
    ir_emit("  %%sub = sub i128 %%sum, %%pw\n");
    ir_emit("  %%res = select i1 %%ge, i128 %%sub, i128 %%sum\n");
    ir_emit("  %%out = trunc i128 %%res to i64\n");
    ir_emit("  ret i64 %%out\n");
    ir_emit("}\n");

    /* sub: (a - b) mod p, branchless. */
    ir_emit("\ndefine internal i64 @field_dyn_sub(i64 %%a, i64 %%b, ptr %%f) alwaysinline {\n");
    ir_emit("  %%pp = getelementptr %%__Field, ptr %%f, i32 0, i32 0\n");
    ir_emit("  %%p = load i64, ptr %%pp\n");
    ir_emit("  %%aw = zext i64 %%a to i128\n");
    ir_emit("  %%bw = zext i64 %%b to i128\n");
    ir_emit("  %%pw = zext i64 %%p to i128\n");
    ir_emit("  %%d = add i128 %%aw, %%pw\n");
    ir_emit("  %%d2 = sub i128 %%d, %%bw\n");
    ir_emit("  %%ge = icmp uge i128 %%d2, %%pw\n");
    ir_emit("  %%sub = sub i128 %%d2, %%pw\n");
    ir_emit("  %%res = select i1 %%ge, i128 %%sub, i128 %%d2\n");
    ir_emit("  %%out = trunc i128 %%res to i64\n");
    ir_emit("  ret i64 %%out\n");
    ir_emit("}\n");

    /* mul: Barrett reduction with runtime constant m = floor(2^128 / p).
     * q = (prod * m) >> 128; r = prod - q*p; correct once. */
    ir_emit("\ndefine internal i64 @field_dyn_mul(i64 %%a, i64 %%b, ptr %%f) alwaysinline {\n");
    ir_emit("  %%pp = getelementptr %%__Field, ptr %%f, i32 0, i32 0\n");
    ir_emit("  %%p = load i64, ptr %%pp\n");
    ir_emit("  %%mlop = getelementptr %%__Field, ptr %%f, i32 0, i32 4\n");
    ir_emit("  %%mlo = load i64, ptr %%mlop\n");
    ir_emit("  %%mhip = getelementptr %%__Field, ptr %%f, i32 0, i32 5\n");
    ir_emit("  %%mhi = load i64, ptr %%mhip\n");
    /* prod = a*b in i128 */
    ir_emit("  %%aw = zext i64 %%a to i128\n");
    ir_emit("  %%bw = zext i64 %%b to i128\n");
    ir_emit("  %%prod = mul i128 %%aw, %%bw\n");
    /* reconstruct m in i128 */
    ir_emit("  %%mlo128 = zext i64 %%mlo to i128\n");
    ir_emit("  %%mhi128 = zext i64 %%mhi to i128\n");
    ir_emit("  %%msh = shl i128 %%mhi128, 64\n");
    ir_emit("  %%m = or i128 %%msh, %%mlo128\n");
    /* q = (prod * m) >> 128 via i256 */
    ir_emit("  %%p256 = zext i128 %%prod to i256\n");
    ir_emit("  %%m256 = zext i128 %%m to i256\n");
    ir_emit("  %%qfull = mul i256 %%p256, %%m256\n");
    ir_emit("  %%q256 = lshr i256 %%qfull, 128\n");
    ir_emit("  %%q = trunc i256 %%q256 to i128\n");
    /* r = prod - q*p */
    ir_emit("  %%pw = zext i64 %%p to i128\n");
    ir_emit("  %%qp = mul i128 %%q, %%pw\n");
    ir_emit("  %%r = sub i128 %%prod, %%qp\n");
    /* correction: while r >= p (at most twice for safety) */
    ir_emit("  %%ge1 = icmp uge i128 %%r, %%pw\n");
    ir_emit("  %%r1sub = sub i128 %%r, %%pw\n");
    ir_emit("  %%r1 = select i1 %%ge1, i128 %%r1sub, i128 %%r\n");
    ir_emit("  %%ge2 = icmp uge i128 %%r1, %%pw\n");
    ir_emit("  %%r2sub = sub i128 %%r1, %%pw\n");
    ir_emit("  %%r2 = select i1 %%ge2, i128 %%r2sub, i128 %%r1\n");
    ir_emit("  %%out = trunc i128 %%r2 to i64\n");
    ir_emit("  ret i64 %%out\n");
    ir_emit("}\n");

    /* pow: base^exp mod p by repeated squaring over dyn_mul. */
    ir_emit("\ndefine internal i64 @field_dyn_pow(i64 %%base_in, i64 %%exp_in, ptr %%f) alwaysinline {\n");
    ir_emit("entry:\n");
    ir_emit("  br label %%loop\n");
    ir_emit("loop:\n");
    ir_emit("  %%result = phi i64 [1, %%entry], [%%result.next, %%loop.end]\n");
    ir_emit("  %%base = phi i64 [%%base_in, %%entry], [%%base.next, %%loop.end]\n");
    ir_emit("  %%exp = phi i64 [%%exp_in, %%entry], [%%exp.next, %%loop.end]\n");
    ir_emit("  %%done = icmp eq i64 %%exp, 0\n");
    ir_emit("  br i1 %%done, label %%exit, label %%body\n");
    ir_emit("body:\n");
    ir_emit("  %%odd = and i64 %%exp, 1\n");
    ir_emit("  %%is_odd = icmp ne i64 %%odd, 0\n");
    ir_emit("  %%mul_res = call i64 @field_dyn_mul(i64 %%result, i64 %%base, ptr %%f)\n");
    ir_emit("  %%result.maybe = select i1 %%is_odd, i64 %%mul_res, i64 %%result\n");
    ir_emit("  %%base.sq = call i64 @field_dyn_mul(i64 %%base, i64 %%base, ptr %%f)\n");
    ir_emit("  %%exp.shr = lshr i64 %%exp, 1\n");
    ir_emit("  br label %%loop.end\n");
    ir_emit("loop.end:\n");
    ir_emit("  %%result.next = phi i64 [%%result.maybe, %%body]\n");
    ir_emit("  %%base.next = phi i64 [%%base.sq, %%body]\n");
    ir_emit("  %%exp.next = phi i64 [%%exp.shr, %%body]\n");
    ir_emit("  br label %%loop\n");
    ir_emit("exit:\n");
    ir_emit("  ret i64 %%result\n");
    ir_emit("}\n");

    /* inv: a^(p-2) mod p (Fermat). p loaded at runtime. */
    ir_emit("\ndefine internal i64 @field_dyn_inv(i64 %%a, ptr %%f) alwaysinline {\n");
    ir_emit("  %%pp = getelementptr %%__Field, ptr %%f, i32 0, i32 0\n");
    ir_emit("  %%p = load i64, ptr %%pp\n");
    ir_emit("  %%pm2 = sub i64 %%p, 2\n");
    ir_emit("  %%r = call i64 @field_dyn_pow(i64 %%a, i64 %%pm2, ptr %%f)\n");
    ir_emit("  ret i64 %%r\n");
    ir_emit("}\n");

    /* div: a * inv(b) */
    ir_emit("\ndefine internal i64 @field_dyn_div(i64 %%a, i64 %%b, ptr %%f) alwaysinline {\n");
    ir_emit("  %%inv = call i64 @field_dyn_inv(i64 %%b, ptr %%f)\n");
    ir_emit("  %%r = call i64 @field_dyn_mul(i64 %%a, i64 %%inv, ptr %%f)\n");
    ir_emit("  ret i64 %%r\n");
    ir_emit("}\n");

    /* __udivrem_128(n, d) -> {quotient, remainder}: 128-bit unsigned divide,
     * DIVISION-FREE via bit-serial long division (shl/icmp/sub/select/or only,
     * no udiv/urem i128 -> no __udivti3/__umodti3 libcall). Mirrors the wide
     * path's i576 routine one width down. Sole consumer is field construction
     * (Barrett factor + Miller-Rabin modmul); never the hot path. d != 0 is
     * guaranteed (validated prime). Kept in sync with tvc_self.tv. */
    ir_emit("\ndefine internal {i128, i128} @__udivrem_128(i128 %%n, i128 %%d) {\n");
    ir_emit("entry:\n");
    ir_emit("  br label %%dv.head\n");
    ir_emit("dv.head:\n");
    ir_emit("  %%i = phi i32 [ 128, %%entry ], [ %%inext, %%dv.body ]\n");
    ir_emit("  %%ncur = phi i128 [ %%n, %%entry ], [ %%ncurn, %%dv.body ]\n");
    ir_emit("  %%rem = phi i128 [ 0, %%entry ], [ %%remfin, %%dv.body ]\n");
    ir_emit("  %%quot = phi i128 [ 0, %%entry ], [ %%quotn, %%dv.body ]\n");
    ir_emit("  %%cont = icmp ne i32 %%i, 0\n");
    ir_emit("  br i1 %%cont, label %%dv.body, label %%dv.done\n");
    ir_emit("dv.body:\n");
    ir_emit("  %%msb = lshr i128 %%ncur, 127\n");
    ir_emit("  %%ncurn = shl i128 %%ncur, 1\n");
    ir_emit("  %%remsh = shl i128 %%rem, 1\n");
    ir_emit("  %%remin = or i128 %%remsh, %%msb\n");
    ir_emit("  %%ge = icmp uge i128 %%remin, %%d\n");
    ir_emit("  %%rsub = sub i128 %%remin, %%d\n");
    ir_emit("  %%remfin = select i1 %%ge, i128 %%rsub, i128 %%remin\n");
    ir_emit("  %%qb = zext i1 %%ge to i128\n");
    ir_emit("  %%qsh = shl i128 %%quot, 1\n");
    ir_emit("  %%quotn = or i128 %%qsh, %%qb\n");
    ir_emit("  %%inext = sub i32 %%i, 1\n");
    ir_emit("  br label %%dv.head\n");
    ir_emit("dv.done:\n");
    ir_emit("  %%rv0 = insertvalue {i128, i128} undef, i128 %%quot, 0\n");
    ir_emit("  %%rv1 = insertvalue {i128, i128} %%rv0, i128 %%rem, 1\n");
    ir_emit("  ret {i128, i128} %%rv1\n");
    ir_emit("}\n");

    /* __field_init(p, data_max): construct a %__Field on the heap,
     * validate primality (deterministic Miller-Rabin), compute Barrett m.
     * Returns ptr to a malloc'd %__Field. One-time per prime.
     *
     * data_max sets data_bytes (byte width of raw data values, for
     * wire-format literal blocks) independently of elem_bytes (byte
     * width of field elements, from the prime).  data_max == 0 means
     * "derive from p" — data_bytes = elem_bytes.
     *
     * Barrett m = floor(2^128 / p) is computed in IR via a 128-bit
     * unsigned division (udiv i128) — emitted ONCE here, not per op,
     * so the __udivti3 it may lower to is amortized over the field's
     * entire lifetime (millions of ops). Primality uses a runtime
     * Miller-Rabin helper @__is_prime_u64. */
    ir_emit("\ndefine internal ptr @__field_init(i64 %%p, i64 %%data_max) {\n");
    ir_emit("entry:\n");
    /* primality check */
    ir_emit("  %%isp = call i1 @__is_prime_u64(i64 %%p)\n");
    ir_emit("  br i1 %%isp, label %%ok, label %%bad\n");
    ir_emit("bad:\n");
    ir_emit("  call void @abort()\n");
    ir_emit("  unreachable\n");
    ir_emit("ok:\n");
    ir_emit("  %%mem = call ptr @malloc(i64 48)\n");  /* sizeof %__Field = 48 */
    /* store p */
    ir_emit("  %%pp = getelementptr %%__Field, ptr %%mem, i32 0, i32 0\n");
    ir_emit("  store i64 %%p, ptr %%pp\n");
    /* half_p = p/2 */
    ir_emit("  %%hp = udiv i64 %%p, 2\n");
    ir_emit("  %%hpp = getelementptr %%__Field, ptr %%mem, i32 0, i32 1\n");
    ir_emit("  store i64 %%hp, ptr %%hpp\n");
    /* elem_bytes: 1 if p<=256, 2 if <=65536, 4 if <=2^32, else 8 */
    ir_emit("  %%le8 = icmp ule i64 %%p, 256\n");
    ir_emit("  %%le16 = icmp ule i64 %%p, 65536\n");
    ir_emit("  %%le32 = icmp ule i64 %%p, 4294967296\n");
    ir_emit("  %%eb_a = select i1 %%le32, i32 4, i32 8\n");
    ir_emit("  %%eb_b = select i1 %%le16, i32 2, i32 %%eb_a\n");
    ir_emit("  %%eb = select i1 %%le8, i32 1, i32 %%eb_b\n");
    ir_emit("  %%ebp = getelementptr %%__Field, ptr %%mem, i32 0, i32 2\n");
    ir_emit("  store i32 %%eb, ptr %%ebp\n");
    /* data_bytes: wire-packing width (1/2/3/4) for values in
     * [0, data_max], breakpoints 255/65535/16777215.  data_max == 0
     * means "derive from the field": effective max = p - 1.
     * NOTE: distinct from elem_bytes (IR element width, 1/2/4/8) —
     * data_bytes is the codec wire-format semantic. */
    ir_emit("  %%dm_z = icmp eq i64 %%data_max, 0\n");
    ir_emit("  %%pm1d = sub i64 %%p, 1\n");
    ir_emit("  %%dm_eff = select i1 %%dm_z, i64 %%pm1d, i64 %%data_max\n");
    ir_emit("  %%dle8 = icmp ule i64 %%dm_eff, 255\n");
    ir_emit("  %%dle16 = icmp ule i64 %%dm_eff, 65535\n");
    ir_emit("  %%dle24 = icmp ule i64 %%dm_eff, 16777215\n");
    ir_emit("  %%db_a = select i1 %%dle24, i32 3, i32 4\n");
    ir_emit("  %%db_b = select i1 %%dle16, i32 2, i32 %%db_a\n");
    ir_emit("  %%db = select i1 %%dle8, i32 1, i32 %%db_b\n");
    ir_emit("  %%dbp = getelementptr %%__Field, ptr %%mem, i32 0, i32 3\n");
    ir_emit("  store i32 %%db, ptr %%dbp\n");
    /* Barrett m = floor(2^128 / p), computed in i128.
     * 2^128 doesn't fit i128; use (2^128 - 1)/p and correct:
     *   if (2^128-1) mod p == p-1, then floor(2^128/p) = that + 1. */
    ir_emit("  %%maxv = sub i128 0, 1\n");          /* 2^128 - 1 */
    ir_emit("  %%pw = zext i64 %%p to i128\n");
    ir_emit("  %%dr = call {i128, i128} @__udivrem_128(i128 %%maxv, i128 %%pw)\n");
    ir_emit("  %%mq = extractvalue {i128, i128} %%dr, 0\n");
    ir_emit("  %%mr = extractvalue {i128, i128} %%dr, 1\n");
    ir_emit("  %%pm1 = sub i128 %%pw, 1\n");
    ir_emit("  %%exact = icmp eq i128 %%mr, %%pm1\n");
    ir_emit("  %%mq1 = add i128 %%mq, 1\n");
    ir_emit("  %%m = select i1 %%exact, i128 %%mq1, i128 %%mq\n");
    /* split m into lo/hi u64 halves */
    ir_emit("  %%mlo = trunc i128 %%m to i64\n");
    ir_emit("  %%mhi128 = lshr i128 %%m, 64\n");
    ir_emit("  %%mhi = trunc i128 %%mhi128 to i64\n");
    ir_emit("  %%mlop = getelementptr %%__Field, ptr %%mem, i32 0, i32 4\n");
    ir_emit("  store i64 %%mlo, ptr %%mlop\n");
    ir_emit("  %%mhip = getelementptr %%__Field, ptr %%mem, i32 0, i32 5\n");
    ir_emit("  store i64 %%mhi, ptr %%mhip\n");
    ir_emit("  ret ptr %%mem\n");
    ir_emit("}\n");

    /* __is_prime_u64: deterministic Miller-Rabin for all u64.
     * Witnesses {2,3,5,7,11,13,17,19,23,29,31,37} suffice for < 2^64. */
    ir_emit("\ndefine internal i1 @__is_prime_u64(i64 %%n) {\n");
    ir_emit("entry:\n");
    ir_emit("  %%lt2 = icmp ult i64 %%n, 2\n");
    ir_emit("  br i1 %%lt2, label %%notprime, label %%chk2\n");
    ir_emit("chk2:\n");
    ir_emit("  %%eq2 = icmp eq i64 %%n, 2\n");
    ir_emit("  br i1 %%eq2, label %%isprime, label %%chkeven\n");
    ir_emit("chkeven:\n");
    ir_emit("  %%lowbit = and i64 %%n, 1\n");
    ir_emit("  %%even = icmp eq i64 %%lowbit, 0\n");
    ir_emit("  br i1 %%even, label %%notprime, label %%decomp\n");
    /* write n-1 = d * 2^r */
    ir_emit("decomp:\n");
    ir_emit("  %%nm1 = sub i64 %%n, 1\n");
    ir_emit("  br label %%dloop\n");
    ir_emit("dloop:\n");
    ir_emit("  %%d = phi i64 [%%nm1, %%decomp], [%%dnext, %%dbody]\n");
    ir_emit("  %%r = phi i64 [0, %%decomp], [%%rnext, %%dbody]\n");
    ir_emit("  %%dodd = and i64 %%d, 1\n");
    ir_emit("  %%disodd = icmp ne i64 %%dodd, 0\n");
    ir_emit("  br i1 %%disodd, label %%witinit, label %%dbody\n");
    ir_emit("dbody:\n");
    ir_emit("  %%dnext = lshr i64 %%d, 1\n");
    ir_emit("  %%rnext = add i64 %%r, 1\n");
    ir_emit("  br label %%dloop\n");
    /* iterate the 12 witnesses */
    ir_emit("witinit:\n");
    ir_emit("  br label %%witloop\n");
    ir_emit("witloop:\n");
    ir_emit("  %%wi = phi i64 [0, %%witinit], [%%winext, %%witcont]\n");
    ir_emit("  %%wdone = icmp uge i64 %%wi, 12\n");
    ir_emit("  br i1 %%wdone, label %%isprime, label %%witbody\n");
    ir_emit("witbody:\n");
    ir_emit("  %%wgep = getelementptr [12 x i64], ptr @__mr_witnesses, i64 0, i64 %%wi\n");
    ir_emit("  %%a = load i64, ptr %%wgep\n");
    /* if a >= n, skip (a mod n could be 0); witness a%n==0 => skip */
    ir_emit("  %%amod = urem i64 %%a, %%n\n");
    ir_emit("  %%askip = icmp eq i64 %%amod, 0\n");
    ir_emit("  br i1 %%askip, label %%witcont, label %%mrtest\n");
    ir_emit("mrtest:\n");
    /* x = a^d mod n */
    ir_emit("  %%x0 = call i64 @__mr_powmod(i64 %%amod, i64 %%d, i64 %%n)\n");
    ir_emit("  %%x0eq1 = icmp eq i64 %%x0, 1\n");
    ir_emit("  %%x0eqnm1 = icmp eq i64 %%x0, %%nm1\n");
    ir_emit("  %%x0pass = or i1 %%x0eq1, %%x0eqnm1\n");
    ir_emit("  br i1 %%x0pass, label %%witcont, label %%sqloop\n");
    /* square r-1 times, looking for n-1 */
    ir_emit("sqloop:\n");
    ir_emit("  %%x = phi i64 [%%x0, %%mrtest], [%%xsq, %%sqbody]\n");
    ir_emit("  %%si = phi i64 [0, %%mrtest], [%%sinext, %%sqbody]\n");
    ir_emit("  %%rm1 = sub i64 %%r, 1\n");
    ir_emit("  %%sdone = icmp uge i64 %%si, %%rm1\n");
    ir_emit("  br i1 %%sdone, label %%notprime, label %%sqbody\n");
    ir_emit("sqbody:\n");
    ir_emit("  %%xsq = call i64 @__mr_powmod(i64 %%x, i64 2, i64 %%n)\n");
    ir_emit("  %%xsqnm1 = icmp eq i64 %%xsq, %%nm1\n");
    ir_emit("  %%sinext = add i64 %%si, 1\n");
    ir_emit("  br i1 %%xsqnm1, label %%witcont, label %%sqloop\n");
    ir_emit("witcont:\n");
    ir_emit("  %%winext = add i64 %%wi, 1\n");
    ir_emit("  br label %%witloop\n");
    ir_emit("isprime:\n");
    ir_emit("  ret i1 1\n");
    ir_emit("notprime:\n");
    ir_emit("  ret i1 0\n");
    ir_emit("}\n");

    /* __mr_powmod(base, exp, mod): modular exponentiation in u64 using
     * i128 intermediate (no overflow for mod < 2^63). */
    ir_emit("\ndefine internal i64 @__mr_powmod(i64 %%base_in, i64 %%exp_in, i64 %%mod) {\n");
    ir_emit("entry:\n");
    ir_emit("  %%b0 = urem i64 %%base_in, %%mod\n");
    ir_emit("  br label %%loop\n");
    ir_emit("loop:\n");
    ir_emit("  %%result = phi i64 [1, %%entry], [%%result.next, %%loop.end]\n");
    ir_emit("  %%base = phi i64 [%%b0, %%entry], [%%base.next, %%loop.end]\n");
    ir_emit("  %%exp = phi i64 [%%exp_in, %%entry], [%%exp.next, %%loop.end]\n");
    ir_emit("  %%done = icmp eq i64 %%exp, 0\n");
    ir_emit("  br i1 %%done, label %%exit, label %%body\n");
    ir_emit("body:\n");
    ir_emit("  %%odd = and i64 %%exp, 1\n");
    ir_emit("  %%is_odd = icmp ne i64 %%odd, 0\n");
    /* result = (result * base) % mod via i128 */
    ir_emit("  %%rw = zext i64 %%result to i128\n");
    ir_emit("  %%bw = zext i64 %%base to i128\n");
    ir_emit("  %%modw = zext i64 %%mod to i128\n");
    ir_emit("  %%rb = mul i128 %%rw, %%bw\n");
    ir_emit("  %%rbmd = call {i128, i128} @__udivrem_128(i128 %%rb, i128 %%modw)\n");
    ir_emit("  %%rbm = extractvalue {i128, i128} %%rbmd, 1\n");
    ir_emit("  %%rbm64 = trunc i128 %%rbm to i64\n");
    ir_emit("  %%result.maybe = select i1 %%is_odd, i64 %%rbm64, i64 %%result\n");
    /* base = (base * base) % mod */
    ir_emit("  %%bb = mul i128 %%bw, %%bw\n");
    ir_emit("  %%bbmd = call {i128, i128} @__udivrem_128(i128 %%bb, i128 %%modw)\n");
    ir_emit("  %%bbm = extractvalue {i128, i128} %%bbmd, 1\n");
    ir_emit("  %%base.sq = trunc i128 %%bbm to i64\n");
    ir_emit("  %%exp.shr = lshr i64 %%exp, 1\n");
    ir_emit("  br label %%loop.end\n");
    ir_emit("loop.end:\n");
    ir_emit("  %%result.next = phi i64 [%%result.maybe, %%body]\n");
    ir_emit("  %%base.next = phi i64 [%%base.sq, %%body]\n");
    ir_emit("  %%exp.next = phi i64 [%%exp.shr, %%body]\n");
    ir_emit("  br label %%loop\n");
    ir_emit("exit:\n");
    ir_emit("  ret i64 %%result\n");
    ir_emit("}\n");
}

/* ============================================================
 * Poly conversion functions: @std_to_newton_F{p}, @newton_to_std_F{p}
 * ============================================================ */

/* ============================================================
 * Extension field arithmetic: ExtField<F, 2>
 *
 * Element = {a, b} representing a + b*i where i^2 = nr.
 * All operations decompose into base field operations.
 * ============================================================ */

static void emit_extfield_funcs(FieldInfo *ext_fi) {
    uint64_t p = ext_fi->prime;
    uint64_t nr = ext_fi->nonresidue;
    const char *el = ext_fi->elem_ir;      /* base element: "i64" */
    const char *et = ext_fi->ext_elem_ir;  /* pair: "{i64, i64}" */
    unsigned long long pu = (unsigned long long)p;
    char base_fn[64];
    snprintf(base_fn, sizeof(base_fn), "field%llu", pu);

    /* ---- ext_add: {a0+b0, a1+b1} — 2 base adds ---- */
    ir_emit("\ndefine internal %s @ext%llu_add(%s %%a, %s %%b) alwaysinline {\n", et, pu, et, et);
    ir_emit("  %%a0 = extractvalue %s %%a, 0\n", et);
    ir_emit("  %%a1 = extractvalue %s %%a, 1\n", et);
    ir_emit("  %%b0 = extractvalue %s %%b, 0\n", et);
    ir_emit("  %%b1 = extractvalue %s %%b, 1\n", et);
    ir_emit("  %%r0 = call %s @%s_add(%s %%a0, %s %%b0)\n", el, base_fn, el, el);
    ir_emit("  %%r1 = call %s @%s_add(%s %%a1, %s %%b1)\n", el, base_fn, el, el);
    ir_emit("  %%s0 = insertvalue %s undef, %s %%r0, 0\n", et, el);
    ir_emit("  %%s1 = insertvalue %s %%s0, %s %%r1, 1\n", et, el);
    ir_emit("  ret %s %%s1\n", et);
    ir_emit("}\n");

    /* ---- ext_sub: {a0-b0, a1-b1} — 2 base subs ---- */
    ir_emit("\ndefine internal %s @ext%llu_sub(%s %%a, %s %%b) alwaysinline {\n", et, pu, et, et);
    ir_emit("  %%a0 = extractvalue %s %%a, 0\n", et);
    ir_emit("  %%a1 = extractvalue %s %%a, 1\n", et);
    ir_emit("  %%b0 = extractvalue %s %%b, 0\n", et);
    ir_emit("  %%b1 = extractvalue %s %%b, 1\n", et);
    ir_emit("  %%r0 = call %s @%s_sub(%s %%a0, %s %%b0)\n", el, base_fn, el, el);
    ir_emit("  %%r1 = call %s @%s_sub(%s %%a1, %s %%b1)\n", el, base_fn, el, el);
    ir_emit("  %%s0 = insertvalue %s undef, %s %%r0, 0\n", et, el);
    ir_emit("  %%s1 = insertvalue %s %%s0, %s %%r1, 1\n", et, el);
    ir_emit("  ret %s %%s1\n", et);
    ir_emit("}\n");

    /* ---- ext_mul: {a0*b0 + a1*b1*nr, a0*b1 + a1*b0} — 4 base mul + 2 base add ---- */
    ir_emit("\ndefine internal %s @ext%llu_mul(%s %%a, %s %%b) alwaysinline {\n", et, pu, et, et);
    ir_emit("  %%a0 = extractvalue %s %%a, 0\n", et);
    ir_emit("  %%a1 = extractvalue %s %%a, 1\n", et);
    ir_emit("  %%b0 = extractvalue %s %%b, 0\n", et);
    ir_emit("  %%b1 = extractvalue %s %%b, 1\n", et);
    /* r0 = a0*b0 + a1*b1*nr */
    ir_emit("  %%t00 = call %s @%s_mul(%s %%a0, %s %%b0)\n", el, base_fn, el, el);
    ir_emit("  %%t11 = call %s @%s_mul(%s %%a1, %s %%b1)\n", el, base_fn, el, el);
    ir_emit("  %%nr = add %s 0, %llu\n", el, (unsigned long long)nr);
    ir_emit("  %%tnr = call %s @%s_mul(%s %%t11, %s %%nr)\n", el, base_fn, el, el);
    ir_emit("  %%r0 = call %s @%s_add(%s %%t00, %s %%tnr)\n", el, base_fn, el, el);
    /* r1 = a0*b1 + a1*b0 */
    ir_emit("  %%t01 = call %s @%s_mul(%s %%a0, %s %%b1)\n", el, base_fn, el, el);
    ir_emit("  %%t10 = call %s @%s_mul(%s %%a1, %s %%b0)\n", el, base_fn, el, el);
    ir_emit("  %%r1 = call %s @%s_add(%s %%t01, %s %%t10)\n", el, base_fn, el, el);
    ir_emit("  %%s0 = insertvalue %s undef, %s %%r0, 0\n", et, el);
    ir_emit("  %%s1 = insertvalue %s %%s0, %s %%r1, 1\n", et, el);
    ir_emit("  ret %s %%s1\n", et);
    ir_emit("}\n");

    /* ---- ext_pow: repeated squaring using ext_mul ---- */
    const char *ety = (ext_fi->elem_bits == 64) ? "i64" : "i32";
    ir_emit("\ndefine internal %s @ext%llu_pow(%s %%base_in, %s %%exp_in) alwaysinline {\n",
            et, pu, et, ety);
    ir_emit("entry:\n");
    /* identity = {1, 0} */
    ir_emit("  %%id0 = insertvalue %s undef, %s 1, 0\n", et, el);
    ir_emit("  %%id1 = insertvalue %s %%id0, %s 0, 1\n", et, el);
    ir_emit("  br label %%loop\n");
    ir_emit("loop:\n");
    ir_emit("  %%result = phi %s [%%id1, %%entry], [%%result.next, %%loop.end]\n", et);
    ir_emit("  %%base = phi %s [%%base_in, %%entry], [%%base.next, %%loop.end]\n", et);
    ir_emit("  %%exp = phi %s [%%exp_in, %%entry], [%%exp.next, %%loop.end]\n", ety);
    ir_emit("  %%done = icmp eq %s %%exp, 0\n", ety);
    ir_emit("  br i1 %%done, label %%exit, label %%body\n");
    ir_emit("body:\n");
    ir_emit("  %%odd = and %s %%exp, 1\n", ety);
    ir_emit("  %%is_odd = icmp ne %s %%odd, 0\n", ety);
    ir_emit("  %%mul_res = call %s @ext%llu_mul(%s %%result, %s %%base)\n", et, pu, et, et);
    ir_emit("  %%result.maybe = select i1 %%is_odd, %s %%mul_res, %s %%result\n", et, et);
    ir_emit("  %%base.sq = call %s @ext%llu_mul(%s %%base, %s %%base)\n", et, pu, et, et);
    ir_emit("  %%exp.shr = lshr %s %%exp, 1\n", ety);
    ir_emit("  br label %%loop.end\n");
    ir_emit("loop.end:\n");
    ir_emit("  %%result.next = phi %s [%%result.maybe, %%body]\n", et);
    ir_emit("  %%base.next = phi %s [%%base.sq, %%body]\n", et);
    ir_emit("  %%exp.next = phi %s [%%exp.shr, %%body]\n", ety);
    ir_emit("  br label %%loop\n");
    ir_emit("exit:\n");
    ir_emit("  ret %s %%result\n", et);
    ir_emit("}\n");

    /* ---- ext_inv: conjugate formula ----
     * inv(a + bi) = (a - bi) / norm, where norm = a^2 - b^2 * nr
     * Components: r0 = a * inv(norm), r1 = (p - b) * inv(norm) */
    ir_emit("\ndefine internal %s @ext%llu_inv(%s %%a) alwaysinline {\n", et, pu, et);
    ir_emit("  %%a0 = extractvalue %s %%a, 0\n", et);
    ir_emit("  %%a1 = extractvalue %s %%a, 1\n", et);
    /* norm = a0^2 - a1^2 * nr  (in base field) */
    ir_emit("  %%a0sq = call %s @%s_mul(%s %%a0, %s %%a0)\n", el, base_fn, el, el);
    ir_emit("  %%a1sq = call %s @%s_mul(%s %%a1, %s %%a1)\n", el, base_fn, el, el);
    ir_emit("  %%nr.c = add %s 0, %llu\n", el, (unsigned long long)nr);
    ir_emit("  %%a1nr = call %s @%s_mul(%s %%a1sq, %s %%nr.c)\n", el, base_fn, el, el);
    ir_emit("  %%norm = call %s @%s_sub(%s %%a0sq, %s %%a1nr)\n", el, base_fn, el, el);
    /* inv_norm = base_inv(norm) */
    ir_emit("  %%inv_norm = call %s @%s_inv(%s %%norm)\n", el, base_fn, el);
    /* r0 = a0 * inv_norm */
    ir_emit("  %%r0 = call %s @%s_mul(%s %%a0, %s %%inv_norm)\n", el, base_fn, el, el);
    /* r1 = (p - a1) * inv_norm = neg(a1) * inv_norm */
    ir_emit("  %%zero = add %s 0, 0\n", el);
    ir_emit("  %%neg_a1 = call %s @%s_sub(%s %%zero, %s %%a1)\n", el, base_fn, el, el);
    ir_emit("  %%r1 = call %s @%s_mul(%s %%neg_a1, %s %%inv_norm)\n", el, base_fn, el, el);
    ir_emit("  %%s0 = insertvalue %s undef, %s %%r0, 0\n", et, el);
    ir_emit("  %%s1 = insertvalue %s %%s0, %s %%r1, 1\n", et, el);
    ir_emit("  ret %s %%s1\n", et);
    ir_emit("}\n");

    /* ---- ext_div: a * inv(b) ---- */
    ir_emit("\ndefine internal %s @ext%llu_div(%s %%a, %s %%b) alwaysinline {\n", et, pu, et, et);
    ir_emit("  %%inv_b = call %s @ext%llu_inv(%s %%b)\n", et, pu, et);
    ir_emit("  %%r = call %s @ext%llu_mul(%s %%a, %s %%inv_b)\n", et, pu, et, et);
    ir_emit("  ret %s %%r\n", et);
    ir_emit("}\n");
}

static uint64_t g_poly_conv_emitted_primes[MAX_FIELDS];
static int g_poly_conv_emitted_count = 0;

/* Emit the two O(d^2) conversion functions for an arbitrary prime field.
 * Called once per field from codegen_program after emit_field_funcs. */
static void emit_poly_conv_funcs(FieldInfo *f) {
    /* Check if already emitted for this prime */
    for (int i = 0; i < g_poly_conv_emitted_count; i++) {
        if (g_poly_conv_emitted_primes[i] == f->prime) return;
    }
    g_poly_conv_emitted_primes[g_poly_conv_emitted_count++] = f->prime;

    const char *el = f->elem_ir;      /* "i8", "i16", "i32" */
    const char *wi = f->wide_ir;      /* "i16", "i32", "i64" */
    uint64_t prime = f->prime;
    int buf_bytes = 32 * (f->elem_bits / 8);
    unsigned long long P = (unsigned long long)prime;

    /* -------------------------------------------------------
     * @std_to_newton_F{p}(ptr %in, ptr %out, i32 %d)
     *
     * Algorithm:
     *   work[t] = eval standard poly at t=0..d
     *   then iterated forward differences -> out[0..d]
     * ------------------------------------------------------- */
    ir_emit("\ndefine internal void @std_to_newton_F%llu(ptr %%in, ptr %%out, i32 %%d) {\n", P);
    ir_emit("entry:\n");
    ir_emit("  %%work = alloca [32 x %s]\n", el);
    ir_emit("  call void @llvm.memset.p0.i64(ptr %%work, i8 0, i64 %d, i1 false)\n", buf_bytes);
    ir_emit("  %%t_alloca = alloca i32\n");
    ir_emit("  store i32 0, ptr %%t_alloca\n");
    ir_emit("  br label %%sn_outer_cond\n");
    ir_emit("sn_outer_cond:\n");
    ir_emit("  %%t_cur = load i32, ptr %%t_alloca\n");
    ir_emit("  %%d_p1 = add i32 %%d, 1\n");
    ir_emit("  %%t_done = icmp sge i32 %%t_cur, %%d_p1\n");
    ir_emit("  br i1 %%t_done, label %%sn_diff_init, label %%sn_outer_body\n");
    ir_emit("sn_outer_body:\n");
    ir_emit("  %%v_alloca = alloca %s\n", el);
    ir_emit("  store %s 0, ptr %%v_alloca\n", el);
    ir_emit("  %%tp_alloca = alloca %s\n", el);
    ir_emit("  store %s 1, ptr %%tp_alloca\n", el);
    /* t as field element: cast i32 loop counter -> wide type -> urem -> elem type */
    if (f->elem_bits <= 16) {
        /* i32 is wider than or equal to wide_ir; trunc (or identity for i32→i32) */
        if (f->elem_bits == 8)
            ir_emit("  %%t_wide = trunc i32 %%t_cur to %s\n", wi);
        else
            ir_emit("  %%t_wide = and i32 %%t_cur, 4294967295\n");  /* identity for i32 */
    } else {
        ir_emit("  %%t_wide = sext i32 %%t_cur to %s\n", wi);
    }
    ir_emit("  %%t_mod = urem %s %%t_wide, %llu\n", wi, P);
    if (f->elem_bits < 32 && f->elem_bits != 16)
        ir_emit("  %%t_f = trunc %s %%t_mod to %s\n", wi, el);
    else if (f->elem_bits == 16)
        ir_emit("  %%t_f = trunc %s %%t_mod to %s\n", wi, el);
    else
        ir_emit("  %%t_f = trunc %s %%t_mod to %s\n", wi, el);
    /* inner loop k = 0..d */
    ir_emit("  %%k_alloca = alloca i32\n");
    ir_emit("  store i32 0, ptr %%k_alloca\n");
    ir_emit("  br label %%sn_inner_cond\n");
    ir_emit("sn_inner_cond:\n");
    ir_emit("  %%k_cur = load i32, ptr %%k_alloca\n");
    ir_emit("  %%d_p1b = add i32 %%d, 1\n");
    ir_emit("  %%k_done = icmp sge i32 %%k_cur, %%d_p1b\n");
    ir_emit("  br i1 %%k_done, label %%sn_inner_end, label %%sn_inner_body\n");
    ir_emit("sn_inner_body:\n");
    ir_emit("  %%k64 = sext i32 %%k_cur to i64\n");
    ir_emit("  %%in_k_ptr = getelementptr [32 x %s], ptr %%in, i64 0, i64 %%k64\n", el);
    ir_emit("  %%in_k = load %s, ptr %%in_k_ptr\n", el);
    ir_emit("  %%tp_cur = load %s, ptr %%tp_alloca\n", el);
    ir_emit("  %%term = call %s @field%llu_mul(%s %%in_k, %s %%tp_cur)\n", el, P, el, el);
    ir_emit("  %%v_cur = load %s, ptr %%v_alloca\n", el);
    ir_emit("  %%v_new = call %s @field%llu_add(%s %%v_cur, %s %%term)\n", el, P, el, el);
    ir_emit("  store %s %%v_new, ptr %%v_alloca\n", el);
    ir_emit("  %%tp_new = call %s @field%llu_mul(%s %%tp_cur, %s %%t_f)\n", el, P, el, el);
    ir_emit("  store %s %%tp_new, ptr %%tp_alloca\n", el);
    ir_emit("  %%k_next = add i32 %%k_cur, 1\n");
    ir_emit("  store i32 %%k_next, ptr %%k_alloca\n");
    ir_emit("  br label %%sn_inner_cond\n");
    ir_emit("sn_inner_end:\n");
    ir_emit("  %%t64b = sext i32 %%t_cur to i64\n");
    ir_emit("  %%work_t_ptr = getelementptr [32 x %s], ptr %%work, i64 0, i64 %%t64b\n", el);
    ir_emit("  %%v_final = load %s, ptr %%v_alloca\n", el);
    ir_emit("  store %s %%v_final, ptr %%work_t_ptr\n", el);
    ir_emit("  %%t_next = add i32 %%t_cur, 1\n");
    ir_emit("  store i32 %%t_next, ptr %%t_alloca\n");
    ir_emit("  br label %%sn_outer_cond\n");
    /* Iterated forward differences */
    ir_emit("sn_diff_init:\n");
    ir_emit("  %%lv_alloca = alloca i32\n");
    ir_emit("  store i32 0, ptr %%lv_alloca\n");
    ir_emit("  br label %%sn_lv_cond\n");
    ir_emit("sn_lv_cond:\n");
    ir_emit("  %%lv_cur = load i32, ptr %%lv_alloca\n");
    ir_emit("  %%d_p1c = add i32 %%d, 1\n");
    ir_emit("  %%lv_done = icmp sge i32 %%lv_cur, %%d_p1c\n");
    ir_emit("  br i1 %%lv_done, label %%sn_exit, label %%sn_lv_body\n");
    ir_emit("sn_lv_body:\n");
    ir_emit("  %%work0_ptr = getelementptr [32 x %s], ptr %%work, i64 0, i64 0\n", el);
    ir_emit("  %%work0 = load %s, ptr %%work0_ptr\n", el);
    ir_emit("  %%lv64 = sext i32 %%lv_cur to i64\n");
    ir_emit("  %%out_lv_ptr = getelementptr [32 x %s], ptr %%out, i64 0, i64 %%lv64\n", el);
    ir_emit("  store %s %%work0, ptr %%out_lv_ptr\n", el);
    ir_emit("  %%di_alloca = alloca i32\n");
    ir_emit("  store i32 0, ptr %%di_alloca\n");
    ir_emit("  br label %%sn_di_cond\n");
    ir_emit("sn_di_cond:\n");
    ir_emit("  %%di_cur = load i32, ptr %%di_alloca\n");
    ir_emit("  %%lv_cur2 = load i32, ptr %%lv_alloca\n");
    ir_emit("  %%di_lim = sub i32 %%d, %%lv_cur2\n");
    ir_emit("  %%di_done = icmp sge i32 %%di_cur, %%di_lim\n");
    ir_emit("  br i1 %%di_done, label %%sn_di_end, label %%sn_di_body\n");
    ir_emit("sn_di_body:\n");
    ir_emit("  %%di64 = sext i32 %%di_cur to i64\n");
    ir_emit("  %%di64p1 = add i64 %%di64, 1\n");
    ir_emit("  %%wi_ptr = getelementptr [32 x %s], ptr %%work, i64 0, i64 %%di64\n", el);
    ir_emit("  %%wi1_ptr = getelementptr [32 x %s], ptr %%work, i64 0, i64 %%di64p1\n", el);
    ir_emit("  %%wi = load %s, ptr %%wi_ptr\n", el);
    ir_emit("  %%wi1 = load %s, ptr %%wi1_ptr\n", el);
    ir_emit("  %%wi_new = call %s @field%llu_sub(%s %%wi1, %s %%wi)\n", el, P, el, el);
    ir_emit("  store %s %%wi_new, ptr %%wi_ptr\n", el);
    ir_emit("  %%di_next = add i32 %%di_cur, 1\n");
    ir_emit("  store i32 %%di_next, ptr %%di_alloca\n");
    ir_emit("  br label %%sn_di_cond\n");
    ir_emit("sn_di_end:\n");
    ir_emit("  %%lv_next = add i32 %%lv_cur, 1\n");
    ir_emit("  store i32 %%lv_next, ptr %%lv_alloca\n");
    ir_emit("  br label %%sn_lv_cond\n");
    ir_emit("sn_exit:\n");
    ir_emit("  ret void\n");
    ir_emit("}\n");

    /* -------------------------------------------------------
     * @newton_to_std_F251(ptr %in, ptr %out, i32 %d)
     *
     * Correct algorithm for UNDIVIDED Newton forward differences
     * (the kind produced by analyze()).
     *
     * The polynomial is f(x) = Σ_k c_k * C(x,k)
     * where C(x,k) = x*(x-1)*...*(x-k+1)/k!  (binomial coefficient).
     *
     * We accumulate the standard form by maintaining a running
     * product poly (the standard form of C(x,k)) and adding
     * c_k * product to out at each step.
     *
     * product[0..d] starts as [1, 0, ...] (C(x,0) = 1)
     * At each step k=0..d:
     *   for j=0..k: out[j] += c_k * product[j]
     *   Update product → C(x, k+1) = C(x, k) * (x - k) / (k+1):
     *     new_product[k+1] = product[k]
     *     for j=k downto 1: new_product[j] = product[j-1] - k*product[j]
     *     new_product[0] = -k * product[0]
     *     divide all by (k+1)
     *
     * ------------------------------------------------------- */
    ir_emit("\ndefine internal void @newton_to_std_F%llu(ptr %%in, ptr %%out, i32 %%d) {\n", P);
    ir_emit("entry:\n");
    /* #35: zero exactly the caller's (d+1) elements — the fixed 32-elem
     * memset smashed past the caller's [ncoeffs x elem] conv alloca
     * (llc-layout-dependent neighbor corruption). Mirrors tvc_self. */
    ir_emit("  %%oz_n = add i32 %%d, 1\n");
    ir_emit("  %%oz_64 = sext i32 %%oz_n to i64\n");
    ir_emit("  %%oz_b = mul i64 %%oz_64, %d\n", f->elem_bits / 8);
    ir_emit("  call void @llvm.memset.p0.i64(ptr %%out, i8 0, i64 %%oz_b, i1 false)\n");
    ir_emit("  %%product = alloca [32 x %s]\n", el);
    ir_emit("  call void @llvm.memset.p0.i64(ptr %%product, i8 0, i64 %d, i1 false)\n", buf_bytes);
    ir_emit("  %%p0_ptr = getelementptr [32 x %s], ptr %%product, i64 0, i64 0\n", el);
    ir_emit("  store %s 1, ptr %%p0_ptr\n", el);
    ir_emit("  %%k_ns_alloca = alloca i32\n");
    ir_emit("  store i32 0, ptr %%k_ns_alloca\n");
    ir_emit("  br label %%ns_k_cond\n");
    ir_emit("ns_k_cond:\n");
    ir_emit("  %%k_ns = load i32, ptr %%k_ns_alloca\n");
    ir_emit("  %%d_p1_ns = add i32 %%d, 1\n");
    ir_emit("  %%k_done_ns = icmp sge i32 %%k_ns, %%d_p1_ns\n");
    ir_emit("  br i1 %%k_done_ns, label %%ns_exit, label %%ns_k_body\n");
    ir_emit("ns_k_body:\n");
    ir_emit("  %%k_ns64 = sext i32 %%k_ns to i64\n");
    ir_emit("  %%ck_ptr = getelementptr [32 x %s], ptr %%in, i64 0, i64 %%k_ns64\n", el);
    ir_emit("  %%ck = load %s, ptr %%ck_ptr\n", el);
    ir_emit("  %%j_add_alloca = alloca i32\n");
    ir_emit("  store i32 0, ptr %%j_add_alloca\n");
    ir_emit("  br label %%ns_jadd_cond\n");
    ir_emit("ns_jadd_cond:\n");
    ir_emit("  %%j_add = load i32, ptr %%j_add_alloca\n");
    ir_emit("  %%k_ns2 = load i32, ptr %%k_ns_alloca\n");
    ir_emit("  %%jadd_lim = add i32 %%k_ns2, 1\n");
    ir_emit("  %%jadd_done = icmp sge i32 %%j_add, %%jadd_lim\n");
    ir_emit("  br i1 %%jadd_done, label %%ns_jadd_end, label %%ns_jadd_body\n");
    ir_emit("ns_jadd_body:\n");
    ir_emit("  %%jadd64 = sext i32 %%j_add to i64\n");
    ir_emit("  %%prod_j_ptr = getelementptr [32 x %s], ptr %%product, i64 0, i64 %%jadd64\n", el);
    ir_emit("  %%out_j_ptr = getelementptr [32 x %s], ptr %%out, i64 0, i64 %%jadd64\n", el);
    ir_emit("  %%prod_j = load %s, ptr %%prod_j_ptr\n", el);
    ir_emit("  %%out_j = load %s, ptr %%out_j_ptr\n", el);
    ir_emit("  %%ck_pj = call %s @field%llu_mul(%s %%ck, %s %%prod_j)\n", el, P, el, el);
    ir_emit("  %%out_j_new = call %s @field%llu_add(%s %%out_j, %s %%ck_pj)\n", el, P, el, el);
    ir_emit("  store %s %%out_j_new, ptr %%out_j_ptr\n", el);
    ir_emit("  %%jadd_next = add i32 %%j_add, 1\n");
    ir_emit("  store i32 %%jadd_next, ptr %%j_add_alloca\n");
    ir_emit("  br label %%ns_jadd_cond\n");
    ir_emit("ns_jadd_end:\n");
    ir_emit("  %%new_prod = alloca [32 x %s]\n", el);
    ir_emit("  call void @llvm.memset.p0.i64(ptr %%new_prod, i8 0, i64 %d, i1 false)\n", buf_bytes);
    /* k_f = k as field element: i32 -> wide -> urem -> elem */
    ir_emit("  %%k_ns3 = load i32, ptr %%k_ns_alloca\n");
    if (f->elem_bits == 8) {
        ir_emit("  %%kf_wide = trunc i32 %%k_ns3 to %s\n", wi);
    } else if (f->elem_bits == 16) {
        ir_emit("  %%kf_wide = and i32 %%k_ns3, 4294967295\n");
    } else {
        ir_emit("  %%kf_wide = sext i32 %%k_ns3 to %s\n", wi);
    }
    ir_emit("  %%kf_mod = urem %s %%kf_wide, %llu\n", wi, P);
    ir_emit("  %%kf = trunc %s %%kf_mod to %s\n", wi, el);
    /* new_prod[k+1] = product[k] */
    ir_emit("  %%k_ns3_64 = sext i32 %%k_ns3 to i64\n");
    ir_emit("  %%prod_k_ptr = getelementptr [32 x %s], ptr %%product, i64 0, i64 %%k_ns3_64\n", el);
    ir_emit("  %%prod_k = load %s, ptr %%prod_k_ptr\n", el);
    ir_emit("  %%kp1_64 = add i64 %%k_ns3_64, 1\n");
    ir_emit("  %%np_kp1_ptr = getelementptr [32 x %s], ptr %%new_prod, i64 0, i64 %%kp1_64\n", el);
    ir_emit("  store %s %%prod_k, ptr %%np_kp1_ptr\n", el);
    /* for j = k downto 1: new_prod[j] = prod[j-1] - k*prod[j] */
    ir_emit("  %%jmul_alloca = alloca i32\n");
    ir_emit("  store i32 %%k_ns3, ptr %%jmul_alloca\n");
    ir_emit("  br label %%ns_jmul_cond\n");
    ir_emit("ns_jmul_cond:\n");
    ir_emit("  %%jmul = load i32, ptr %%jmul_alloca\n");
    ir_emit("  %%jmul_done = icmp slt i32 %%jmul, 1\n");
    ir_emit("  br i1 %%jmul_done, label %%ns_jmul_end, label %%ns_jmul_body\n");
    ir_emit("ns_jmul_body:\n");
    ir_emit("  %%jmul64 = sext i32 %%jmul to i64\n");
    ir_emit("  %%jmul_m1 = sub i64 %%jmul64, 1\n");
    ir_emit("  %%pj_ptr = getelementptr [32 x %s], ptr %%product, i64 0, i64 %%jmul64\n", el);
    ir_emit("  %%pjm1_ptr = getelementptr [32 x %s], ptr %%product, i64 0, i64 %%jmul_m1\n", el);
    ir_emit("  %%pj = load %s, ptr %%pj_ptr\n", el);
    ir_emit("  %%pjm1 = load %s, ptr %%pjm1_ptr\n", el);
    ir_emit("  %%k_pj = call %s @field%llu_mul(%s %%kf, %s %%pj)\n", el, P, el, el);
    ir_emit("  %%new_pj = call %s @field%llu_sub(%s %%pjm1, %s %%k_pj)\n", el, P, el, el);
    ir_emit("  %%np_j_ptr = getelementptr [32 x %s], ptr %%new_prod, i64 0, i64 %%jmul64\n", el);
    ir_emit("  store %s %%new_pj, ptr %%np_j_ptr\n", el);
    ir_emit("  %%jmul_next = sub i32 %%jmul, 1\n");
    ir_emit("  store i32 %%jmul_next, ptr %%jmul_alloca\n");
    ir_emit("  br label %%ns_jmul_cond\n");
    ir_emit("ns_jmul_end:\n");
    /* new_prod[0] = -k * product[0] */
    ir_emit("  %%p0_r_ptr = getelementptr [32 x %s], ptr %%product, i64 0, i64 0\n", el);
    ir_emit("  %%p0_r = load %s, ptr %%p0_r_ptr\n", el);
    ir_emit("  %%k_p0 = call %s @field%llu_mul(%s %%kf, %s %%p0_r)\n", el, P, el, el);
    ir_emit("  %%zero_el = add %s 0, 0\n", el);
    ir_emit("  %%neg_k_p0 = call %s @field%llu_sub(%s %%zero_el, %s %%k_p0)\n", el, P, el, el);
    ir_emit("  %%np0_ptr = getelementptr [32 x %s], ptr %%new_prod, i64 0, i64 0\n", el);
    ir_emit("  store %s %%neg_k_p0, ptr %%np0_ptr\n", el);
    /* Divide all by (k+1): inv_kp1 = inv(k+1) */
    ir_emit("  %%k_ns4 = load i32, ptr %%k_ns_alloca\n");
    ir_emit("  %%kp1_i32 = add i32 %%k_ns4, 1\n");
    if (f->elem_bits == 8) {
        ir_emit("  %%kp1_wide = trunc i32 %%kp1_i32 to %s\n", wi);
    } else if (f->elem_bits == 16) {
        ir_emit("  %%kp1_wide = and i32 %%kp1_i32, 4294967295\n");
    } else {
        ir_emit("  %%kp1_wide = sext i32 %%kp1_i32 to %s\n", wi);
    }
    ir_emit("  %%kp1_mod = urem %s %%kp1_wide, %llu\n", wi, P);
    ir_emit("  %%kp1_f = trunc %s %%kp1_mod to %s\n", wi, el);
    ir_emit("  %%inv_kp1 = call %s @field%llu_inv(%s %%kp1_f)\n", el, P, el);
    /* for j=0..k+1: new_prod[j] *= inv_kp1 */
    ir_emit("  %%jdiv_alloca = alloca i32\n");
    ir_emit("  store i32 0, ptr %%jdiv_alloca\n");
    ir_emit("  br label %%ns_jdiv_cond\n");
    ir_emit("ns_jdiv_cond:\n");
    ir_emit("  %%jdiv = load i32, ptr %%jdiv_alloca\n");
    ir_emit("  %%k_ns5 = load i32, ptr %%k_ns_alloca\n");
    ir_emit("  %%jdiv_lim = add i32 %%k_ns5, 2\n");
    ir_emit("  %%jdiv_done = icmp sge i32 %%jdiv, %%jdiv_lim\n");
    ir_emit("  br i1 %%jdiv_done, label %%ns_jdiv_end, label %%ns_jdiv_body\n");
    ir_emit("ns_jdiv_body:\n");
    ir_emit("  %%jdiv64 = sext i32 %%jdiv to i64\n");
    ir_emit("  %%np_jdiv_ptr = getelementptr [32 x %s], ptr %%new_prod, i64 0, i64 %%jdiv64\n", el);
    ir_emit("  %%np_jdiv = load %s, ptr %%np_jdiv_ptr\n", el);
    ir_emit("  %%np_jdiv_new = call %s @field%llu_mul(%s %%np_jdiv, %s %%inv_kp1)\n", el, P, el, el);
    ir_emit("  store %s %%np_jdiv_new, ptr %%np_jdiv_ptr\n", el);
    ir_emit("  %%jdiv_next = add i32 %%jdiv, 1\n");
    ir_emit("  store i32 %%jdiv_next, ptr %%jdiv_alloca\n");
    ir_emit("  br label %%ns_jdiv_cond\n");
    ir_emit("ns_jdiv_end:\n");
    /* Copy new_prod to product */
    ir_emit("  %%jcopy_alloca = alloca i32\n");
    ir_emit("  store i32 0, ptr %%jcopy_alloca\n");
    ir_emit("  br label %%ns_jcopy_cond\n");
    ir_emit("ns_jcopy_cond:\n");
    ir_emit("  %%jcopy = load i32, ptr %%jcopy_alloca\n");
    ir_emit("  %%copy_lim2 = load i32, ptr %%k_ns_alloca\n");
    ir_emit("  %%copy_lim3 = add i32 %%copy_lim2, 2\n");
    ir_emit("  %%jcopy_done = icmp sge i32 %%jcopy, %%copy_lim3\n");
    ir_emit("  br i1 %%jcopy_done, label %%ns_jcopy_end, label %%ns_jcopy_body\n");
    ir_emit("ns_jcopy_body:\n");
    ir_emit("  %%jcopy64 = sext i32 %%jcopy to i64\n");
    ir_emit("  %%np_src_ptr = getelementptr [32 x %s], ptr %%new_prod, i64 0, i64 %%jcopy64\n", el);
    ir_emit("  %%np_dst_ptr = getelementptr [32 x %s], ptr %%product, i64 0, i64 %%jcopy64\n", el);
    ir_emit("  %%np_src = load %s, ptr %%np_src_ptr\n", el);
    ir_emit("  store %s %%np_src, ptr %%np_dst_ptr\n", el);
    ir_emit("  %%jcopy_next = add i32 %%jcopy, 1\n");
    ir_emit("  store i32 %%jcopy_next, ptr %%jcopy_alloca\n");
    ir_emit("  br label %%ns_jcopy_cond\n");
    ir_emit("ns_jcopy_end:\n");
    ir_emit("  %%k_ns7 = load i32, ptr %%k_ns_alloca\n");
    ir_emit("  %%k_ns_next = add i32 %%k_ns7, 1\n");
    ir_emit("  store i32 %%k_ns_next, ptr %%k_ns_alloca\n");
    ir_emit("  br label %%ns_k_cond\n");
    ir_emit("ns_exit:\n");
    ir_emit("  ret void\n");
    ir_emit("}\n");
}

/* ============================================================
 * Expression codegen — returns LLVM SSA register number
 * ============================================================ */

typedef struct { int reg; Type type; } IRValue;

static IRValue codegen_expr(ASTNode *n, Type ctx_type);
static void codegen_stmt(ASTNode *n, Type fn_ret_type);
static void codegen_fn(ASTNode *n);

/* Forward declarations for parallel dispatch analysis */

/* Affine index normal form: loop_var + (sum of literal) + (sum of ±terms).
 * A write index is admissible only if it flattens to this form with the
 * loop variable appearing exactly once with coefficient +1 — a provably
 * injective (stride-1) map from iterations to cells.  Anything else
 * (constant index, loop-invariant index, i/2, i%k, let-bound index,
 * unknown identifier) is rejected and the loop stays sequential. */
#define MAX_AFFINE_TERMS 4
#define MAX_PFOR_WRITES  8
#define MAX_PFOR_LETS    16

#define MAX_PFOR_READS   16

typedef struct {
    int64_t lit;                                /* sum of literal terms */
    char    terms[MAX_AFFINE_TERMS][MAX_IDENT]; /* loop-invariant term keys
                                                   (serialized subtrees) */
    int     signs[MAX_AFFINE_TERMS];            /* +1 / -1 per term */
    int     nterms;
    bool    has_lv;                             /* loop var present (coeff +1) */
} AffineIdx;

typedef struct {
    char      arr[MAX_IDENT];   /* target array name */
    AffineIdx idx;              /* classified write index */
} PForWrite;

typedef struct {
    char      arr[MAX_IDENT];   /* source array name */
    AffineIdx idx;              /* classified read index */
    bool      classified;       /* false = wild read (arbitrary index) */
} PForRead;

typedef struct {
    bool         is_independent;
    PForCapture  caps[MAX_PFOR_CAP];
    int          ncaps;
    bool         has_field_element;
    /* Write records for injectivity + disjointness analysis */
    PForWrite    writes[MAX_PFOR_WRITES];
    int          nwrites;
    /* Read records for write-set hazard analysis */
    PForRead     reads[MAX_PFOR_READS];
    int          nreads;
    bool         reads_overflow;  /* too many reads to track: only safe if
                                     no array is both read and written */
    bool         private_read;    /* #46 mirror: a read went through a body-
                                     local base (alias-opaque name); hazard
                                     check refuses iff nwrites > 0 */
    /* Loop-local let bindings: idents that must NOT appear in indices
     * (their value varies per-iteration in unanalyzed ways) */
    char         lets[MAX_PFOR_LETS][MAX_IDENT];
    int          nlets;
} PForAnalysis;

static PForAnalysis pfor_analyze_body(ASTNode *body, const char *loop_var,
                                      ASTNode *start, ASTNode *end);
static void emit_pfor_workers(void);
static void emit_parallel_runtime(void);

/* Register a monomorphization request for a generic function.
 * Does NOT emit code — just registers the mangled name and FuncInfo
 * so the call site can emit the correct call. Actual codegen happens
 * in a deferred pass after all regular functions are emitted. */
static const char *monomorphize(FuncInfo *generic_fi, const char concrete[][MAX_IDENT]) {
    /* Check if already instantiated */
    MonoEntry *existing = find_mono(generic_fi->name, concrete, generic_fi->ngen);
    if (existing) return existing->mangled_name;

    /* Create cache entry */
    if (g_nmono >= MAX_MONO) cap_overflow("monomorphization cache (g_mono)", MAX_MONO);
    MonoEntry *me = &g_mono[g_nmono++];
    strcpy(me->base_name, generic_fi->name);
    me->ngen = generic_fi->ngen;
    for (int i = 0; i < generic_fi->ngen; i++) {
        strcpy(me->concrete_types[i], concrete[i]);
    }
    mangle_name(me->mangled_name, generic_fi->name, concrete, generic_fi->ngen);

    /* Dyn instantiation: any concrete type "dyn" means the function gets
     * an implicit leading 'Field' parameter (ptr to %__Field) carrying
     * the runtime modulus.  Callers pass the Field value explicitly:
     * forward_sum_dyn(f, coeffs, ...). */
    bool is_dyn = false;
    for (int i = 0; i < generic_fi->ngen; i++) {
        if (!strcmp(concrete[i], "dyn")) { is_dyn = true; break; }
    }

    /* Register the monomorphized version as a concrete function in g_funcs.
     * This lets subsequent calls find it via find_func(mangled_name)
     * and resolve the correct return type for the call instruction. */
    if (g_nfuncs >= MAX_FUNCS) cap_overflow("function registry (g_funcs)", MAX_FUNCS);
    FuncInfo *mono_fi = &g_funcs[g_nfuncs++];
    strcpy(mono_fi->name, me->mangled_name);
    mono_fi->nparams = generic_fi->nparams + (is_dyn ? 1 : 0);
    mono_fi->is_extern = false;
    mono_fi->is_generic = false;
    mono_fi->ngen = 0;
    mono_fi->ast = generic_fi->ast;

    /* Resolve param_types and ret_type by substituting generic params.
     * Dyn: param 0 is the implicit Field carrier; AST params shift +1. */
    int pshift = is_dyn ? 1 : 0;
    if (is_dyn) strcpy(mono_fi->param_types[0], "Field");
    for (int j = 0; j < generic_fi->nparams; j++) {
        strcpy(mono_fi->param_types[j + pshift], generic_fi->param_types[j]);
        for (int gi = 0; gi < generic_fi->ngen; gi++) {
            if (!strcmp(mono_fi->param_types[j + pshift], generic_fi->gen_names[gi])) {
                strcpy(mono_fi->param_types[j + pshift], concrete[gi]);
            }
        }
    }
    strcpy(mono_fi->ret_type, generic_fi->ret_type);
    for (int gi = 0; gi < generic_fi->ngen; gi++) {
        if (!strcmp(mono_fi->ret_type, generic_fi->gen_names[gi])) {
            strcpy(mono_fi->ret_type, concrete[gi]);
        }
    }

    return me->mangled_name;
}

/* Emit all deferred monomorphized function bodies.
 * Called from codegen_program after all regular functions are emitted. */
static void emit_monomorphized_fns(void) {
    for (int mi = 0; mi < g_nmono; mi++) {
        MonoEntry *me = &g_mono[mi];
        /* Find the original generic function to get AST and generic param info */
        FuncInfo *generic_fi = find_func(me->base_name);
        if (!generic_fi || !generic_fi->ast) continue;

        /* Set up substitution context */
        g_gen_nsub = me->ngen;
        for (int i = 0; i < me->ngen; i++) {
            strcpy(g_gen_sub_name[i], generic_fi->gen_names[i]);
            strcpy(g_gen_sub_type[i], me->concrete_types[i]);
        }
        strcpy(g_gen_mangled, me->mangled_name);
        g_mono_explicit = me->is_explicit;

        /* Emit the monomorphized function body */
        codegen_fn(generic_fi->ast);

        /* Clear substitution context */
        g_gen_nsub = 0;
        g_gen_mangled[0] = 0;
        g_mono_explicit = false;
    }
}

/* Load the implicit Field carrier pointer in a dyn-instantiated function.
 * Returns the SSA register holding the ptr, or -1 (with an error) if not
 * inside a dyn context. */
static int load_dyn_field_ptr(int line, int col) {
    Symbol *fs = sym_find("__field");
    if (!fs) {
        error(line, col, "dyn field operation outside a dyn-instantiated function");
        return -1;
    }
    int fv = ir_tmp();
    ir_emit("  %%t%d = load ptr, ptr %%t%d\n", fv, fs->ir_reg);
    return fv;
}

static IRValue codegen_expr(ASTNode *n, Type ctx_type) {
    IRValue val = { .reg = -1, .type = ctx_type };

    switch (n->kind) {
    case AST_NULL: {
        /* null pointer literal: produces ptr null */
        val.reg = ir_tmp();
        val.type.kind = TYPE_PTR;
        ir_emit("  %%t%d = inttoptr i64 0 to ptr\n", val.reg);
        return val;
    }

    case AST_STR_LIT: {
        /* String literal: emit as global constant, return ptr.
         * Type is *u8 (pointer to null-terminated bytes). */
        if (g_nstrings >= MAX_STRINGS) {
            error(n->line, n->col, "string literal table overflow (max %d)", MAX_STRINGS);
            val.reg = ir_tmp();
            val.type.kind = TYPE_PTR;
            return val;
        }
        int sid = g_nstrings++;
        int slen = (int)strlen(n->ident.name);
        memcpy(g_strings[sid].data, n->ident.name, slen);
        g_strings[sid].data[slen] = 0;
        g_strings[sid].len = slen;
        g_strings[sid].id = sid;

        /* Reference the global (emitted at end of IR) */
        val.reg = ir_tmp();
        val.type.kind = TYPE_PTR;
        val.type.ptr_pointee_kind = TYPE_U8;
        ir_emit("  %%t%d = getelementptr [%d x i8], ptr @.str.%d, i64 0, i64 0\n",
                val.reg, slen + 1, sid);
        return val;
    }

    case AST_LIT_INT: {
        val.reg = ir_tmp();
        /* Extension field literals: value goes into first component, second = 0 */
        if (ctx_type.kind == TYPE_FIELD && ctx_type.field_idx >= 0 &&
            ctx_type.field_idx < g_nfields &&
            g_fields[ctx_type.field_idx].field_kind == FIELD_KIND_EXTENSION) {
            FieldInfo *ext_fi = &g_fields[ctx_type.field_idx];
            const char *el = ext_fi->elem_ir;
            const char *et = ext_fi->ext_elem_ir;
            uint64_t reduced = n->lit_int.value % ext_fi->prime;
            int tmp0 = ir_tmp();
            ir_emit("  %%t%d = insertvalue %s undef, %s %llu, 0\n",
                    tmp0, et, el, (unsigned long long)reduced);
            ir_emit("  %%t%d = insertvalue %s %%t%d, %s 0, 1\n",
                    val.reg, et, tmp0, el);
            return val;
        }
        /* Dyn field literals: prime is runtime — emit i64 + runtime urem
         * against p loaded from the implicit %__field carrier.  At a
         * NON-dyn call site (passing a literal arg to a dyn instance),
         * there is no %__field in scope: emit the raw i64 — the value
         * is the caller's responsibility, same as any extern ABI. */
        if (ctx_type.kind == TYPE_DYNFIELD) {
            if (!sym_find("__field")) {
                ir_emit("  %%t%d = add i64 0, %llu\n", val.reg,
                        (unsigned long long)n->lit_int.value);
                return val;
            }
            int fv = load_dyn_field_ptr(n->line, n->col);
            int raw = ir_tmp();
            ir_emit("  %%t%d = add i64 0, %llu\n", raw,
                    (unsigned long long)n->lit_int.value);
            int pp = ir_tmp();
            ir_emit("  %%t%d = getelementptr %%__Field, ptr %%t%d, i32 0, i32 0\n",
                    pp, fv);
            int p = ir_tmp();
            ir_emit("  %%t%d = load i64, ptr %%t%d\n", p, pp);
            ir_emit("  %%t%d = urem i64 %%t%d, %%t%d\n", val.reg, raw, p);
            return val;
        }
        const char *ir_ty = type_to_ir(ctx_type);
        /* Field literals: safe by default — auto-reduce mod prime */
        ir_emit("  %%t%d = add %s 0, %llu\n", val.reg, ir_ty,
                (unsigned long long)(ctx_type.kind == TYPE_FIELD ?
                    n->lit_int.value % ctx_type.field_prime : n->lit_int.value));
        return val;
    }

    case AST_LIT_BOOL: {
        val.reg = ir_tmp();
        val.type.kind = TYPE_BOOL;
        ir_emit("  %%t%d = add i1 0, %d\n", val.reg, n->lit_bool.value ? 1 : 0);
        return val;
    }

    case AST_IDENT: {
        Symbol *s = sym_find(n->ident.name);
        if (!s) {
            error(n->line, n->col, "undefined variable '%s'", n->ident.name);
            val.reg = ir_tmp();
            ir_emit("  %%t%d = add %s 0, 0\n", val.reg, type_to_ir(ctx_type));
            return val;
        }
        val.type = s->type;

        /* Global variable: load from @name */
        if (s->ir_reg < 0) {
            val.reg = ir_tmp();
            ir_emit("  %%t%d = load %s, ptr @%s\n", val.reg,
                    type_to_ir(s->type), s->name);
            return val;
        }

        /* For compound types, return the alloca pointer directly (no load) */
        if (s->type.kind == TYPE_POLY || s->type.kind == TYPE_REGISTER ||
            s->type.kind == TYPE_ARRAY ||
            s->type.kind == TYPE_STRUCT || s->type.kind == TYPE_ENUM) {
            val.reg = s->ir_reg;
            return val;
        }
        if (s->is_alloca) {
            val.reg = ir_tmp();
            ir_emit("  %%t%d = load %s, ptr %%t%d\n", val.reg,
                    type_to_ir(s->type), s->ir_reg);
        } else {
            val.reg = s->ir_reg;
        }
        return val;
    }

    case AST_FIELD_ACCESS: {
        FieldInfo *fi = find_field(n->field_access.obj);
        if (!fi) {
            error(n->line, n->col, "unknown field '%s'", n->field_access.obj);
            val.reg = ir_tmp();
            ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
            return val;
        }
        val.type.kind = TYPE_FIELD;
        val.type.field_prime = fi->prime;
        val.type.field_idx = (int)(fi - g_fields);
        val.reg = ir_tmp();

        /* Extension field constants */
        if (fi->field_kind == FIELD_KIND_EXTENSION) {
            const char *el = fi->elem_ir;
            const char *et = fi->ext_elem_ir;
            if (!strcmp(n->field_access.member, "ZERO")) {
                int tmp0 = ir_tmp();
                ir_emit("  %%t%d = insertvalue %s undef, %s 0, 0\n", tmp0, et, el);
                ir_emit("  %%t%d = insertvalue %s %%t%d, %s 0, 1\n", val.reg, et, tmp0, el);
            } else if (!strcmp(n->field_access.member, "ONE")) {
                int tmp0 = ir_tmp();
                ir_emit("  %%t%d = insertvalue %s undef, %s 1, 0\n", tmp0, et, el);
                ir_emit("  %%t%d = insertvalue %s %%t%d, %s 0, 1\n", val.reg, et, tmp0, el);
            } else if (!strcmp(n->field_access.member, "IMAG")) {
                /* The imaginary unit: (0, 1) where i^2 = nr */
                int tmp0 = ir_tmp();
                ir_emit("  %%t%d = insertvalue %s undef, %s 0, 0\n", tmp0, et, el);
                ir_emit("  %%t%d = insertvalue %s %%t%d, %s 1, 1\n", val.reg, et, tmp0, el);
            } else if (!strcmp(n->field_access.member, "NR")) {
                /* The non-residue as a scalar in the base field (for verification) */
                val.type.kind = TYPE_U64;
                ir_emit("  %%t%d = add i64 0, %llu\n", val.reg,
                        (unsigned long long)fi->nonresidue);
            } else {
                error(n->line, n->col, "unknown extension field constant '%s::%s'",
                      n->field_access.obj, n->field_access.member);
                int tmp0 = ir_tmp();
                ir_emit("  %%t%d = insertvalue %s undef, %s 0, 0\n", tmp0, et, el);
                ir_emit("  %%t%d = insertvalue %s %%t%d, %s 0, 1\n", val.reg, et, tmp0, el);
            }
            return val;
        }

        if (!strcmp(n->field_access.member, "ZERO")) {
            ir_emit("  %%t%d = add %s 0, 0\n", val.reg, fi->elem_ir);
        } else if (!strcmp(n->field_access.member, "ONE")) {
            ir_emit("  %%t%d = add %s 0, 1\n", val.reg, fi->elem_ir);
        } else if (!strcmp(n->field_access.member, "PRIME")) {
            /* Return the prime as a wider integer */
            val.type.kind = TYPE_U64;
            ir_emit("  %%t%d = add i64 0, %llu\n", val.reg,
                    (unsigned long long)fi->prime);
        } else {
            error(n->line, n->col, "unknown field constant '%s::%s'",
                  n->field_access.obj, n->field_access.member);
            ir_emit("  %%t%d = add %s 0, 0\n", val.reg, fi->elem_ir);
        }
        return val;
    }

    case AST_BINARY: {
        /* ---- Short-circuit && / || (known-issue #14 fix) ----
         *
         * MIRRORS tvc_self.tv. This is a deliberate exception to the tvc.c
         * freeze: the dual-parity gate (tests/run_dual.sh) asserts runtime
         * OUTPUT equality between the two compilers, so they must short-circuit
         * IDENTICALLY or a program with a faulting / side-effecting RHS would
         * diverge (e.g. `p != null && p[0]`, `i < len && arr[i]`). The old
         * codegen emitted a branchless `and/or i1` of both operands — the RHS
         * was ALWAYS evaluated, a null-deref / OOB-read footgun. Lower to a
         * control-flow diamond with the result in an alloca slot (no phi: the
         * merge load reads whichever path stored last). i1-only; integer
         * truthiness is out of scope (deferred). Precedent for touching the
         * frozen seed: the A6 evaluation-order fix. */
        if (n->binary.op == OP_AND || n->binary.op == OP_OR) {
            /* Result slot in the entry block (alloca-prelude idiom). */
            int sc = ir_tmp();
            bool saved_up = g_use_prelude;
            g_use_prelude = true;
            ir_emit_alloca("  %%t%d = alloca i1\n", sc);
            g_use_prelude = saved_up;
            /* Evaluate LHS, store as the provisional result. */
            Type bool_ctx = ctx_type; bool_ctx.kind = TYPE_BOOL;
            IRValue sc_lhs = codegen_expr(n->binary.lhs, bool_ctx);
            ir_emit("  store i1 %%t%d, ptr %%t%d\n", sc_lhs.reg, sc);
            int lbl_rhs = ir_label();
            int lbl_merge = ir_label();
            /* && : LHS true -> eval RHS, else merge.
             * || : LHS true -> merge,   else eval RHS. */
            if (n->binary.op == OP_AND) {
                ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n",
                        sc_lhs.reg, lbl_rhs, lbl_merge);
            } else {
                ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n",
                        sc_lhs.reg, lbl_merge, lbl_rhs);
            }
            /* RHS block: evaluate, overwrite the slot, fall to merge. */
            ir_emit("L%d:\n", lbl_rhs);
            IRValue sc_rhs = codegen_expr(n->binary.rhs, bool_ctx);
            ir_emit("  store i1 %%t%d, ptr %%t%d\n", sc_rhs.reg, sc);
            ir_emit("  br label %%L%d\n", lbl_merge);
            /* Merge block: load whichever value was stored. */
            ir_emit("L%d:\n", lbl_merge);
            g_block_terminated = false;
            IRValue scv;
            scv.reg = ir_tmp();
            scv.type = ctx_type;
            scv.type.kind = TYPE_BOOL;
            ir_emit("  %%t%d = load i1, ptr %%t%d\n", scv.reg, sc);
            return scv;
        }
        /* Poly addition: p + q where both are TYPE_POLY */
        if (n->binary.op == OP_ADD && ctx_type.kind == TYPE_POLY) {
            int deg = ctx_type.poly_degree;
            int n_coeffs = deg + 1;
            uint64_t prime = ctx_type.field_prime;
            const char *el = field_elem_ir(prime);

            /* Get lhs poly */
            IRValue lhs_poly = codegen_expr(n->binary.lhs, ctx_type);
            /* Get rhs poly */
            IRValue rhs_poly = codegen_expr(n->binary.rhs, ctx_type);

            /* Determine repr of each operand */
            int lhs_repr = REPR_UNKNOWN;
            int rhs_repr = REPR_UNKNOWN;
            if (n->binary.lhs->kind == AST_IDENT) {
                Symbol *ls = sym_find(n->binary.lhs->ident.name);
                if (ls) lhs_repr = ls->repr_tag;
            }
            if (n->binary.rhs->kind == AST_IDENT) {
                Symbol *rs = sym_find(n->binary.rhs->ident.name);
                if (rs) rhs_repr = rs->repr_tag;
            }

            /* Convert Newton to standard if needed */
            int lhs_std_reg = lhs_poly.reg;
            if (lhs_repr == REPR_NEWTON) {
                lhs_std_reg = ir_tmp();
                ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", lhs_std_reg, n_coeffs, el);
                ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                        lhs_std_reg, n_coeffs);
                int d_reg = ir_tmp();
                ir_emit("  %%t%d = add i32 0, %d\n", d_reg, deg);
                ir_emit("  call void @newton_to_std_F%llu(ptr %%t%d, ptr %%t%d, i32 %%t%d)\n",
                        (unsigned long long)prime, lhs_poly.reg, lhs_std_reg, d_reg);
            }

            int rhs_std_reg = rhs_poly.reg;
            if (rhs_repr == REPR_NEWTON) {
                rhs_std_reg = ir_tmp();
                ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", rhs_std_reg, n_coeffs, el);
                ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                        rhs_std_reg, n_coeffs);
                int d_reg = ir_tmp();
                ir_emit("  %%t%d = add i32 0, %d\n", d_reg, deg);
                ir_emit("  call void @newton_to_std_F%llu(ptr %%t%d, ptr %%t%d, i32 %%t%d)\n",
                        (unsigned long long)prime, rhs_poly.reg, rhs_std_reg, d_reg);
            }

            /* Alloca result buffer */
            int result_reg = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", result_reg, n_coeffs, el);
            ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                    result_reg, n_coeffs);

            /* Element-wise field_add */
            for (int i = 0; i < n_coeffs; i++) {
                int i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", i64, i);
                int lg = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        lg, n_coeffs, el, lhs_std_reg, i64);
                int rg = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        rg, n_coeffs, el, rhs_std_reg, i64);
                int resg = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        resg, n_coeffs, el, result_reg, i64);
                int lv = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", lv, el, lg);
                int rv = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", rv, el, rg);
                int sv = ir_tmp();
                ir_emit("  %%t%d = call %s @field%llu_add(%s %%t%d, %s %%t%d)\n",
                        sv, el, (unsigned long long)prime, el, lv, el, rv);
                ir_emit("  store %s %%t%d, ptr %%t%d\n", el, sv, resg);
            }

            val.type = ctx_type;
            val.reg = result_reg;
            g_last_repr = REPR_STANDARD;
            return val;
        }

        /* Codegen lhs first to discover its type, then use that
         * as context for rhs. This lets `x <= 125` work when
         * x is a field element but the parent context is bool. */
        Type lhs_ctx = ctx_type;

        /* For comparisons, don't force bool on operands */
        bool is_cmp = (n->binary.op >= OP_EQ && n->binary.op <= OP_GE);
        if (is_cmp && ctx_type.kind == TYPE_BOOL) {
            /* Let lhs infer its own type — pass a neutral ctx */
            lhs_ctx.kind = TYPE_I32;  /* fallback; ident will override */
        }

        IRValue lhs = codegen_expr(n->binary.lhs, lhs_ctx);

        /* Use lhs type as context for rhs */
        Type rhs_ctx = lhs.type;

        /* Power: rhs is u32 (or u64 for 64-bit fields where p-2 > 2^32) */
        if (n->binary.op == OP_POW) {
            FieldInfo *fi_exp = (lhs.type.kind == TYPE_FIELD) ?
                find_field_by_prime(lhs.type.field_prime) : NULL;
            rhs_ctx.kind = (fi_exp && fi_exp->elem_bits == 64) ? TYPE_U64 : TYPE_U32;
            rhs_ctx.field_prime = 0;
        }

        IRValue rhs = codegen_expr(n->binary.rhs, rhs_ctx);

        /* If one side resolved to a field type, use that as context */
        if (lhs.type.kind == TYPE_FIELD) ctx_type = lhs.type;
        else if (rhs.type.kind == TYPE_FIELD) ctx_type = rhs.type;
        else if (lhs.type.kind == TYPE_DYNFIELD) ctx_type = lhs.type;
        else if (rhs.type.kind == TYPE_DYNFIELD) ctx_type = rhs.type;
        else if (lhs.type.kind != TYPE_BOOL) ctx_type = lhs.type;

        /* Cross-field type error: two different field types in binary expr */
        if (lhs.type.kind == TYPE_FIELD && rhs.type.kind == TYPE_FIELD &&
            lhs.type.field_prime != rhs.type.field_prime) {
            error(n->line, n->col,
                  "cannot mix Field<%llu> and Field<%llu> in binary expression",
                  (unsigned long long)lhs.type.field_prime,
                  (unsigned long long)rhs.type.field_prime);
        }

        val.type = ctx_type;
        val.reg = ir_tmp();

        if (ctx_type.kind == TYPE_FIELD) {
            uint64_t p = ctx_type.field_prime;
            const char *el = field_elem_ir(p);

            /* Determine function name prefix and element IR type by field kind */
            FieldInfo *fi_ctx = (ctx_type.field_idx >= 0 && ctx_type.field_idx < g_nfields)
                ? &g_fields[ctx_type.field_idx] : find_field_by_prime(p);
            bool is_bin = (fi_ctx && fi_ctx->field_kind == FIELD_KIND_BINARY);
            bool is_ext = (fi_ctx && fi_ctx->field_kind == FIELD_KIND_EXTENSION);
            char fn_prefix[64];
            if (is_bin) {
                snprintf(fn_prefix, sizeof(fn_prefix), "gf256");
            } else if (is_ext) {
                snprintf(fn_prefix, sizeof(fn_prefix), "ext%llu", (unsigned long long)p);
                el = fi_ctx->ext_elem_ir;  /* use pair type for extension */
            } else {
                snprintf(fn_prefix, sizeof(fn_prefix), "field%llu", (unsigned long long)p);
            }

            switch (n->binary.op) {
            case OP_ADD:
                ir_emit("  %%t%d = call %s @%s_add(%s %%t%d, %s %%t%d)\n",
                        val.reg, el, fn_prefix, el, lhs.reg, el, rhs.reg);
                break;
            case OP_SUB:
                ir_emit("  %%t%d = call %s @%s_sub(%s %%t%d, %s %%t%d)\n",
                        val.reg, el, fn_prefix, el, lhs.reg, el, rhs.reg);
                break;
            case OP_MUL:
                ir_emit("  %%t%d = call %s @%s_mul(%s %%t%d, %s %%t%d)\n",
                        val.reg, el, fn_prefix, el, lhs.reg, el, rhs.reg);
                break;
            case OP_DIV:
                ir_emit("  %%t%d = call %s @%s_div(%s %%t%d, %s %%t%d)\n",
                        val.reg, el, fn_prefix, el, lhs.reg, el, rhs.reg);
                break;
            case OP_POW: {
                /* Detect negative exponents: x ** -1 should use inv(),
                   x ** -k is an error for other negative values. */
                ASTNode *exp_node = n->binary.rhs;
                bool neg_exp = false;
                if (exp_node->kind == AST_UNARY && exp_node->unary.op == OP_NEG) {
                    neg_exp = true;
                }
                /* Also detect large unsigned literals that look like they meant to be negative
                   (e.g., literal > 2^31) — this catches x ** 4294967295 which is likely x ** -1 */
                if (exp_node->kind == AST_LIT_INT && exp_node->lit_int.value > 2147483647ULL) {
                    error(n->line, n->col,
                          "exponent %llu exceeds i32 range; use inv() for field inverse",
                          (unsigned long long)exp_node->lit_int.value);
                }
                /* EC-2: Warn if x^k is not a permutation polynomial.
                   gcd(k, p-1) must be 1 for x^k to be bijective over Z/pZ. */
                if (!is_bin && !neg_exp && exp_node->kind == AST_LIT_INT) {
                    uint64_t k = exp_node->lit_int.value;
                    if (k > 1 && p > 2) {
                        uint64_t g = gcd64(k, p - 1);
                        if (g != 1) {
                            uint64_t unique = (p - 1) / g + 1;
                            /* Find smallest valid odd power */
                            uint64_t suggest = 0;
                            for (uint64_t s = k % 2 == 0 ? 3 : k + 2; s < p - 1; s += 2) {
                                if (gcd64(s, p - 1) == 1) { suggest = s; break; }
                            }
                            if (suggest) {
                                warn(n->line, n->col,
                                     "x^%llu is not a permutation over Z/%lluZ "
                                     "(gcd(%llu,%llu)=%llu, only %llu/%llu unique outputs). "
                                     "Consider x^%llu",
                                     (unsigned long long)k, (unsigned long long)p,
                                     (unsigned long long)k, (unsigned long long)(p-1),
                                     (unsigned long long)g, (unsigned long long)unique,
                                     (unsigned long long)p, (unsigned long long)suggest);
                            } else {
                                warn(n->line, n->col,
                                     "x^%llu is not a permutation over Z/%lluZ "
                                     "(gcd(%llu,%llu)=%llu, only %llu/%llu unique outputs)",
                                     (unsigned long long)k, (unsigned long long)p,
                                     (unsigned long long)k, (unsigned long long)(p-1),
                                     (unsigned long long)g, (unsigned long long)unique,
                                     (unsigned long long)p);
                            }
                        }
                    }
                }
                /* Determine exponent IR type for this field */
                const char *ety = (fi_ctx && fi_ctx->elem_bits == 64) ? "i64" : "i32";
                if (neg_exp) {
                    /* x ** -1 → inv(x) */
                    if (exp_node->unary.operand->kind == AST_LIT_INT &&
                        exp_node->unary.operand->lit_int.value == 1) {
                        ir_emit("  %%t%d = call %s @%s_inv(%s %%t%d)\n",
                                val.reg, el, fn_prefix, el, lhs.reg);
                    } else {
                        error(n->line, n->col,
                              "negative exponent in field power; only x ** -1 (= inv(x)) is supported");
                        ir_emit("  %%t%d = call %s @%s_pow(%s %%t%d, %s %%t%d)\n",
                                val.reg, el, fn_prefix, el, lhs.reg, ety, rhs.reg);
                    }
                } else {
                    ir_emit("  %%t%d = call %s @%s_pow(%s %%t%d, %s %%t%d)\n",
                            val.reg, el, fn_prefix, el, lhs.reg, ety, rhs.reg);
                }
                break;
            }
            case OP_EQ:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp eq %s %%t%d, %%t%d\n", val.reg, el, lhs.reg, rhs.reg);
                break;
            case OP_NEQ:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp ne %s %%t%d, %%t%d\n", val.reg, el, lhs.reg, rhs.reg);
                break;
            case OP_LT:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp ult %s %%t%d, %%t%d\n", val.reg, el, lhs.reg, rhs.reg);
                break;
            case OP_GT:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp ugt %s %%t%d, %%t%d\n", val.reg, el, lhs.reg, rhs.reg);
                break;
            case OP_LE:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp ule %s %%t%d, %%t%d\n", val.reg, el, lhs.reg, rhs.reg);
                break;
            case OP_GE:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp uge %s %%t%d, %%t%d\n", val.reg, el, lhs.reg, rhs.reg);
                break;
            default:
                error(n->line, n->col, "unsupported field operator");
                ir_emit("  %%t%d = add %s 0, 0\n", val.reg, el);
            }
        } else if (ctx_type.kind == TYPE_DYNFIELD) {
            /* Dyn field arithmetic: runtime modulus loaded from the
             * implicit %__field carrier.  All ops route through the
             * @field_dyn_* runtime (i64, i64, ptr). */
            int fv = load_dyn_field_ptr(n->line, n->col);

            /* Harmonize operand widths to i64 (literals codegen'd in a
             * non-dyn sub-context may be narrower). */
            int lreg = lhs.reg, rreg = rhs.reg;
            int lb = type_ir_bitwidth(lhs.type);
            if (lb > 0 && lb < 64) {
                lreg = ir_tmp();
                ir_emit("  %%t%d = zext %s %%t%d to i64\n", lreg,
                        type_to_ir(lhs.type), lhs.reg);
            }
            int rb = type_ir_bitwidth(rhs.type);
            if (rb > 0 && rb < 64) {
                rreg = ir_tmp();
                ir_emit("  %%t%d = zext %s %%t%d to i64\n", rreg,
                        type_to_ir(rhs.type), rhs.reg);
            }

            switch (n->binary.op) {
            case OP_ADD:
                ir_emit("  %%t%d = call i64 @field_dyn_add(i64 %%t%d, i64 %%t%d, ptr %%t%d)\n",
                        val.reg, lreg, rreg, fv);
                break;
            case OP_SUB:
                ir_emit("  %%t%d = call i64 @field_dyn_sub(i64 %%t%d, i64 %%t%d, ptr %%t%d)\n",
                        val.reg, lreg, rreg, fv);
                break;
            case OP_MUL:
                ir_emit("  %%t%d = call i64 @field_dyn_mul(i64 %%t%d, i64 %%t%d, ptr %%t%d)\n",
                        val.reg, lreg, rreg, fv);
                break;
            case OP_DIV:
                ir_emit("  %%t%d = call i64 @field_dyn_div(i64 %%t%d, i64 %%t%d, ptr %%t%d)\n",
                        val.reg, lreg, rreg, fv);
                break;
            case OP_POW: {
                ASTNode *exp_node = n->binary.rhs;
                if (exp_node->kind == AST_UNARY && exp_node->unary.op == OP_NEG) {
                    if (exp_node->unary.operand->kind == AST_LIT_INT &&
                        exp_node->unary.operand->lit_int.value == 1) {
                        ir_emit("  %%t%d = call i64 @field_dyn_inv(i64 %%t%d, ptr %%t%d)\n",
                                val.reg, lreg, fv);
                    } else {
                        error(n->line, n->col,
                              "negative exponent in field power; only x ** -1 (= inv(x)) is supported");
                        ir_emit("  %%t%d = add i64 0, 0\n", val.reg);
                    }
                } else {
                    ir_emit("  %%t%d = call i64 @field_dyn_pow(i64 %%t%d, i64 %%t%d, ptr %%t%d)\n",
                            val.reg, lreg, rreg, fv);
                }
                break;
            }
            case OP_EQ:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp eq i64 %%t%d, %%t%d\n", val.reg, lreg, rreg);
                break;
            case OP_NEQ:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp ne i64 %%t%d, %%t%d\n", val.reg, lreg, rreg);
                break;
            case OP_LT:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp ult i64 %%t%d, %%t%d\n", val.reg, lreg, rreg);
                break;
            case OP_GT:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp ugt i64 %%t%d, %%t%d\n", val.reg, lreg, rreg);
                break;
            case OP_LE:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp ule i64 %%t%d, %%t%d\n", val.reg, lreg, rreg);
                break;
            case OP_GE:
                val.type.kind = TYPE_BOOL;
                ir_emit("  %%t%d = icmp uge i64 %%t%d, %%t%d\n", val.reg, lreg, rreg);
                break;
            default:
                error(n->line, n->col, "unsupported dyn field operator");
                ir_emit("  %%t%d = add i64 0, 0\n", val.reg);
            }
        } else if (lhs.type.kind == TYPE_PTR || rhs.type.kind == TYPE_PTR) {
            /* Pointer comparison (== null, != null) */
            val.type.kind = TYPE_BOOL;
            switch (n->binary.op) {
            case OP_EQ:  ir_emit("  %%t%d = icmp eq ptr %%t%d, %%t%d\n", val.reg, lhs.reg, rhs.reg); break;
            case OP_NEQ: ir_emit("  %%t%d = icmp ne ptr %%t%d, %%t%d\n", val.reg, lhs.reg, rhs.reg); break;
            default:
                error(n->line, n->col, "unsupported pointer operator");
                ir_emit("  %%t%d = add i32 0, 0\n", val.reg);
            }
        } else {
            /* Integer arithmetic.
             * Signed types (i8-i64): use sdiv, srem, slt, sgt, sle, sge.
             * Unsigned types (u8-u64, usize, bool): use udiv, urem, ult, ugt, ule, uge.
             *
             * Implicit widening: if lhs and rhs have different bit widths,
             * extend the narrower operand to match the wider one.
             * Rule: wider type wins. Signed source uses sext, unsigned uses zext. */
            int lhs_bits = type_ir_bitwidth(lhs.type);
            int rhs_bits = type_ir_bitwidth(rhs.type);
            if (lhs_bits > 0 && rhs_bits > 0 && lhs_bits != rhs_bits) {
                if (lhs_bits > rhs_bits) {
                    /* Widen rhs to match lhs */
                    int widened = ir_tmp();
                    ir_emit("  %%t%d = %s %s %%t%d to %s\n", widened,
                            type_is_signed(rhs.type) ? "sext" : "zext",
                            type_to_ir(rhs.type), rhs.reg, type_to_ir(lhs.type));
                    rhs.reg = widened;
                    rhs.type = lhs.type;
                    ctx_type = lhs.type;
                } else {
                    /* Widen lhs to match rhs */
                    int widened = ir_tmp();
                    ir_emit("  %%t%d = %s %s %%t%d to %s\n", widened,
                            type_is_signed(lhs.type) ? "sext" : "zext",
                            type_to_ir(lhs.type), lhs.reg, type_to_ir(rhs.type));
                    lhs.reg = widened;
                    lhs.type = rhs.type;
                    ctx_type = rhs.type;
                }
                val.type = ctx_type; /* update return type after widening */
            }
            const char *ir_ty = type_to_ir(ctx_type);
            bool is_signed = (ctx_type.kind == TYPE_I8 || ctx_type.kind == TYPE_I16 ||
                              ctx_type.kind == TYPE_I32 || ctx_type.kind == TYPE_I64);
            /* For comparison ops, determine signedness from operand types, not ctx_type
             * (ctx_type may be TYPE_BOOL for conditions, but operands carry the real type) */
            bool operand_signed = (lhs.type.kind == TYPE_I8 || lhs.type.kind == TYPE_I16 ||
                                   lhs.type.kind == TYPE_I32 || lhs.type.kind == TYPE_I64);
            switch (n->binary.op) {
            case OP_ADD: ir_emit("  %%t%d = add %s %%t%d, %%t%d\n", val.reg, ir_ty, lhs.reg, rhs.reg); break;
            case OP_SUB: ir_emit("  %%t%d = sub %s %%t%d, %%t%d\n", val.reg, ir_ty, lhs.reg, rhs.reg); break;
            case OP_MUL: ir_emit("  %%t%d = mul %s %%t%d, %%t%d\n", val.reg, ir_ty, lhs.reg, rhs.reg); break;
            case OP_DIV: ir_emit("  %%t%d = %s %s %%t%d, %%t%d\n", val.reg, is_signed ? "sdiv" : "udiv", ir_ty, lhs.reg, rhs.reg); break;
            case OP_MOD: ir_emit("  %%t%d = %s %s %%t%d, %%t%d\n", val.reg, is_signed ? "srem" : "urem", ir_ty, lhs.reg, rhs.reg); break;
            case OP_EQ:  val.type.kind = TYPE_BOOL; ir_emit("  %%t%d = icmp eq %s %%t%d, %%t%d\n", val.reg, ir_ty, lhs.reg, rhs.reg); break;
            case OP_NEQ: val.type.kind = TYPE_BOOL; ir_emit("  %%t%d = icmp ne %s %%t%d, %%t%d\n", val.reg, ir_ty, lhs.reg, rhs.reg); break;
            case OP_LT:  val.type.kind = TYPE_BOOL; ir_emit("  %%t%d = icmp %s %s %%t%d, %%t%d\n", val.reg, operand_signed ? "slt" : "ult", ir_ty, lhs.reg, rhs.reg); break;
            case OP_GT:  val.type.kind = TYPE_BOOL; ir_emit("  %%t%d = icmp %s %s %%t%d, %%t%d\n", val.reg, operand_signed ? "sgt" : "ugt", ir_ty, lhs.reg, rhs.reg); break;
            case OP_LE:  val.type.kind = TYPE_BOOL; ir_emit("  %%t%d = icmp %s %s %%t%d, %%t%d\n", val.reg, operand_signed ? "sle" : "ule", ir_ty, lhs.reg, rhs.reg); break;
            case OP_GE:  val.type.kind = TYPE_BOOL; ir_emit("  %%t%d = icmp %s %s %%t%d, %%t%d\n", val.reg, operand_signed ? "sge" : "uge", ir_ty, lhs.reg, rhs.reg); break;
            case OP_AND: val.type.kind = TYPE_BOOL; ir_emit("  %%t%d = and i1 %%t%d, %%t%d\n", val.reg, lhs.reg, rhs.reg); break;
            case OP_OR:  val.type.kind = TYPE_BOOL; ir_emit("  %%t%d = or i1 %%t%d, %%t%d\n", val.reg, lhs.reg, rhs.reg); break;
            case OP_BITAND: ir_emit("  %%t%d = and %s %%t%d, %%t%d\n", val.reg, ir_ty, lhs.reg, rhs.reg); break;
            case OP_BITOR:  ir_emit("  %%t%d = or %s %%t%d, %%t%d\n", val.reg, ir_ty, lhs.reg, rhs.reg); break;
            case OP_BITXOR: ir_emit("  %%t%d = xor %s %%t%d, %%t%d\n", val.reg, ir_ty, lhs.reg, rhs.reg); break;
            case OP_SHL: ir_emit("  %%t%d = shl %s %%t%d, %%t%d\n", val.reg, ir_ty, lhs.reg, rhs.reg); break;
            case OP_SHR: ir_emit("  %%t%d = %s %s %%t%d, %%t%d\n", val.reg, is_signed ? "ashr" : "lshr", ir_ty, lhs.reg, rhs.reg); break;
            default:
                error(n->line, n->col, "unsupported operator");
                ir_emit("  %%t%d = add %s 0, 0\n", val.reg, ir_ty);
            }
        }
        return val;
    }

    case AST_UNARY: {
        /* Address-of must be handled before operand evaluation,
         * because we need the address, not the loaded value. */
        if (n->unary.op == OP_ADDR) {
            ASTNode *inner = n->unary.operand;
            if (inner->kind == AST_IDENT) {
                Symbol *s = sym_find(inner->ident.name);
                if (!s) {
                    error(n->line, n->col, "undefined variable '%s'", inner->ident.name);
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = inttoptr i64 0 to ptr\n", val.reg);
                    val.type.kind = TYPE_PTR;
                    return val;
                }
                if (!s->is_alloca && s->ir_reg >= 0) {
                    error(n->line, n->col,
                          "cannot take address of immutable binding '%s' (declare with 'let mut')",
                          inner->ident.name);
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = inttoptr i64 0 to ptr\n", val.reg);
                    val.type.kind = TYPE_PTR;
                    return val;
                }
                val.type.kind = TYPE_PTR;
                val.type.ptr_pointee_kind = s->type.kind;
                if (s->type.kind == TYPE_STRUCT)
                    val.type.ptr_struct_id = s->type.struct_id;
                if (s->type.kind == TYPE_FIELD)
                    val.type.field_prime = s->type.field_prime;
                if (s->ir_reg < 0) {
                    /* Global: @name is already a pointer in LLVM.
                     * Use GEP with offset 0 to get it into an SSA register. */
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = getelementptr i8, ptr @%s, i64 0\n",
                            val.reg, s->name);
                } else {
                    /* Local alloca: the alloca register IS the address */
                    val.reg = s->ir_reg;
                }
                return val;
            }
            /* &arr[i] or &expr[i]: compute GEP to element, return pointer */
            if (inner->kind == AST_INDEX) {
                /* Expression-based: &tokens[ntok].text[0] — obj_expr holds the base */
                if (inner->index_expr.obj_expr) {
                    IRValue base = codegen_expr(inner->index_expr.obj_expr, (Type){0});
                    IRValue idx = codegen_expr(inner->index_expr.index, (Type){.kind = TYPE_I32});
                    int idx64 = ir_tmp();
                    ir_emit("  %%t%d = sext i32 %%t%d to i64\n", idx64, idx.reg);
                    if (base.type.kind == TYPE_ARRAY) {
                        Type elem_type = {.kind = base.type.elem_kind,
                                          .field_prime = base.type.field_prime};
                        val.reg = ir_tmp();
                        ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 0, i64 %%t%d\n",
                                val.reg, type_to_ir(base.type), base.reg, idx64);
                        val.type.kind = TYPE_PTR;
                        val.type.ptr_pointee_kind = elem_type.kind;
                        if (elem_type.kind == TYPE_FIELD)
                            val.type.ptr_field_prime = elem_type.field_prime;
                    } else {
                        /* Pointer-typed base */
                        Type pointee = type_pointee(base.type);
                        val.reg = ir_tmp();
                        ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 %%t%d\n",
                                val.reg, type_to_ir(pointee), base.reg, idx64);
                        val.type.kind = TYPE_PTR;
                        val.type.ptr_pointee_kind = pointee.kind;
                    }
                    return val;
                }
                Symbol *arr_sym = sym_find(inner->index_expr.name);
                if (!arr_sym) {
                    error(n->line, n->col, "undefined variable '%s'", inner->index_expr.name);
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = inttoptr i64 0 to ptr\n", val.reg);
                    val.type.kind = TYPE_PTR;
                    return val;
                }
                IRValue idx = codegen_expr(inner->index_expr.index, (Type){.kind = TYPE_I32});
                int idx64 = ir_tmp();
                ir_emit("  %%t%d = sext i32 %%t%d to i64\n", idx64, idx.reg);

                if (arr_sym->type.kind == TYPE_PTR) {
                    /* Pointer: load ptr, GEP */
                    Type pointee = type_pointee(arr_sym->type);
                    int ptr_val;
                    if (arr_sym->ir_reg < 0) {
                        ptr_val = ir_tmp();
                        ir_emit("  %%t%d = load ptr, ptr @%s\n", ptr_val, arr_sym->name);
                    } else if (arr_sym->is_alloca) {
                        ptr_val = ir_tmp();
                        ir_emit("  %%t%d = load ptr, ptr %%t%d\n", ptr_val, arr_sym->ir_reg);
                    } else {
                        ptr_val = arr_sym->ir_reg;
                    }
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 %%t%d\n",
                            val.reg, type_to_ir(pointee), ptr_val, idx64);
                    val.type.kind = TYPE_PTR;
                    val.type.ptr_pointee_kind = pointee.kind;
                    if (pointee.kind == TYPE_STRUCT) val.type.ptr_struct_id = pointee.struct_id;
                    if (pointee.kind == TYPE_FIELD) val.type.ptr_field_prime = pointee.field_prime;
                } else {
                    /* Array: GEP into array alloca */
                    Type elem_type = {.kind = arr_sym->type.elem_kind,
                                      .field_prime = arr_sym->type.field_prime};
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 0, i64 %%t%d\n",
                            val.reg, type_to_ir(arr_sym->type), arr_sym->ir_reg, idx64);
                    val.type.kind = TYPE_PTR;
                    val.type.ptr_pointee_kind = elem_type.kind;
                    if (elem_type.kind == TYPE_FIELD) val.type.ptr_field_prime = elem_type.field_prime;
                }
                return val;
            }

            /* &obj.field: compute GEP to struct field, return pointer */
            if (inner->kind == AST_MEMBER_ACCESS) {
                /* Evaluate the object expression to get the struct pointer */
                IRValue obj;
                if (inner->member_access.obj_expr) {
                    obj = codegen_expr(inner->member_access.obj_expr, ctx_type);
                } else {
                    Symbol *s = sym_find(inner->member_access.obj_name);
                    if (!s) {
                        error(n->line, n->col, "undefined variable '%s'",
                              inner->member_access.obj_name);
                        val.reg = ir_tmp();
                        ir_emit("  %%t%d = inttoptr i64 0 to ptr\n", val.reg);
                        val.type.kind = TYPE_PTR;
                        return val;
                    }
                    obj.reg = s->ir_reg;
                    obj.type = s->type;
                }
                /* Resolve struct info */
                int sid = -1;
                if (obj.type.kind == TYPE_STRUCT) sid = obj.type.struct_id;
                else if (obj.type.kind == TYPE_PTR && obj.type.ptr_pointee_kind == TYPE_STRUCT)
                    sid = obj.type.ptr_struct_id;
                if (sid < 0 || sid >= g_nstructs) {
                    error(n->line, n->col, "cannot take address of field on non-struct type");
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = inttoptr i64 0 to ptr\n", val.reg);
                    val.type.kind = TYPE_PTR;
                    return val;
                }
                StructInfo *si = &g_structs[sid];
                int fidx = -1;
                for (int i = 0; i < si->nfields; i++) {
                    if (!strcmp(si->fields[i].name, inner->member_access.member)) {
                        fidx = i; break;
                    }
                }
                if (fidx < 0) {
                    error(n->line, n->col, "struct '%s' has no field '%s'",
                          si->name, inner->member_access.member);
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = inttoptr i64 0 to ptr\n", val.reg);
                    val.type.kind = TYPE_PTR;
                    return val;
                }
                /* Auto-deref if obj is a pointer to struct */
                int struct_ptr = obj.reg;
                if (obj.type.kind == TYPE_PTR) {
                    struct_ptr = ir_tmp();
                    ir_emit("  %%t%d = load ptr, ptr %%t%d\n", struct_ptr, obj.reg);
                }
                /* GEP to field — no load */
                val.reg = ir_tmp();
                ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 %d\n",
                        val.reg, si->ir_name, struct_ptr, fidx);
                Type ft = si->fields[fidx].type;
                val.type.kind = TYPE_PTR;
                val.type.ptr_pointee_kind = ft.kind;
                if (ft.kind == TYPE_STRUCT) val.type.ptr_struct_id = ft.struct_id;
                if (ft.kind == TYPE_FIELD) val.type.ptr_field_prime = ft.field_prime;
                return val;
            }

            error(n->line, n->col,
                  "address-of requires a variable, array element, or struct field");
            val.reg = ir_tmp();
            ir_emit("  %%t%d = inttoptr i64 0 to ptr\n", val.reg);
            val.type.kind = TYPE_PTR;
            return val;
        }

        IRValue operand = codegen_expr(n->unary.operand, ctx_type);
        val.type = operand.type;
        val.reg = ir_tmp();

        if (n->unary.op == OP_NEG) {
            if (operand.type.kind == TYPE_FIELD) {
                uint64_t p = operand.type.field_prime;
                const char *el = field_elem_ir(p);
                FieldInfo *fi_neg = (operand.type.field_idx >= 0) ?
                    &g_fields[operand.type.field_idx] : find_field_by_prime(p);
                if (fi_neg && fi_neg->field_kind == FIELD_KIND_BINARY) {
                    /* In characteristic 2, -x = x (every element is its own additive inverse) */
                    val.reg = operand.reg;
                } else if (fi_neg && fi_neg->field_kind == FIELD_KIND_EXTENSION) {
                    /* -ext = ext_sub({0,0}, x) */
                    const char *et = fi_neg->ext_elem_ir;
                    int zero = ir_tmp();
                    ir_emit("  %%t%d = insertvalue %s undef, %s 0, 0\n", zero, et, el);
                    int zero2 = ir_tmp();
                    ir_emit("  %%t%d = insertvalue %s %%t%d, %s 0, 1\n", zero2, et, zero, el);
                    ir_emit("  %%t%d = call %s @ext%llu_sub(%s %%t%d, %s %%t%d)\n",
                            val.reg, et, (unsigned long long)p, et, zero2, et, operand.reg);
                } else {
                    /* -x = p - x = sub(0, x) */
                    int zero = ir_tmp();
                    ir_emit("  %%t%d = add %s 0, 0\n", zero, el);
                    ir_emit("  %%t%d = call %s @field%llu_sub(%s %%t%d, %s %%t%d)\n",
                            val.reg, el, (unsigned long long)p, el, zero, el, operand.reg);
                }
            } else {
                ir_emit("  %%t%d = sub %s 0, %%t%d\n", val.reg,
                        type_to_ir(operand.type), operand.reg);
            }
        } else if (n->unary.op == OP_NOT) {
            ir_emit("  %%t%d = xor i1 %%t%d, 1\n", val.reg, operand.reg);
            val.type.kind = TYPE_BOOL;
        } else if (n->unary.op == OP_BITNOT) {
            /* ~x = xor x, -1 (LLVM treats -1 as all-ones for any integer width) */
            ir_emit("  %%t%d = xor %s %%t%d, -1\n", val.reg,
                    type_to_ir(operand.type), operand.reg);
            /* val.type stays as operand.type — preserves integer width */
        }
        return val;
    }

    case AST_CALL: {
        if (!strcmp(n->call.name, "print") && !find_func(n->call.name)) {
            /* print(expr) -> printf */
            if (n->call.nargs != 1) {
                error(n->line, n->col, "print() takes exactly 1 argument");
            }
            /* If ctx_type is void (e.g. inside fn main()), default to i32
               so that integer literal arguments get a concrete IR type
               instead of emitting 'add void 0, 0'. */
            Type print_ctx = ctx_type;
            if (print_ctx.kind == TYPE_VOID) {
                print_ctx.kind = TYPE_I32;
            }
            IRValue arg = codegen_expr(n->call.args[0], print_ctx);

            /* print(*u8) — string: use puts */
            if (arg.type.kind == TYPE_PTR) {
                val.reg = ir_tmp();
                ir_emit("  %%t%d = call i32 @puts(ptr %%t%d)\n", val.reg, arg.reg);
                val.type.kind = TYPE_VOID;
                return val;
            }

            /* Extension field print: extract both components, print each */
            if (arg.type.kind == TYPE_FIELD && arg.type.field_idx >= 0 &&
                arg.type.field_idx < g_nfields &&
                g_fields[arg.type.field_idx].field_kind == FIELD_KIND_EXTENSION) {
                FieldInfo *ext_fi = &g_fields[arg.type.field_idx];
                const char *et = ext_fi->ext_elem_ir;
                const char *el = ext_fi->elem_ir;
                int c0 = ir_tmp();
                ir_emit("  %%t%d = extractvalue %s %%t%d, 0\n", c0, et, arg.reg);
                int c1 = ir_tmp();
                ir_emit("  %%t%d = extractvalue %s %%t%d, 1\n", c1, et, arg.reg);
                /* Print each component */
                int ext0 = c0, ext1 = c1;
                bool use_i64 = (ext_fi->elem_bits == 64);
                if (ext_fi->elem_bits < 32) {
                    ext0 = ir_tmp();
                    ir_emit("  %%t%d = zext %s %%t%d to i32\n", ext0, el, c0);
                    ext1 = ir_tmp();
                    ir_emit("  %%t%d = zext %s %%t%d to i32\n", ext1, el, c1);
                }
                val.reg = ir_tmp();
                if (use_i64) {
                    ir_emit("  %%t%d = call i32 (i8*, ...) @printf(i8* getelementptr ([6 x i8], [6 x i8]* @.fmt_llu, i32 0, i32 0), i64 %%t%d)\n",
                            val.reg, ext0);
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = call i32 (i8*, ...) @printf(i8* getelementptr ([6 x i8], [6 x i8]* @.fmt_llu, i32 0, i32 0), i64 %%t%d)\n",
                            val.reg, ext1);
                } else {
                    ir_emit("  %%t%d = call i32 (i8*, ...) @printf(i8* getelementptr ([4 x i8], [4 x i8]* @.fmt_u, i32 0, i32 0), i32 %%t%d)\n",
                            val.reg, ext0);
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = call i32 (i8*, ...) @printf(i8* getelementptr ([4 x i8], [4 x i8]* @.fmt_u, i32 0, i32 0), i32 %%t%d)\n",
                            val.reg, ext1);
                }
                val.type.kind = TYPE_VOID;
                return val;
            }

            int ext = ir_tmp();
            const char *ir_ty = type_to_ir(arg.type);

            /* Extend/truncate to printf argument width.
             * For types narrower than i32: zext to i32, printf %u.
             * For i32: use directly, printf %u.
             * For i64 (64-bit fields): use i64, printf %llu. */
            bool use_i64_fmt = false;
            if (arg.type.kind == TYPE_FIELD) {
                FieldInfo *fi_print = find_field_by_prime(arg.type.field_prime);
                if (fi_print && fi_print->elem_bits == 64) {
                    use_i64_fmt = true;
                    ext = arg.reg;  /* already i64 */
                } else if (fi_print && fi_print->elem_bits >= 32) {
                    ext = arg.reg;  /* already i32 */
                } else {
                    ir_emit("  %%t%d = zext %s %%t%d to i32\n", ext, ir_ty, arg.reg);
                }
            } else if (arg.type.kind == TYPE_U8 || arg.type.kind == TYPE_I8 ||
                       arg.type.kind == TYPE_U16 || arg.type.kind == TYPE_I16) {
                ir_emit("  %%t%d = zext %s %%t%d to i32\n", ext, ir_ty, arg.reg);
            } else if (arg.type.kind == TYPE_BOOL) {
                ir_emit("  %%t%d = zext i1 %%t%d to i32\n", ext, arg.reg);
            } else if (arg.type.kind == TYPE_U64 || arg.type.kind == TYPE_I64 ||
                       arg.type.kind == TYPE_DYNFIELD) {
                use_i64_fmt = true;
                ext = arg.reg;
            } else {
                ext = arg.reg;
            }

            val.reg = ir_tmp();
            bool use_signed_fmt = (arg.type.kind == TYPE_I8 || arg.type.kind == TYPE_I16 ||
                                   arg.type.kind == TYPE_I32 || arg.type.kind == TYPE_I64);
            if (use_i64_fmt && use_signed_fmt) {
                ir_emit("  %%t%d = call i32 (i8*, ...) @printf(i8* getelementptr ([6 x i8], [6 x i8]* @.fmt_lld, i32 0, i32 0), i64 %%t%d)\n",
                        val.reg, ext);
            } else if (use_i64_fmt) {
                ir_emit("  %%t%d = call i32 (i8*, ...) @printf(i8* getelementptr ([6 x i8], [6 x i8]* @.fmt_llu, i32 0, i32 0), i64 %%t%d)\n",
                        val.reg, ext);
            } else if (use_signed_fmt) {
                ir_emit("  %%t%d = call i32 (i8*, ...) @printf(i8* getelementptr ([4 x i8], [4 x i8]* @.fmt_d, i32 0, i32 0), i32 %%t%d)\n",
                        val.reg, ext);
            } else {
                ir_emit("  %%t%d = call i32 (i8*, ...) @printf(i8* getelementptr ([4 x i8], [4 x i8]* @.fmt_u, i32 0, i32 0), i32 %%t%d)\n",
                        val.reg, ext);
            }
            val.type.kind = TYPE_VOID;
            return val;
        }

        /* alloc(count) — heap allocation. Type inferred from ctx_type.
         * ctx_type must be *T. Allocates count * sizeof(T) bytes.
         * Returns ptr. Traps on OOM. Zero-initializes. */
        if (!strcmp(n->call.name, "alloc")) {
            if (n->call.nargs != 1) {
                error(n->line, n->col, "alloc() takes exactly 1 argument (count)");
            }
            if (ctx_type.kind != TYPE_PTR) {
                error(n->line, n->col, "alloc() requires pointer context type (let p: *T = alloc(n))");
            }
            Type pointee = type_pointee(ctx_type);
            int elem_size = type_sizeof(pointee);

            /* Evaluate count argument */
            Type usize_ty = {.kind = TYPE_USIZE};
            IRValue count = codegen_expr(n->call.args[0], usize_ty);

            /* Widen count to i64 if narrower (e.g., alloc(n) where n: i32) */
            int count_bits = type_ir_bitwidth(count.type);
            if (count_bits > 0 && count_bits < 64) {
                int widened = ir_tmp();
                ir_emit("  %%t%d = %s %s %%t%d to i64\n", widened,
                        type_is_signed(count.type) ? "sext" : "zext",
                        type_to_ir(count.type), count.reg);
                count.reg = widened;
            }

            /* Compute total bytes: count * sizeof(T) */
            int total = ir_tmp();
            ir_emit("  %%t%d = mul i64 %%t%d, %d\n", total, count.reg, elem_size);

            /* Call malloc */
            int ptr = ir_tmp();
            ir_emit("  %%t%d = call ptr @malloc(i64 %%t%d)\n", ptr, total);

            /* Null check → abort on OOM */
            int is_null = ir_tmp();
            ir_emit("  %%t%d = icmp eq ptr %%t%d, null\n", is_null, ptr);
            int lbl_oom = ir_label();
            int lbl_ok = ir_label();
            ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n", is_null, lbl_oom, lbl_ok);
            ir_emit("L%d:\n", lbl_oom);
            ir_emit("  call void @abort()\n");
            ir_emit("  unreachable\n");
            ir_emit("L%d:\n", lbl_ok);

            /* Zero-initialize */
            ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %%t%d, i1 false)\n", ptr, total);

            val.reg = ptr;
            val.type = ctx_type;
            return val;
        }

        /* free(ptr) — heap deallocation */
        if (!strcmp(n->call.name, "free")) {
            if (n->call.nargs != 1) {
                error(n->line, n->col, "free() takes exactly 1 argument");
            }
            Type ptr_ctx = ctx_type;
            if (ptr_ctx.kind == TYPE_VOID) { ptr_ctx.kind = TYPE_PTR; }
            IRValue arg = codegen_expr(n->call.args[0], ptr_ctx);
            ir_emit("  call void @free(ptr %%t%d)\n", arg.reg);
            val.type.kind = TYPE_VOID;
            return val;
        }

        /* realloc(ptr, count) — resize a heap block (A6).  Mirrors alloc's
         * element-size-from-context sizing; preserves old contents (new bytes
         * uninitialized).  Only mirrored into the frozen bootstrap so it can
         * compile the canonical tvc_self.tv, whose growable arenas use it. */
        if (!strcmp(n->call.name, "realloc")) {
            if (n->call.nargs != 2) {
                error(n->line, n->col, "realloc() takes exactly 2 arguments (ptr, count)");
            }
            if (ctx_type.kind != TYPE_PTR) {
                error(n->line, n->col, "realloc() requires pointer context type (p = realloc(p, n))");
            }
            Type pointee = type_pointee(ctx_type);
            int elem_size = type_sizeof(pointee);

            Type ptr_ctx = ctx_type;
            IRValue oldp = codegen_expr(n->call.args[0], ptr_ctx);

            Type usize_ty = {.kind = TYPE_USIZE};
            IRValue count = codegen_expr(n->call.args[1], usize_ty);
            int count_bits = type_ir_bitwidth(count.type);
            if (count_bits > 0 && count_bits < 64) {
                int widened = ir_tmp();
                ir_emit("  %%t%d = %s %s %%t%d to i64\n", widened,
                        type_is_signed(count.type) ? "sext" : "zext",
                        type_to_ir(count.type), count.reg);
                count.reg = widened;
            }

            int total = ir_tmp();
            ir_emit("  %%t%d = mul i64 %%t%d, %d\n", total, count.reg, elem_size);

            int ptr = ir_tmp();
            ir_emit("  %%t%d = call ptr @realloc(ptr %%t%d, i64 %%t%d)\n", ptr, oldp.reg, total);

            int is_null = ir_tmp();
            ir_emit("  %%t%d = icmp eq ptr %%t%d, null\n", is_null, ptr);
            int lbl_oom = ir_label();
            int lbl_ok = ir_label();
            ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n", is_null, lbl_oom, lbl_ok);
            ir_emit("L%d:\n", lbl_oom);
            ir_emit("  call void @abort()\n");
            ir_emit("  unreachable\n");
            ir_emit("L%d:\n", lbl_ok);

            val.reg = ptr;
            val.type = ctx_type;
            return val;
        }

        /* signed(x) — balanced/signed interpretation of field element.
         * Maps x → x if x <= (p-1)/2, x → x - p if x > (p-1)/2.
         * Returns i32 (or i64 for 64-bit fields). */
        if (!strcmp(n->call.name, "signed") && !find_func(n->call.name)) {
            if (n->call.nargs != 1) {
                error(n->line, n->col, "signed() takes exactly 1 argument");
            }
            IRValue arg = codegen_expr(n->call.args[0], ctx_type);
            if (arg.type.kind == TYPE_DYNFIELD) {
                /* Dyn: load p and half_p from %__field, branchless select.
                 * Returns i64 (the prime may be 64-bit). */
                int fv = load_dyn_field_ptr(n->line, n->col);
                int pp = ir_tmp();
                ir_emit("  %%t%d = getelementptr %%__Field, ptr %%t%d, i32 0, i32 0\n",
                        pp, fv);
                int p = ir_tmp();
                ir_emit("  %%t%d = load i64, ptr %%t%d\n", p, pp);
                int hpp = ir_tmp();
                ir_emit("  %%t%d = getelementptr %%__Field, ptr %%t%d, i32 0, i32 1\n",
                        hpp, fv);
                int hp = ir_tmp();
                ir_emit("  %%t%d = load i64, ptr %%t%d\n", hp, hpp);
                int cmp = ir_tmp();
                ir_emit("  %%t%d = icmp ugt i64 %%t%d, %%t%d\n", cmp, arg.reg, hp);
                int neg = ir_tmp();
                ir_emit("  %%t%d = sub i64 %%t%d, %%t%d\n", neg, arg.reg, p);
                val.reg = ir_tmp();
                ir_emit("  %%t%d = select i1 %%t%d, i64 %%t%d, i64 %%t%d\n",
                        val.reg, cmp, neg, arg.reg);
                val.type.kind = TYPE_I64;
                return val;
            }
            if (arg.type.kind != TYPE_FIELD) {
                error(n->line, n->col, "signed() requires a field element argument");
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i32 0, 0\n", val.reg);
                val.type.kind = TYPE_I32;
                return val;
            }
            uint64_t p = arg.type.field_prime;
            FieldInfo *fi_s = find_field_by_prime(p);
            const char *el = field_elem_ir(p);
            const char *result_ty = (fi_s && fi_s->elem_bits == 64) ? "i64" : "i32";
            uint64_t half = (p - 1) / 2;

            /* zext to result type */
            int ext = ir_tmp();
            if (fi_s && fi_s->elem_bits < 32) {
                ir_emit("  %%t%d = zext %s %%t%d to %s\n", ext, el, arg.reg, result_ty);
            } else if (fi_s && fi_s->elem_bits == 32 && !strcmp(result_ty, "i32")) {
                ext = arg.reg;  /* already i32 */
            } else {
                ir_emit("  %%t%d = zext %s %%t%d to %s\n", ext, el, arg.reg, result_ty);
            }

            /* compare: x > (p-1)/2 */
            int cmp = ir_tmp();
            ir_emit("  %%t%d = icmp ugt %s %%t%d, %llu\n", cmp, result_ty, ext,
                    (unsigned long long)half);

            /* x - p (as signed: subtract p, which wraps to negative in signed interpretation) */
            int neg = ir_tmp();
            ir_emit("  %%t%d = sub %s %%t%d, %llu\n", neg, result_ty, ext,
                    (unsigned long long)p);

            /* select */
            val.reg = ir_tmp();
            ir_emit("  %%t%d = select i1 %%t%d, %s %%t%d, %s %%t%d\n",
                    val.reg, cmp, result_ty, neg, result_ty, ext);

            val.type.kind = !strcmp(result_ty, "i64") ? TYPE_I64 : TYPE_I32;
            return val;
        }

        /* User function call — look up return type from registry */
        FuncInfo *fi = find_func(n->call.name);
        Type call_ret = ctx_type;

        /* Arity check (#26): a fixed-arity, non-generic user function must
         * receive exactly its declared parameter count.  A dyn instance whose
         * first declared param is the implicit "Field" carrier (supplied from
         * __field) may omit it, so nparams-1 is also valid — mirrors the
         * prepend_carrier rule below.  Generics and externs are exempt (the
         * seed has no variadic-extern surface).  Catches both under- and
         * over-arity.  Mirrors the tvc_self.tv #26 guard; the frozen-seed rule
         * permits adding safety guards. */
        if (fi && !fi->is_generic && !fi->is_extern) {
            bool carrier_opt = (fi->nparams >= 1 &&
                                !strcmp(fi->param_types[0], "Field") &&
                                sym_find("__field") != NULL);
            if (n->call.nargs != fi->nparams &&
                !(carrier_opt && n->call.nargs == fi->nparams - 1)) {
                error(n->line, n->col,
                      "wrong number of arguments to '%s': expected %d, found %d",
                      n->call.name, fi->nparams, n->call.nargs);
            }
        }

        /* Generic function: monomorphize if needed.
         * Infer concrete types from call arguments (first field-typed arg wins).
         * After monomorphization, redirect fi to the monomorphized FuncInfo. */
        const char *call_target = n->call.name;
        if (fi && fi->is_generic) {
            /* Infer concrete types for each generic parameter.
             * Strategy: scan parameter types, find generic param names,
             * match against context type or pre-codegen the first arg to get its type. */
            char inferred[MAX_GENERICS][MAX_IDENT];
            bool inferred_ok[MAX_GENERICS];
            for (int gi = 0; gi < fi->ngen; gi++) inferred_ok[gi] = false;

            /* Zeroth pass: dyn context propagation.  Inside a dyn-
             * substituted body the caller's generic params resolve to
             * TYPE_DYNFIELD; a nested generic call inherits "dyn" for
             * any of its params matching the context type or any
             * dyn-typed argument (checked in the passes below). */
            if (ctx_type.kind == TYPE_DYNFIELD) {
                for (int gi = 0; gi < fi->ngen; gi++) {
                    if (!strcmp(fi->ret_type, fi->gen_names[gi]) && !inferred_ok[gi]) {
                        strcpy(inferred[gi], "dyn");
                        inferred_ok[gi] = true;
                    }
                }
            }

            /* First pass: check context type (return type context).
             * If ctx_type is a field, try to match it to generic params. */
            if (ctx_type.kind == TYPE_FIELD && ctx_type.field_prime > 0) {
                for (int gi = 0; gi < fi->ngen; gi++) {
                    if (!strcmp(fi->ret_type, fi->gen_names[gi]) && !inferred_ok[gi]) {
                        FieldInfo *ctx_fi = (ctx_type.field_idx >= 0) ?
                            &g_fields[ctx_type.field_idx] : find_field_by_prime(ctx_type.field_prime);
                        if (ctx_fi && ctx_fi->field_kind == FIELD_KIND_BINARY) {
                            snprintf(inferred[gi], MAX_IDENT, "BinField<%d,0x%llX>",
                                     ctx_fi->degree, (unsigned long long)ctx_fi->poly);
                        } else if (ctx_fi && ctx_fi->field_kind == FIELD_KIND_EXTENSION) {
                            snprintf(inferred[gi], MAX_IDENT, "ExtField<Field<%llu>,%d>",
                                     (unsigned long long)ctx_fi->prime, ctx_fi->degree);
                        } else {
                            snprintf(inferred[gi], MAX_IDENT, "Field<%llu>",
                                     (unsigned long long)ctx_type.field_prime);
                        }
                        inferred_ok[gi] = true;
                    }
                }
            }

            /* Second pass: match param types to declared fields to infer remaining generics.
             * For each generic param, find the first function parameter using that type name.
             * Handles bare F, *F (pointer to F), and **F (double pointer). */
            for (int pi = 0; pi < fi->nparams && pi < n->call.nargs; pi++) {
                for (int gi = 0; gi < fi->ngen; gi++) {
                    if (inferred_ok[gi]) continue;

                    /* Check if this param type contains the generic param name.
                     * Bare: "F" matches "F"
                     * Pointer: "*F" — strip leading '*'s, check remainder */
                    const char *ptype = fi->param_types[pi];
                    while (*ptype == '*') ptype++;  /* strip pointer prefix */
                    if (strcmp(ptype, fi->gen_names[gi]) != 0) continue;

                    /* This param uses generic type gi.
                     * Determine the concrete type from the argument expression. */
                    ASTNode *arg = n->call.args[pi];
                    Type arg_type = {0};
                    if (arg->kind == AST_IDENT) {
                        Symbol *s = sym_find(arg->ident.name);
                        if (s) arg_type = s->type;
                    } else if (arg->kind == AST_UNARY && arg->unary.op == OP_ADDR) {
                        /* &expr — the result is a pointer; we need the pointee type.
                         * Look up the inner expression. */
                        ASTNode *inner = arg->unary.operand;
                        if (inner->kind == AST_INDEX) {
                            /* &arr[i] — look up arr's element type */
                            Symbol *s = sym_find(inner->index_expr.name);
                            if (s && s->type.kind == TYPE_ARRAY) {
                                arg_type.kind = TYPE_FIELD;
                                arg_type.field_prime = s->type.field_prime;
                            }
                        } else if (inner->kind == AST_IDENT) {
                            Symbol *s = sym_find(inner->ident.name);
                            if (s) arg_type = s->type;
                        }
                    } else if (arg->kind == AST_LIT_INT) {
                        arg_type = ctx_type;
                    }

                    /* Follow pointers to find the field type */
                    if (arg_type.kind == TYPE_PTR && arg_type.ptr_field_prime > 0) {
                        arg_type.kind = TYPE_FIELD;
                        arg_type.field_prime = arg_type.ptr_field_prime;
                        arg_type.field_idx = arg_type.ptr_field_idx;
                    }

                    /* Dyn element or dyn pointee: nested generic call
                     * inside a dyn instance — propagate "dyn". */
                    if (arg_type.kind == TYPE_DYNFIELD ||
                        (arg_type.kind == TYPE_PTR &&
                         arg_type.ptr_pointee_kind == TYPE_DYNFIELD)) {
                        strcpy(inferred[gi], "dyn");
                        inferred_ok[gi] = true;
                        continue;
                    }

                    if (arg_type.kind == TYPE_FIELD && arg_type.field_prime > 0) {
                        FieldInfo *arg_fi = (arg_type.field_idx >= 0) ?
                            &g_fields[arg_type.field_idx] : find_field_by_prime(arg_type.field_prime);
                        if (arg_fi && arg_fi->field_kind == FIELD_KIND_BINARY) {
                            snprintf(inferred[gi], MAX_IDENT, "BinField<%d,0x%llX>",
                                     arg_fi->degree, (unsigned long long)arg_fi->poly);
                        } else if (arg_fi && arg_fi->field_kind == FIELD_KIND_EXTENSION) {
                            snprintf(inferred[gi], MAX_IDENT, "ExtField<Field<%llu>,%d>",
                                     (unsigned long long)arg_fi->prime, arg_fi->degree);
                        } else {
                            snprintf(inferred[gi], MAX_IDENT, "Field<%llu>",
                                     (unsigned long long)arg_type.field_prime);
                        }
                        inferred_ok[gi] = true;
                    }
                }
            }

            /* Check all generics inferred */
            for (int gi = 0; gi < fi->ngen; gi++) {
                if (!inferred_ok[gi]) {
                    error(n->line, n->col,
                          "cannot infer type for generic parameter '%s' in call to '%s'",
                          fi->gen_names[gi], fi->name);
                    strcpy(inferred[gi], "Field<251>"); /* fallback to avoid crash */
                }
            }

            /* Monomorphize and redirect */
            call_target = monomorphize(fi, (const char (*)[MAX_IDENT])inferred);
            fi = find_func(call_target);  /* now points to monomorphized version */
        }

        if (fi) {
            call_ret = resolve_type(fi->ret_type);
        }

        /* poly(c0, c1, ..., cd) — construct polynomial from standard coefficients */
        if (!strcmp(n->call.name, "poly")) {
            int nargs = n->call.nargs;
            if (nargs < 1) {
                error(n->line, n->col, "poly() needs at least 1 argument");
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
                return val;
            }
            /* Determine field prime from context */
            uint64_t prime = ctx_type.field_prime;
            if (prime == 0) {
                error(n->line, n->col, "poly() requires field context (use Poly<Field<p>, d> type)");
                prime = 251; /* continue with fallback to avoid crash */
            }
            const char *el = field_elem_ir(prime);

            /* Alloca an [nargs x el] array */
            int alloca_reg = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", alloca_reg, nargs, el);
            ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                    alloca_reg, nargs);

            /* Codegen each argument and store it */
            Type ftype = {.kind = TYPE_FIELD, .field_prime = prime};
            for (int i = 0; i < nargs; i++) {
                IRValue av = codegen_expr(n->call.args[i], ftype);
                int i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", i64, i);
                int gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        gep, nargs, el, alloca_reg, i64);
                ir_emit("  store %s %%t%d, ptr %%t%d\n", el, av.reg, gep);
            }

            val.type.kind = TYPE_POLY;
            val.type.poly_degree = nargs - 1;
            val.type.field_prime = prime;
            val.reg = alloca_reg;
            g_last_repr = REPR_STANDARD;
            return val;
        }

        /* register(c0, c1, ..., cd) — construct Register<F, d> from Newton coefficients */
        /* field(p) / field(p, data_max) — construct a runtime Field value
         * (ptr to %__Field).  Emits @__field_init: validates primality
         * (Miller-Rabin) and computes the Barrett factor at runtime.
         * One-time per prime.  The optional second argument sets
         * data_bytes (raw-data byte width for wire-format literal
         * blocks) independently of elem_bytes; omitted = derive from p. */
        if (!strcmp(n->call.name, "field") && !find_func(n->call.name)) {
            if (n->call.nargs < 1 || n->call.nargs > 2) {
                error(n->line, n->col, "field() takes 1 or 2 arguments (prime[, data_max])");
                val.reg = ir_tmp();
                ir_emit("  %%t%d = inttoptr i64 0 to ptr\n", val.reg);
                val.type = (Type){.kind = TYPE_FIELD_VALUE};
                return val;
            }
            IRValue p_arg = codegen_expr(n->call.args[0], (Type){.kind = TYPE_I64});
            int p64 = p_arg.reg;
            int p_bits = type_ir_bitwidth(p_arg.type);
            if (p_bits > 0 && p_bits < 64) {
                p64 = ir_tmp();
                ir_emit("  %%t%d = zext %s %%t%d to i64\n", p64,
                        type_to_ir(p_arg.type), p_arg.reg);
            }
            /* data_max: explicit second arg, or literal 0 (= derive) */
            int dm64;
            if (n->call.nargs == 2) {
                IRValue dm_arg = codegen_expr(n->call.args[1], (Type){.kind = TYPE_I64});
                dm64 = dm_arg.reg;
                int dm_bits = type_ir_bitwidth(dm_arg.type);
                if (dm_bits > 0 && dm_bits < 64) {
                    dm64 = ir_tmp();
                    ir_emit("  %%t%d = zext %s %%t%d to i64\n", dm64,
                            type_to_ir(dm_arg.type), dm_arg.reg);
                }
            } else {
                dm64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, 0\n", dm64);
            }
            val.reg = ir_tmp();
            ir_emit("  %%t%d = call ptr @__field_init(i64 %%t%d, i64 %%t%d)\n",
                    val.reg, p64, dm64);
            val.type = (Type){.kind = TYPE_FIELD_VALUE};
            return val;
        }

        if (!strcmp(n->call.name, "register")) {
            int nargs = n->call.nargs;
            if (nargs < 1) {
                error(n->line, n->col, "register() needs at least 1 argument");
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
                return val;
            }

            /* ---- Dyn register path ----
             * Detected when ctx_type says Register<dyn,d> (elem_kind == TYPE_DYNFIELD).
             * Two constructor forms:
             *   Explicit: register(field_val, c0, c1, ..., cd) — arg0 is Field carrier
             *   Implicit: register(c0, c1, ..., cd)           — carrier from %__field
             * Layout: { ptr (carrier), [ncoeffs x i64] } */
            if (ctx_type.elem_kind == TYPE_DYNFIELD) {
                /* Probe arg0: codegen as i64 (neutral); if it resolves to
                 * TYPE_FIELD_VALUE we have the explicit-carrier form. */
                IRValue arg0 = codegen_expr(n->call.args[0], (Type){.kind = TYPE_I64});
                bool explicit_carrier = (arg0.type.kind == TYPE_FIELD_VALUE);
                int carrier_reg;  /* ptr to %__Field */
                int coeff_start;  /* first coefficient arg index */
                int ncoeffs;

                if (explicit_carrier) {
                    carrier_reg = arg0.reg;
                    coeff_start = 1;
                    ncoeffs = nargs - 1;
                } else {
                    /* Implicit: get carrier from dyn context (%__field) */
                    carrier_reg = load_dyn_field_ptr(n->line, n->col);
                    if (carrier_reg < 0) {
                        error(n->line, n->col,
                              "dyn register() needs either a Field first argument or a dyn context (__field)");
                        val.reg = ir_tmp();
                        ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
                        return val;
                    }
                    coeff_start = 0;
                    ncoeffs = nargs;
                }

                if (ncoeffs < 1) {
                    error(n->line, n->col, "register() needs at least 1 coefficient");
                    val.reg = ir_tmp();
                    ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
                    return val;
                }

                /* Alloca the fat register: { ptr, [ncoeffs x i64] } */
                int struct_size = 8 + ncoeffs * 8;
                int alloca_reg = ir_tmp();
                char ir_ty[64];
                snprintf(ir_ty, sizeof(ir_ty), "{ ptr, [%d x i64] }", ncoeffs);
                ir_emit_alloca("  %%t%d = alloca %s\n", alloca_reg, ir_ty);
                ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                        alloca_reg, struct_size);

                /* Store carrier ptr at index 0 */
                int carrier_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 0\n",
                        carrier_gep, ir_ty, alloca_reg);
                ir_emit("  store ptr %%t%d, ptr %%t%d\n", carrier_reg, carrier_gep);

                /* Load p from carrier for coefficient reduction */
                int pp_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr %%__Field, ptr %%t%d, i32 0, i32 0\n",
                        pp_gep, carrier_reg);
                int p_val = ir_tmp();
                ir_emit("  %%t%d = load i64, ptr %%t%d\n", p_val, pp_gep);

                /* Store coefficients at struct index 1 (the [ncoeffs x i64] array) */
                Type dyn_elem = {.kind = TYPE_DYNFIELD};
                for (int i = 0; i < ncoeffs; i++) {
                    IRValue av;
                    if (i == 0 && !explicit_carrier) {
                        /* arg0 was already codegen'd above (the probe) */
                        av = arg0;
                    } else {
                        av = codegen_expr(n->call.args[coeff_start + i], dyn_elem);
                    }
                    /* Widen to i64 if narrower */
                    int av_reg = av.reg;
                    int av_bits = type_ir_bitwidth(av.type);
                    if (av_bits > 0 && av_bits < 64) {
                        av_reg = ir_tmp();
                        ir_emit("  %%t%d = zext %s %%t%d to i64\n", av_reg,
                                type_to_ir(av.type), av.reg);
                    }
                    /* Reduce mod p */
                    int reduced = ir_tmp();
                    ir_emit("  %%t%d = urem i64 %%t%d, %%t%d\n", reduced, av_reg, p_val);
                    /* GEP into the coefficient array */
                    int arr_gep = ir_tmp();
                    ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 1, i64 %d\n",
                            arr_gep, ir_ty, alloca_reg, i);
                    ir_emit("  store i64 %%t%d, ptr %%t%d\n", reduced, arr_gep);
                }

                val.type.kind = TYPE_REGISTER;
                val.type.elem_kind = TYPE_DYNFIELD;
                val.type.register_degree = ncoeffs - 1;
                val.type.field_prime = 0;
                val.reg = alloca_reg;
                return val;
            }

            /* ---- Baked register path (existing) ---- */
            uint64_t prime = ctx_type.field_prime;
            if (prime == 0) {
                error(n->line, n->col, "register() requires field context (use Register<Field<p>, d> type)");
                prime = 251;
            }
            const char *el = field_elem_ir(prime);

            /* Alloca an [nargs x el] array */
            int alloca_reg = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", alloca_reg, nargs, el);
            ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                    alloca_reg, nargs);

            /* Codegen each argument (Newton coefficients) and store */
            Type ftype = {.kind = TYPE_FIELD, .field_prime = prime};
            for (int i = 0; i < nargs; i++) {
                IRValue av = codegen_expr(n->call.args[i], ftype);
                int i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", i64, i);
                int gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        gep, nargs, el, alloca_reg, i64);
                ir_emit("  store %s %%t%d, ptr %%t%d\n", el, av.reg, gep);
            }

            val.type.kind = TYPE_REGISTER;
            val.type.register_degree = nargs - 1;
            val.type.field_prime = prime;
            val.reg = alloca_reg;
            return val;
        }

        /* advance(reg) — return state[0] then advance register by d additions.
         * Phase transition semantics: value is emitted at the current point,
         * then state advances to the next point. */
        /* A user-defined fn named `advance` (any arity — e.g. a zero-arg parser
         * helper) shadows the builtin; fall through to normal call resolution.
         * Mirrors the tvc_self guard (find_func(call_name) < 0). */
        if (!strcmp(n->call.name, "advance") && !find_func(n->call.name)) {
            if (n->call.nargs != 1) {
                error(n->line, n->col, "advance() takes 1 argument (register)");
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
                return val;
            }
            /* Codegen the register argument — returns alloca pointer */
            Type reg_ctx = ctx_type;
            if (reg_ctx.kind != TYPE_REGISTER) {
                /* Try to infer from argument */
                reg_ctx.kind = TYPE_REGISTER;
            }
            IRValue reg_val = codegen_expr(n->call.args[0], reg_ctx);
            uint64_t prime = reg_val.type.field_prime;
            int deg = reg_val.type.register_degree;
            /* Accept *Register<F, d> (from method call: reg.advance() -> advance(&reg)) */
            if (reg_val.type.kind == TYPE_PTR && reg_val.type.ptr_pointee_kind == TYPE_REGISTER) {
                /* Load the pointer to get the register's alloca */
                prime = reg_val.type.ptr_field_prime;
                /* For pointer-to-register, the degree info isn't in the ptr type.
                 * We need to look up the original symbol. The arg is &ident. */
                ASTNode *arg0 = n->call.args[0];
                if (arg0->kind == AST_UNARY && arg0->unary.op == OP_ADDR) {
                    ASTNode *inner = arg0->unary.operand;
                    if (inner->kind == AST_IDENT) {
                        Symbol *sym = sym_find(inner->ident.name);
                        if (sym && sym->type.kind == TYPE_REGISTER) {
                            prime = sym->type.field_prime;
                            deg = sym->type.register_degree;
                            reg_val.reg = sym->ir_reg;
                            reg_val.type = sym->type;
                        }
                    }
                }
            }
            if (reg_val.type.kind != TYPE_REGISTER) {
                error(n->line, n->col, "advance() argument must be a Register type");
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
                return val;
            }

            /* ---- Dyn register advance ----
             * Layout: { ptr (carrier), [d+1 x i64] (coefficients) }
             * Load carrier from header, use @field_dyn_add for state update. */
            if (reg_val.type.elem_kind == TYPE_DYNFIELD) {
                int n_elems = deg + 1;
                char ir_ty[64];
                snprintf(ir_ty, sizeof(ir_ty), "{ ptr, [%d x i64] }", n_elems);

                /* Load carrier ptr from struct index 0 */
                int carrier_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 0\n",
                        carrier_gep, ir_ty, reg_val.reg);
                int carrier = ir_tmp();
                ir_emit("  %%t%d = load ptr, ptr %%t%d\n", carrier, carrier_gep);

                /* 1. Load state[0] — return value (pre-advance) */
                int s0_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 1, i64 0\n",
                        s0_gep, ir_ty, reg_val.reg);
                int ret_val = ir_tmp();
                ir_emit("  %%t%d = load i64, ptr %%t%d\n", ret_val, s0_gep);

                /* 2. Advance: state[k] += state[k+1] via @field_dyn_add */
                for (int k = 0; k < deg; k++) {
                    int gep_k = ir_tmp();
                    ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 1, i64 %d\n",
                            gep_k, ir_ty, reg_val.reg, k);
                    int sk = ir_tmp();
                    ir_emit("  %%t%d = load i64, ptr %%t%d\n", sk, gep_k);

                    int gep_k1 = ir_tmp();
                    ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 1, i64 %d\n",
                            gep_k1, ir_ty, reg_val.reg, k + 1);
                    int sk1 = ir_tmp();
                    ir_emit("  %%t%d = load i64, ptr %%t%d\n", sk1, gep_k1);

                    int sum = ir_tmp();
                    ir_emit("  %%t%d = call i64 @field_dyn_add(i64 %%t%d, i64 %%t%d, ptr %%t%d)\n",
                            sum, sk, sk1, carrier);

                    ir_emit("  store i64 %%t%d, ptr %%t%d\n", sum, gep_k);
                }

                val.type.kind = TYPE_DYNFIELD;
                val.reg = ret_val;
                return val;
            }

            /* ---- Baked register advance (existing) ---- */
            const char *el = field_elem_ir(prime);
            int n_elems = deg + 1;

            emit_field_funcs(prime);

            /* 1. Load state[0] — this is the return value (pre-advance) */
            int gep0 = ir_tmp();
            ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 0\n",
                    gep0, n_elems, el, reg_val.reg);
            int ret_val = ir_tmp();
            ir_emit("  %%t%d = load %s, ptr %%t%d\n", ret_val, el, gep0);

            /* 2. Advance: state[k] += state[k+1] for k = 0..d-1 */
            for (int k = 0; k < deg; k++) {
                /* Load state[k] */
                int gep_k = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %d\n",
                        gep_k, n_elems, el, reg_val.reg, k);
                int sk = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", sk, el, gep_k);

                /* Load state[k+1] */
                int gep_k1 = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %d\n",
                        gep_k1, n_elems, el, reg_val.reg, k + 1);
                int sk1 = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", sk1, el, gep_k1);

                /* state[k] = field_add(state[k], state[k+1]) */
                int sum = ir_tmp();
                ir_emit("  %%t%d = call %s @field%llu_add(%s %%t%d, %s %%t%d)\n",
                        sum, el, (unsigned long long)prime, el, sk, el, sk1);

                /* Store back to state[k] */
                ir_emit("  store %s %%t%d, ptr %%t%d\n", el, sum, gep_k);
            }

            val.type.kind = TYPE_FIELD;
            val.type.field_prime = prime;
            val.reg = ret_val;
            return val;
        }

        /* analyze(data, n) — Newton forward differences -> polynomial */
        if (!strcmp(n->call.name, "analyze")) {
            if (n->call.nargs != 2) {
                error(n->line, n->col, "analyze() takes 2 arguments (data, n)");
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
                return val;
            }
            /* Get n from context type degree */
            int deg = ctx_type.poly_degree;
            int n_pts = deg + 1;
            uint64_t prime = ctx_type.field_prime;
            if (prime == 0) {
                error(n->line, n->col, "analyze() requires field context (use Poly<Field<p>, d> type)");
                prime = 251;
            }
            const char *el = field_elem_ir(prime);

            /* Arg 0: data array (get alloca reg) */
            ASTNode *data_arg = n->call.args[0];
            Symbol *data_sym = NULL;
            if (data_arg->kind == AST_IDENT) {
                data_sym = sym_find(data_arg->ident.name);
            }
            int data_reg;
            if (data_sym) {
                data_reg = data_sym->ir_reg;
            } else {
                IRValue dv = codegen_expr(data_arg, ctx_type);
                data_reg = dv.reg;
            }

            /* Alloca work buffer [n_pts x el] */
            int work_reg = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", work_reg, n_pts, el);

            /* Copy n_pts elements from data to work */
            for (int i = 0; i < n_pts; i++) {
                int i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", i64, i);
                /* source: data has type [arr_size x el], use data_sym type or guess */
                int src_gep = ir_tmp();
                if (data_sym) {
                    ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 0, i64 %%t%d\n",
                            src_gep, type_to_ir(data_sym->type), data_reg, i64);
                } else {
                    ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                            src_gep, n_pts, el, data_reg, i64);
                }
                int src_val = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", src_val, el, src_gep);
                int dst_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        dst_gep, n_pts, el, work_reg, i64);
                ir_emit("  store %s %%t%d, ptr %%t%d\n", el, src_val, dst_gep);
            }

            /* Alloca result buffer [n_pts x el] */
            int result_reg = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", result_reg, n_pts, el);
            ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                    result_reg, n_pts);

            /* Outer loop: level = 0..n_pts-1 */
            for (int level = 0; level < n_pts; level++) {
                /* result[level] = work[0] */
                int lv_i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", lv_i64, level);
                int w0_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 0\n",
                        w0_gep, n_pts, el, work_reg);
                int w0_val = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", w0_val, el, w0_gep);
                int r_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        r_gep, n_pts, el, result_reg, lv_i64);
                ir_emit("  store %s %%t%d, ptr %%t%d\n", el, w0_val, r_gep);

                /* Inner loop: i = 0..n_pts-2-level */
                int inner_lim = n_pts - 1 - level;
                for (int i = 0; i < inner_lim; i++) {
                    int wi_i64 = ir_tmp();
                    ir_emit("  %%t%d = add i64 0, %d\n", wi_i64, i);
                    int wi1_i64 = ir_tmp();
                    ir_emit("  %%t%d = add i64 0, %d\n", wi1_i64, i + 1);
                    int wi_gep = ir_tmp();
                    ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                            wi_gep, n_pts, el, work_reg, wi_i64);
                    int wi1_gep = ir_tmp();
                    ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                            wi1_gep, n_pts, el, work_reg, wi1_i64);
                    int wi_val = ir_tmp();
                    ir_emit("  %%t%d = load %s, ptr %%t%d\n", wi_val, el, wi_gep);
                    int wi1_val = ir_tmp();
                    ir_emit("  %%t%d = load %s, ptr %%t%d\n", wi1_val, el, wi1_gep);
                    int sub_val = ir_tmp();
                    ir_emit("  %%t%d = call %s @field%llu_sub(%s %%t%d, %s %%t%d)\n",
                            sub_val, el, (unsigned long long)prime, el, wi1_val, el, wi_val);
                    ir_emit("  store %s %%t%d, ptr %%t%d\n", el, sub_val, wi_gep);
                }
            }

            val.type.kind = TYPE_POLY;
            val.type.poly_degree = deg;
            val.type.field_prime = prime;
            val.reg = result_reg;
            g_last_repr = REPR_NEWTON;
            return val;
        }

        /* eval(poly, point) — evaluate polynomial via forward summation */
        if (!strcmp(n->call.name, "eval") && !find_func(n->call.name)) {
            if (n->call.nargs != 2) {
                error(n->line, n->col, "eval() takes 2 arguments (poly, point)");
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
                return val;
            }

            /* Arg 0: poly identifier */
            ASTNode *poly_arg = n->call.args[0];
            Symbol *poly_sym = NULL;
            if (poly_arg->kind == AST_IDENT) {
                poly_sym = sym_find(poly_arg->ident.name);
            }
            if (!poly_sym) {
                error(n->call.args[0]->line, n->call.args[0]->col,
                      "eval() first argument must be a poly variable");
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
                return val;
            }

            int deg;
            if (poly_sym->type.kind == TYPE_REGISTER)
                deg = poly_sym->type.register_degree;
            else
                deg = poly_sym->type.poly_degree;

            /* Dyn register/poly: eval() doesn't support dyn — use eval_at<dyn> */
            if (poly_sym->type.elem_kind == TYPE_DYNFIELD) {
                error(n->line, n->col,
                      "eval() does not support dyn registers; use eval_at<dyn> instead");
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i64 0, 0\n", val.reg);
                val.type.kind = TYPE_DYNFIELD;
                return val;
            }

            int n_coeffs = deg + 1;
            uint64_t prime = poly_sym->type.field_prime;
            const char *el = field_elem_ir(prime);

            /* Arg 1: evaluation point (integer) */
            IRValue pt_val = codegen_expr(n->call.args[1], (Type){.kind = TYPE_I32});

            /* Auto-insert conversion if poly is in standard form */
            int eval_src_reg = poly_sym->ir_reg;
            if (poly_sym->repr_tag == REPR_STANDARD) {
                /* Convert to Newton form first */
                int newton_tmp = ir_tmp();
                ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", newton_tmp, n_coeffs, el);
                ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                        newton_tmp, n_coeffs);
                int d_reg = ir_tmp();
                ir_emit("  %%t%d = add i32 0, %d\n", d_reg, deg);
                ir_emit("  call void @std_to_newton_F%llu(ptr %%t%d, ptr %%t%d, i32 %%t%d)\n",
                        (unsigned long long)prime, poly_sym->ir_reg, newton_tmp, d_reg);
                eval_src_reg = newton_tmp;
            }

            /* Load all d+1 coefficients into SSA registers */
            int coeff_regs[64];
            for (int i = 0; i < n_coeffs; i++) {
                int i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", i64, i);
                int gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        gep, n_coeffs, el, eval_src_reg, i64);
                coeff_regs[i] = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", coeff_regs[i], el, gep);
            }

            /* Perform forward summation: for step = 0..point-1:
             *   for k = 0..deg-1: reg[k] = add(reg[k], reg[k+1])
             * We need a dynamic loop over the point value.
             * Use alloca array for the working coefficients. */
            int step_work = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", step_work, n_coeffs, el);
            /* Store initial coefficients into step_work */
            for (int i = 0; i < n_coeffs; i++) {
                int i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", i64, i);
                int gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        gep, n_coeffs, el, step_work, i64);
                ir_emit("  store %s %%t%d, ptr %%t%d\n", el, coeff_regs[i], gep);
            }

            /* Outer loop: step = 0..point-1 */
            int step_alloca = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca i32\n", step_alloca);
            ir_emit("  store i32 0, ptr %%t%d\n", step_alloca);
            int lbl_step_cond = ir_label();
            int lbl_step_body = ir_label();
            int lbl_step_end  = ir_label();
            ir_emit("  br label %%L%d\n", lbl_step_cond);
            ir_emit("L%d:\n", lbl_step_cond);
            int step_cur = ir_tmp();
            ir_emit("  %%t%d = load i32, ptr %%t%d\n", step_cur, step_alloca);
            int step_cmp = ir_tmp();
            ir_emit("  %%t%d = icmp slt i32 %%t%d, %%t%d\n", step_cmp, step_cur, pt_val.reg);
            ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n",
                    step_cmp, lbl_step_body, lbl_step_end);
            ir_emit("L%d:\n", lbl_step_body);
            /* Inner: for k = 0..deg-1: work[k] = add(work[k], work[k+1]) */
            for (int k = 0; k < deg; k++) {
                int k_i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", k_i64, k);
                int k1_i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", k1_i64, k + 1);
                int wk_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        wk_gep, n_coeffs, el, step_work, k_i64);
                int wk1_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        wk1_gep, n_coeffs, el, step_work, k1_i64);
                int wk_val = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", wk_val, el, wk_gep);
                int wk1_val = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", wk1_val, el, wk1_gep);
                int new_val = ir_tmp();
                ir_emit("  %%t%d = call %s @field%llu_add(%s %%t%d, %s %%t%d)\n",
                        new_val, el, (unsigned long long)prime, el, wk_val, el, wk1_val);
                ir_emit("  store %s %%t%d, ptr %%t%d\n", el, new_val, wk_gep);
            }
            int step_next = ir_tmp();
            ir_emit("  %%t%d = add i32 %%t%d, 1\n", step_next, step_cur);
            ir_emit("  store i32 %%t%d, ptr %%t%d\n", step_next, step_alloca);
            ir_emit("  br label %%L%d\n", lbl_step_cond);
            ir_emit("L%d:\n", lbl_step_end);
            /* Result is work[0] */
            int r0_i64 = ir_tmp();
            ir_emit("  %%t%d = add i64 0, 0\n", r0_i64);
            int r0_gep = ir_tmp();
            ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                    r0_gep, n_coeffs, el, step_work, r0_i64);
            val.reg = ir_tmp();
            ir_emit("  %%t%d = load %s, ptr %%t%d\n", val.reg, el, r0_gep);
            val.type.kind = TYPE_FIELD;
            val.type.field_prime = prime;
            return val;
        }

        /* read_bytes(fd, buf, n) — POSIX read syscall */
        if (!strcmp(n->call.name, "read_bytes")) {
            if (n->call.nargs != 3) {
                error(n->line, n->col, "read_bytes() takes 3 arguments (fd, buf, n)");
            }
            /* fd: i32 */
            IRValue fd = codegen_expr(n->call.args[0], (Type){.kind = TYPE_I32});
            /* buf: array variable — pass its alloca pointer directly */
            ASTNode *buf_arg = n->call.args[1];
            Symbol *buf_sym = NULL;
            if (buf_arg->kind == AST_IDENT) {
                buf_sym = sym_find(buf_arg->ident.name);
            }
            int buf_reg;
            if (buf_sym) {
                buf_reg = buf_sym->ir_reg;
                /* For pointer types stored in alloca, load the pointer value.
                 * Array types: the alloca IS the buffer address (pass directly).
                 * Pointer types: the alloca HOLDS a pointer to the buffer (must load). */
                if (buf_sym->type.kind == TYPE_PTR && buf_sym->is_alloca) {
                    int loaded = ir_tmp();
                    ir_emit("  %%t%d = load ptr, ptr %%t%d\n", loaded, buf_reg);
                    buf_reg = loaded;
                }
            } else {
                IRValue buf_val = codegen_expr(buf_arg, (Type){.kind = TYPE_U8});
                buf_reg = buf_val.reg;
            }
            /* n: i64 */
            IRValue count = codegen_expr(n->call.args[2], (Type){.kind = TYPE_I64});
            int count64;
            if (count.type.kind == TYPE_I64 || count.type.kind == TYPE_U64) {
                count64 = count.reg;
            } else {
                count64 = ir_tmp();
                ir_emit("  %%t%d = sext %s %%t%d to i64\n", count64,
                        type_to_ir(count.type), count.reg);
            }

            val.reg = ir_tmp();
            ir_emit("  %%t%d = call i64 @read(i32 %%t%d, ptr %%t%d, i64 %%t%d)\n",
                    val.reg, fd.reg, buf_reg, count64);
            val.type.kind = TYPE_I64;
            return val;
        }

        /* __ntt_root(stage), __ntt_inv_root(stage), __ntt_n_inv(stage)
         * Compiler builtins for NTT: resolve to constant table loads.
         * The field is determined by ctx_type (set by monomorphization).
         * Emits getelementptr + load from the per-field constant table. */
        if (!strcmp(n->call.name, "__ntt_root") ||
            !strcmp(n->call.name, "__ntt_inv_root") ||
            !strcmp(n->call.name, "__ntt_n_inv")) {
            if (n->call.nargs != 1) {
                error(n->line, n->col, "%s() takes 1 argument (stage index)",
                      n->call.name);
                return val;
            }
            /* Determine which field we're in */
            uint64_t p = ctx_type.field_prime;
            if (ctx_type.kind != TYPE_FIELD || p == 0) {
                error(n->line, n->col,
                      "%s() requires field type context (use inside generic<F: Field>)",
                      n->call.name);
                return val;
            }
            FieldInfo *ntt_fi = find_field_by_prime(p);
            if (!ntt_fi || ntt_fi->ntt_max_log == 0) {
                error(n->line, n->col,
                      "Field<%llu> is not NTT-friendly (2-adic valuation of p-1 < 4)",
                      (unsigned long long)p);
                return val;
            }

            const char *el = ntt_fi->elem_ir;
            int nlog = ntt_fi->ntt_max_log;

            /* Select table name */
            const char *table_prefix;
            if (!strcmp(n->call.name, "__ntt_root")) {
                table_prefix = "ntt_roots";
            } else if (!strcmp(n->call.name, "__ntt_inv_root")) {
                table_prefix = "ntt_inv_roots";
            } else {
                table_prefix = "ntt_n_inv";
            }

            /* Codegen the stage index argument */
            IRValue idx = codegen_expr(n->call.args[0], (Type){.kind = TYPE_I32});
            int idx64 = ir_tmp();
            ir_emit("  %%t%d = sext i32 %%t%d to i64\n", idx64, idx.reg);

            /* Emit getelementptr + load from the constant table */
            int ptr_reg = ir_tmp();
            ir_emit("  %%t%d = getelementptr [%d x %s], ptr @%s_%llu, i64 0, i64 %%t%d\n",
                    ptr_reg, nlog, el, table_prefix, (unsigned long long)p, idx64);
            val.reg = ir_tmp();
            ir_emit("  %%t%d = load %s, ptr %%t%d\n", val.reg, el, ptr_reg);
            val.type = ctx_type;
            return val;
        }

        /* Dyn instance with implicit carrier: the callee has a leading
         * Field param (param_types[0] == "Field") that the caller's
         * arg list doesn't include — a nested generic call inside a
         * dyn body, inferred to <dyn>.  Load this function's own
         * __field and prepend it.  (Explicit calls like fn_dyn(f, ...)
         * have matching counts and skip this.) */
        bool prepend_carrier = false;
        int carrier_reg = -1;
        if (fi && !fi->is_generic && fi->nparams == n->call.nargs + 1 &&
            !strcmp(fi->param_types[0], "Field") &&
            sym_find("__field") != NULL) {
            carrier_reg = load_dyn_field_ptr(n->line, n->col);
            if (carrier_reg >= 0) prepend_carrier = true;
        }
        int pshift_call = prepend_carrier ? 1 : 0;

        /* Codegen arguments using parameter types from registry.
         * For compound types (struct, enum), codegen_expr returns a pointer
         * (the alloca address). If the function takes a struct by value,
         * we must insert a load to pass the value, not the pointer. */
        IRValue args[MAX_PARAMS];
        for (int i = 0; i < n->call.nargs; i++) {
            Type arg_ctx = ctx_type;
            if (fi && i + pshift_call < fi->nparams) {
                arg_ctx = resolve_type(fi->param_types[i + pshift_call]);
            }
            args[i] = codegen_expr(n->call.args[i], arg_ctx);

            /* Struct/enum by value: the register holds a ptr to the alloca.
             * Load the aggregate so LLVM sees the correct type at the call. */
            if (args[i].type.kind == TYPE_STRUCT || args[i].type.kind == TYPE_ENUM) {
                int loaded = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n",
                        loaded, type_to_ir(args[i].type), args[i].reg);
                args[i].reg = loaded;
            }
            /* Implicit widening: if argument is narrower than parameter type,
             * insert zext/sext. Covers calling foo(i32) with a u8 value. */
            if (fi && i + pshift_call < fi->nparams) {
                int arg_bits = type_ir_bitwidth(args[i].type);
                int param_bits = type_ir_bitwidth(arg_ctx);
                if (arg_bits > 0 && param_bits > 0 && arg_bits < param_bits) {
                    int widened = ir_tmp();
                    ir_emit("  %%t%d = %s %s %%t%d to %s\n", widened,
                            type_is_signed(args[i].type) ? "sext" : "zext",
                            type_to_ir(args[i].type), args[i].reg,
                            type_to_ir(arg_ctx));
                    args[i].reg = widened;
                    args[i].type = arg_ctx;
                }
            }
        }

        val.type = call_ret;
        const char *ret_ir = type_to_ir(call_ret);

        if (call_ret.kind == TYPE_VOID) {
            ir_emit("  call void @%s(", call_target);
            if (prepend_carrier) {
                ir_emit("ptr %%t%d", carrier_reg);
                if (n->call.nargs > 0) ir_emit(", ");
            }
            for (int i = 0; i < n->call.nargs; i++) {
                if (i > 0) ir_emit(", ");
                ir_emit("%s %%t%d", type_to_ir(args[i].type), args[i].reg);
            }
            ir_emit(")\n");
            val.reg = -1;
        } else {
            val.reg = ir_tmp();
            ir_emit("  %%t%d = call %s @%s(", val.reg, ret_ir, call_target);
            if (prepend_carrier) {
                ir_emit("ptr %%t%d", carrier_reg);
                if (n->call.nargs > 0) ir_emit(", ");
            }
            for (int i = 0; i < n->call.nargs; i++) {
                if (i > 0) ir_emit(", ");
                ir_emit("%s %%t%d", type_to_ir(args[i].type), args[i].reg);
            }
            ir_emit(")\n");
        }
        return val;
    }

    case AST_IF: {
        IRValue cond = codegen_expr(n->if_expr.cond, (Type){.kind = TYPE_BOOL});
        int lbl_then = ir_label();
        int lbl_else = ir_label();
        int lbl_end = ir_label();

        if (n->if_expr.else_b) {
            ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n",
                    cond.reg, lbl_then, lbl_else);
        } else {
            ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n",
                    cond.reg, lbl_then, lbl_end);
        }

        /* Then branch */
        ir_emit("L%d:\n", lbl_then);
        g_block_terminated = false;
        sym_push_scope();
        if (n->if_expr.then_b->kind == AST_BLOCK) {
            for (int i = 0; i < n->if_expr.then_b->block.nstmts; i++) {
                codegen_stmt(n->if_expr.then_b->block.stmts[i], ctx_type);
            }
        }
        sym_pop_scope();
        bool then_returned = g_block_terminated;
        if (!g_block_terminated) {
            ir_emit("  br label %%L%d\n", lbl_end);
        }

        /* Else branch */
        bool else_returned = false;
        if (n->if_expr.else_b) {
            ir_emit("L%d:\n", lbl_else);
            g_block_terminated = false;
            sym_push_scope();
            if (n->if_expr.else_b->kind == AST_BLOCK) {
                for (int i = 0; i < n->if_expr.else_b->block.nstmts; i++) {
                    codegen_stmt(n->if_expr.else_b->block.stmts[i], ctx_type);
                }
            }
            sym_pop_scope();
            else_returned = g_block_terminated;
            if (!g_block_terminated) {
                ir_emit("  br label %%L%d\n", lbl_end);
            }
        }

        /* Only emit the join block if at least one branch falls through */
        if (!then_returned || !else_returned) {
            ir_emit("L%d:\n", lbl_end);
            g_block_terminated = false;
        } else {
            /* Both branches returned — current block is terminated.
             * Emit unreachable join block in case LLVM needs the label. */
            ir_emit("L%d:\n", lbl_end);
            ir_emit("  unreachable\n");
            g_block_terminated = true;
        }

        val.reg = ir_tmp();
        val.type.kind = TYPE_VOID;
        return val;
    }

    case AST_INDEX: {
        /* arr[idx] or ptr[idx] or expr[idx] — indexed read */

        /* Expression-based index: b.data[0] where obj_expr is the member access */
        if (n->index_expr.obj_expr) {
            IRValue base = codegen_expr(n->index_expr.obj_expr, ctx_type);
            IRValue idx = codegen_expr(n->index_expr.index, (Type){.kind = TYPE_I32});
            if (base.type.kind == TYPE_ARRAY) {
                Type elem_type = {.kind = base.type.elem_kind, .field_prime = base.type.field_prime};
                const char *elem_ir = type_to_ir(elem_type);
                int idx64 = ir_tmp();
                ir_emit("  %%t%d = sext i32 %%t%d to i64\n", idx64, idx.reg);
                int gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 0, i64 %%t%d\n",
                        gep, type_to_ir(base.type), base.reg, idx64);
                val.reg = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", val.reg, elem_ir, gep);
                val.type = elem_type;
                return val;
            }
            /* Pointer-typed expression result: treat like ptr[i] */
            if (base.type.kind == TYPE_PTR) {
                Type pointee = type_pointee(base.type);
                const char *pointee_ir = type_to_ir(pointee);
                int idx64 = ir_tmp();
                ir_emit("  %%t%d = sext i32 %%t%d to i64\n", idx64, idx.reg);
                int gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 %%t%d\n",
                        gep, pointee_ir, base.reg, idx64);
                val.reg = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", val.reg, pointee_ir, gep);
                val.type = pointee;
                return val;
            }
            error(n->line, n->col, "cannot index into non-array/non-pointer expression");
            val.reg = ir_tmp();
            ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
            val.type.kind = TYPE_U8;
            return val;
        }

        Symbol *arr_sym = sym_find(n->index_expr.name);
        if (!arr_sym) {
            error(n->line, n->col, "undefined variable '%s'", n->index_expr.name);
            val.reg = ir_tmp();
            ir_emit("  %%t%d = add i8 0, 0\n", val.reg);
            val.type.kind = TYPE_U8;
            return val;
        }

        IRValue idx = codegen_expr(n->index_expr.index, (Type){.kind = TYPE_I32});

        if (arr_sym->type.kind == TYPE_PTR) {
            /* Pointer indexing: ptr[i] → GEP + load */
            Type pointee = type_pointee(arr_sym->type);
            const char *pointee_ir = type_to_ir(pointee);

            /* Load the pointer value */
            int ptr_val;
            if (arr_sym->ir_reg < 0) {
                /* Global variable: load from @name */
                ptr_val = ir_tmp();
                ir_emit("  %%t%d = load ptr, ptr @%s\n", ptr_val, arr_sym->name);
            } else if (arr_sym->is_alloca) {
                ptr_val = ir_tmp();
                ir_emit("  %%t%d = load ptr, ptr %%t%d\n", ptr_val, arr_sym->ir_reg);
            } else {
                ptr_val = arr_sym->ir_reg;
            }

            int idx64 = ir_tmp();
            ir_emit("  %%t%d = sext i32 %%t%d to i64\n", idx64, idx.reg);
            int gep = ir_tmp();
            ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 %%t%d\n",
                    gep, pointee_ir, ptr_val, idx64);

            if (pointee.kind == TYPE_STRUCT) {
                /* Struct pointee: return pointer to element (no load).
                 * Enables chained access: tokens[i].kind via auto-deref.
                 *
                 * DESIGN NOTE: This is reference semantics — tokens[i] returns
                 * *T, not T. Mutations to the returned pointer write through to
                 * the original array. This is intentional: it matches how
                 * low-level languages work and avoids hidden copies. If you
                 * need a copy, allocate a new struct and copy fields. */
                val.reg = gep;
                val.type.kind = TYPE_PTR;
                val.type.ptr_pointee_kind = pointee.kind;
                val.type.ptr_struct_id = pointee.struct_id;
                return val;
            }

            val.reg = ir_tmp();
            ir_emit("  %%t%d = load %s, ptr %%t%d\n", val.reg, pointee_ir, gep);
            val.type = pointee;
            return val;
        }

        /* Array indexing (existing path) */
        Type elem_type = {.kind = arr_sym->type.elem_kind, .field_prime = arr_sym->type.field_prime};
        const char *elem_ir = type_to_ir(elem_type);

        /* getelementptr + load */
        int idx64 = ir_tmp();
        ir_emit("  %%t%d = sext i32 %%t%d to i64\n", idx64, idx.reg);
        int gep = ir_tmp();
        ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 0, i64 %%t%d\n",
                gep, type_to_ir(arr_sym->type), arr_sym->ir_reg, idx64);
        val.reg = ir_tmp();
        ir_emit("  %%t%d = load %s, ptr %%t%d\n", val.reg, elem_ir, gep);
        val.type = elem_type;
        return val;
    }

    case AST_PROJECT: {
        /* project(x) — modular reduction of integer into field element.
         * Always evaluate as i32 (the natural loop counter type),
         * then widen/narrow to the field's wide type, urem, trunc to elem. */
        IRValue inner = codegen_expr(n->project.expr, (Type){.kind = TYPE_I32});
        val.type = ctx_type;
        if (ctx_type.kind == TYPE_FIELD) {
            uint64_t p = ctx_type.field_prime;
            FieldInfo *fi_proj = find_field_by_prime(p);
            const char *el = field_elem_ir(p);
            const char *wi = field_wide_ir(p);
            int bits = fi_proj ? fi_proj->elem_bits : 8;

            /* Cast i32 → wide type */
            int wide_reg = ir_tmp();
            if (bits == 8) {
                /* i32 → i16: trunc */
                ir_emit("  %%t%d = trunc i32 %%t%d to %s\n", wide_reg, inner.reg, wi);
            } else if (bits == 16) {
                /* i32 IS the wide type */
                wide_reg = inner.reg;
            } else if (bits == 32) {
                /* i32 → i64: sext */
                ir_emit("  %%t%d = sext i32 %%t%d to %s\n", wide_reg, inner.reg, wi);
            } else {
                /* i32 → i128: sext */
                ir_emit("  %%t%d = sext i32 %%t%d to %s\n", wide_reg, inner.reg, wi);
            }

            /* urem to reduce into field (in wide type) */
            int rem_reg = ir_tmp();
            ir_emit("  %%t%d = urem %s %%t%d, %llu\n", rem_reg, wi, wide_reg,
                    (unsigned long long)p);

            /* trunc to element type */
            val.reg = ir_tmp();
            if (!strcmp(wi, el)) {
                val.reg = rem_reg;  /* same width, no trunc needed */
            } else {
                ir_emit("  %%t%d = trunc %s %%t%d to %s\n", val.reg, wi, rem_reg, el);
            }
        } else {
            val = inner;
        }
        return val;
    }

    case AST_CAST: {
        /* expr as type — supports field reduction, integer trunc/extend,
         * pointer-to-pointer, integer-to-pointer, pointer-to-integer. */
        Type target = resolve_type(n->cast.type_name);
        IRValue inner = codegen_expr(n->cast.expr, ctx_type);
        val.type = target;
        val.reg = ir_tmp();

        if (target.kind == TYPE_DYNFIELD) {
            /* expr as F in a dyn context: widen to i64, runtime urem
             * against p from the implicit %__field carrier. */
            int fv = load_dyn_field_ptr(n->line, n->col);
            int src_bits = type_ir_bitwidth(inner.type);
            int wide = inner.reg;
            if (src_bits > 0 && src_bits < 64) {
                wide = ir_tmp();
                ir_emit("  %%t%d = %s %s %%t%d to i64\n", wide,
                        type_is_signed(inner.type) ? "sext" : "zext",
                        type_to_ir(inner.type), inner.reg);
            }
            /* Signed sources may be negative: urem on the sext'd value is
             * wrong for negatives, but the language contract for `as F`
             * mirrors the baked path (strict urem on the bit pattern);
             * dyn kernels use non-negative loop counters here. */
            int pp = ir_tmp();
            ir_emit("  %%t%d = getelementptr %%__Field, ptr %%t%d, i32 0, i32 0\n",
                    pp, fv);
            int p = ir_tmp();
            ir_emit("  %%t%d = load i64, ptr %%t%d\n", p, pp);
            ir_emit("  %%t%d = urem i64 %%t%d, %%t%d\n", val.reg, wide, p);
            return val;
        }

        if (target.kind == TYPE_FIELD) {
            const char *el = field_elem_ir(target.field_prime);
            const char *wi = field_wide_ir(target.field_prime);
            const char *src_ir = type_to_ir(inner.type);
            /* Extend/trunc source to wide type, then urem, then trunc to elem */
            int wide_reg;
            int src_bits = type_ir_bitwidth(inner.type);
            FieldInfo *tgt_fi = find_field_by_prime(target.field_prime);
            int wide_bits = tgt_fi ? tgt_fi->elem_bits * 2 : 16;
            if (wide_bits > 64) wide_bits = 64; /* cap at i64 */
            if (!strcmp(src_ir, wi)) {
                wide_reg = inner.reg;  /* same width, no conversion needed */
            } else if (src_bits > wide_bits) {
                /* Source wider than wide type: trunc first, then urem */
                wide_reg = ir_tmp();
                ir_emit("  %%t%d = trunc %s %%t%d to %s\n", wide_reg,
                        src_ir, inner.reg, wi);
            } else {
                wide_reg = ir_tmp();
                ir_emit("  %%t%d = zext %s %%t%d to %s\n", wide_reg,
                        src_ir, inner.reg, wi);
            }
            ir_emit("  %%t%d = urem %s %%t%d, %llu\n", val.reg,
                    wi, wide_reg,
                    (unsigned long long)target.field_prime);
            /* Truncate back to element size if needed */
            if (!strcmp(wi, el)) {
                /* wide == elem, no trunc needed (e.g., Field<251> with i8 elem, i16 wide) */
                /* Actually this won't happen — wide > elem always. But val.reg already set. */
            } else {
                int trunc = ir_tmp();
                ir_emit("  %%t%d = trunc %s %%t%d to %s\n", trunc, wi, val.reg, el);
                val.reg = trunc;
            }
        } else if (inner.type.kind == TYPE_PTR && target.kind == TYPE_PTR) {
            /* *T as *U — no-op in LLVM opaque pointer model (both are ptr).
             * Just propagate the register, change the type metadata. */
            val.reg = inner.reg;
        } else if (inner.type.kind == TYPE_PTR &&
                   (target.kind == TYPE_USIZE || target.kind == TYPE_U64 ||
                    target.kind == TYPE_I64)) {
            /* *T as usize — pointer to integer */
            ir_emit("  %%t%d = ptrtoint ptr %%t%d to %s\n",
                    val.reg, inner.reg, type_to_ir(target));
        } else if (target.kind == TYPE_PTR &&
                   (inner.type.kind == TYPE_USIZE || inner.type.kind == TYPE_U64 ||
                    inner.type.kind == TYPE_I64 || inner.type.kind == TYPE_U32 ||
                    inner.type.kind == TYPE_I32)) {
            /* usize/u64/i64/u32/i32 as *T — integer to pointer */
            ir_emit("  %%t%d = inttoptr %s %%t%d to ptr\n",
                    val.reg, type_to_ir(inner.type), inner.reg);
        } else {
            /* Integer-to-integer: trunc if narrowing, zext/sext if widening */
            int src_bits = type_ir_bitwidth(inner.type);
            int dst_bits = type_ir_bitwidth(target);
            if (src_bits > dst_bits) {
                ir_emit("  %%t%d = trunc %s %%t%d to %s\n", val.reg,
                        type_to_ir(inner.type), inner.reg, type_to_ir(target));
            } else if (src_bits < dst_bits) {
                ir_emit("  %%t%d = %s %s %%t%d to %s\n", val.reg,
                        type_is_signed(inner.type) ? "sext" : "zext",
                        type_to_ir(inner.type), inner.reg, type_to_ir(target));
            } else {
                /* Same width — bitcast / no-op */
                val.reg = inner.reg;
            }
        }
        return val;
    }

    case AST_ENUM_CONSTRUCT: {
        /* Enum::Variant(args) — alloca, store tag, store payload fields */
        EnumInfo *ei = find_enum(n->enum_construct.enum_name);
        if (!ei) {
            error(n->line, n->col, "unknown enum '%s'", n->enum_construct.enum_name);
            val.reg = ir_tmp();
            ir_emit("  %%t%d = add i32 0, 0\n", val.reg);
            return val;
        }
        int eid = (int)(ei - g_enums);
        int vidx = enum_variant_index(ei, n->enum_construct.variant_name);
        if (vidx < 0) {
            error(n->line, n->col, "enum '%s' has no variant '%s'",
                  ei->name, n->enum_construct.variant_name);
            val.reg = ir_tmp();
            ir_emit("  %%t%d = add i32 0, 0\n", val.reg);
            return val;
        }
        EnumVariant *var = &ei->variants[vidx];

        val.type.kind = TYPE_ENUM;
        val.type.enum_id = eid;

        /* Alloca the enum */
        val.reg = ir_tmp();
        ir_emit_alloca("  %%t%d = alloca %s\n", val.reg, ei->ir_name);

        /* Store tag byte at offset 0 */
        ir_emit("  store i8 %d, ptr %%t%d\n", var->tag, val.reg);

        /* Store payload fields at offset 8+ */
        int payload_offset = 8;
        for (int i = 0; i < n->enum_construct.nargs && i < var->nfields; i++) {
            Type ft = var->fields[i];
            IRValue arg_val = codegen_expr(n->enum_construct.args[i], ft);

            int fsize = 0;
            switch (ft.kind) {
                case TYPE_BOOL: case TYPE_U8: case TYPE_I8: fsize = 1; break;
                case TYPE_U16: case TYPE_I16: fsize = 2; break;
                case TYPE_U32: case TYPE_I32: fsize = 4; break;
                case TYPE_U64: case TYPE_I64: case TYPE_USIZE: fsize = 8; break;
                case TYPE_FIELD: {
                    FieldInfo *fi_ec = find_field_by_prime(ft.field_prime);
                    fsize = fi_ec ? fi_ec->elem_bits / 8 : 1;
                    break;
                }
                default: fsize = 8; break;
            }
            int align = fsize > 8 ? 8 : fsize;
            payload_offset = (payload_offset + align - 1) & ~(align - 1);

            int gep = ir_tmp();
            ir_emit("  %%t%d = getelementptr i8, ptr %%t%d, i64 %d\n",
                    gep, val.reg, payload_offset);
            ir_emit("  store %s %%t%d, ptr %%t%d\n",
                    type_to_ir(ft), arg_val.reg, gep);
            payload_offset += fsize;
        }
        return val;
    }

    case AST_STRUCT_LIT: {
        /* Struct literal: Point { x: 1, y: 2 }
         * Alloca the struct, store each field via GEP. */
        StructInfo *si = find_struct(n->struct_lit.type_name);
        if (!si) {
            error(n->line, n->col, "unknown struct type '%s'", n->struct_lit.type_name);
            val.reg = ir_tmp();
            ir_emit("  %%t%d = add i32 0, 0\n", val.reg);
            return val;
        }
        int sid = (int)(si - g_structs);
        val.type.kind = TYPE_STRUCT;
        val.type.struct_id = sid;

        /* Alloca the struct on the stack */
        val.reg = ir_tmp();
        ir_emit_alloca("  %%t%d = alloca %s\n", val.reg, si->ir_name);

        /* Store each field */
        for (int i = 0; i < n->struct_lit.nfields; i++) {
            int fidx = struct_field_index(si, n->struct_lit.field_names[i]);
            if (fidx < 0) {
                error(n->line, n->col, "struct '%s' has no field '%s'",
                      si->name, n->struct_lit.field_names[i]);
                continue;
            }
            Type field_ty = si->fields[fidx].type;

            int gep = ir_tmp();
            ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 %d\n",
                    gep, si->ir_name, val.reg, fidx);

            /* Array-typed field: zero-init with memset (can't use add/store on aggregate) */
            if (field_ty.kind == TYPE_ARRAY) {
                int byte_size = field_ty.array_size;
                if (field_ty.elem_kind == TYPE_FIELD) {
                    FieldInfo *fi_arr = find_field_by_prime(field_ty.field_prime);
                    if (fi_arr) byte_size *= (fi_arr->elem_bits / 8);
                }
                else if (field_ty.elem_kind == TYPE_U16 || field_ty.elem_kind == TYPE_I16) byte_size *= 2;
                else if (field_ty.elem_kind == TYPE_U32 || field_ty.elem_kind == TYPE_I32) byte_size *= 4;
                else if (field_ty.elem_kind == TYPE_U64 || field_ty.elem_kind == TYPE_I64) byte_size *= 8;
                ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                        gep, byte_size);
                continue;
            }

            IRValue field_val = codegen_expr(n->struct_lit.field_values[i], field_ty);
            ir_emit("  store %s %%t%d, ptr %%t%d\n",
                    type_to_ir(field_ty), field_val.reg, gep);
        }
        return val;
    }

    case AST_MEMBER_ACCESS: {
        /* obj.member — struct field read via GEP + load.
         * Supports: direct struct, *T auto-deref, chained expr.member */
        Type obj_type = {0};
        int obj_reg = 0;

        if (n->member_access.obj_expr) {
            /* Chained: evaluate sub-expression (e.g. b.next in b.next.value) */
            IRValue obj_val = codegen_expr(n->member_access.obj_expr, ctx_type);
            obj_type = obj_val.type;
            obj_reg = obj_val.reg;
        } else {
            /* Direct: look up variable name */
            Symbol *obj_sym = sym_find(n->member_access.obj_name);
            if (!obj_sym) {
                error(n->line, n->col, "undefined variable '%s'", n->member_access.obj_name);
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i32 0, 0\n", val.reg);
                return val;
            }
            obj_type = obj_sym->type;
            obj_reg = obj_sym->ir_reg;

            /* For pointer types: load the actual pointer value */
            if (obj_type.kind == TYPE_PTR && obj_sym->ir_reg < 0) {
                /* Global variable */
                int loaded = ir_tmp();
                ir_emit("  %%t%d = load ptr, ptr @%s\n", loaded, obj_sym->name);
                obj_reg = loaded;
            } else if (obj_type.kind == TYPE_PTR && obj_sym->is_alloca) {
                int loaded = ir_tmp();
                ir_emit("  %%t%d = load ptr, ptr %%t%d\n", loaded, obj_reg);
                obj_reg = loaded;
            }
        }

        /* Field carrier member access: f.p, f.half_p (i64),
         * f.elem_bytes, f.data_bytes (i32) — GEP into %__Field + load.
         * The carrier value is a ptr to %__Field; symbols hold it
         * spilled in an alloca. */
        if (obj_type.kind == TYPE_FIELD_VALUE) {
            int carrier = obj_reg;
            /* Direct symbol path: the alloca holds the carrier ptr */
            if (!n->member_access.obj_expr) {
                Symbol *fsym = sym_find(n->member_access.obj_name);
                if (fsym && fsym->is_alloca) {
                    carrier = ir_tmp();
                    ir_emit("  %%t%d = load ptr, ptr %%t%d\n", carrier, obj_reg);
                }
            }
            const char *mname = n->member_access.member;
            int fld_idx = -1;
            bool is64 = false;
            if (!strcmp(mname, "p"))          { fld_idx = 0; is64 = true; }
            else if (!strcmp(mname, "half_p")) { fld_idx = 1; is64 = true; }
            else if (!strcmp(mname, "elem_bytes")) { fld_idx = 2; }
            else if (!strcmp(mname, "data_bytes")) { fld_idx = 3; }
            if (fld_idx < 0) {
                error(n->line, n->col,
                      "Field has no member '%s' (p, half_p, elem_bytes, data_bytes)",
                      mname);
                val.reg = ir_tmp();
                ir_emit("  %%t%d = add i64 0, 0\n", val.reg);
                val.type.kind = TYPE_I64;
                return val;
            }
            int fgep = ir_tmp();
            ir_emit("  %%t%d = getelementptr %%__Field, ptr %%t%d, i32 0, i32 %d\n",
                    fgep, carrier, fld_idx);
            val.reg = ir_tmp();
            ir_emit("  %%t%d = load %s, ptr %%t%d\n", val.reg,
                    is64 ? "i64" : "i32", fgep);
            val.type.kind = is64 ? TYPE_I64 : TYPE_I32;
            return val;
        }

        /* Auto-deref: if obj is *T where T is a struct, dereference to get struct ptr */
        StructInfo *si = NULL;
        int struct_ptr = obj_reg;
        if (obj_type.kind == TYPE_PTR && obj_type.ptr_pointee_kind == TYPE_STRUCT) {
            si = &g_structs[obj_type.ptr_struct_id];
            struct_ptr = obj_reg; /* already a ptr to the struct on the heap */
        } else if (obj_type.kind == TYPE_STRUCT) {
            si = &g_structs[obj_type.struct_id];
            struct_ptr = obj_reg; /* alloca ptr to the struct */
        } else {
            error(n->line, n->col, "member access on non-struct type");
            val.reg = ir_tmp();
            ir_emit("  %%t%d = add i32 0, 0\n", val.reg);
            return val;
        }

        int fidx = struct_field_index(si, n->member_access.member);
        if (fidx < 0) {
            error(n->line, n->col, "struct '%s' has no field '%s'",
                  si->name, n->member_access.member);
            val.reg = ir_tmp();
            ir_emit("  %%t%d = add i32 0, 0\n", val.reg);
            return val;
        }
        Type field_ty = si->fields[fidx].type;
        int gep = ir_tmp();
        ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 %d\n",
                gep, si->ir_name, struct_ptr, fidx);

        /* Array/compound-typed field: return GEP pointer directly (no load).
         * Mirrors the AST_IDENT path for compound types at codegen_expr. */
        if (field_ty.kind == TYPE_ARRAY || field_ty.kind == TYPE_STRUCT ||
            field_ty.kind == TYPE_POLY || field_ty.kind == TYPE_REGISTER) {
            val.reg = gep;
            val.type = field_ty;
            return val;
        }

        val.reg = ir_tmp();
        ir_emit("  %%t%d = load %s, ptr %%t%d\n", val.reg, type_to_ir(field_ty), gep);
        val.type = field_ty;
        return val;
    }

    default:
        error(n->line, n->col, "unsupported expression kind %d", n->kind);
        val.reg = ir_tmp();
        ir_emit("  %%t%d = add i32 0, 0\n", val.reg);
        return val;
    }
}

/* ============================================================
 * Statement codegen
 * ============================================================ */

static void codegen_stmt(ASTNode *n, Type fn_ret_type) {
    switch (n->kind) {
    case AST_LET: {
        Type ty = resolve_type(n->let_stmt.type_name);

        /* Enum type: evaluate init, register alloca.
         * Init is typically an alloca pointer (from enum construction, ident,
         * or index). Function calls return loaded values — store into alloca. */
        if (ty.kind == TYPE_ENUM) {
            IRValue init = codegen_expr(n->let_stmt.init, ty);
            if (n->let_stmt.init->kind == AST_CALL) {
                /* Function returned a value, not a pointer — store into alloca */
                int alloca_reg = ir_tmp();
                ir_emit_alloca("  %%t%d = alloca %s\n", alloca_reg, type_to_ir(ty));
                ir_emit("  store %s %%t%d, ptr %%t%d\n",
                        type_to_ir(ty), init.reg, alloca_reg);
                sym_add(n->let_stmt.name, ty, alloca_reg, true);
            } else {
                sym_add(n->let_stmt.name, ty, init.reg, true);
            }
            break;
        }

        /* Struct type: same logic as enum. */
        if (ty.kind == TYPE_STRUCT) {
            IRValue init = codegen_expr(n->let_stmt.init, ty);
            if (n->let_stmt.init->kind == AST_CALL) {
                /* Function returned a value, not a pointer — store into alloca */
                int alloca_reg = ir_tmp();
                ir_emit_alloca("  %%t%d = alloca %s\n", alloca_reg, type_to_ir(ty));
                ir_emit("  store %s %%t%d, ptr %%t%d\n",
                        type_to_ir(ty), init.reg, alloca_reg);
                sym_add(n->let_stmt.name, ty, alloca_reg, true);
            } else {
                sym_add(n->let_stmt.name, ty, init.reg, true);
            }
            break;
        }

        /* Array type: always stack-allocate and zero-initialize */
        if (ty.kind == TYPE_ARRAY) {
            int alloca_reg = ir_tmp();
            const char *arr_ir = type_to_ir(ty);
            ir_emit_alloca("  %%t%d = alloca %s\n", alloca_reg, arr_ir);
            /* Zero-initialize with memset (LLVM intrinsic) */
            int byte_size = ty.array_size;
            if (ty.elem_kind == TYPE_FIELD) {
                FieldInfo *fi_arr = find_field_by_prime(ty.field_prime);
                if (fi_arr) byte_size *= (fi_arr->elem_bits / 8);
            }
            else if (ty.elem_kind == TYPE_U16 || ty.elem_kind == TYPE_I16) byte_size *= 2;
            else if (ty.elem_kind == TYPE_U32 || ty.elem_kind == TYPE_I32) byte_size *= 4;
            else if (ty.elem_kind == TYPE_U64 || ty.elem_kind == TYPE_I64) byte_size *= 8;
            ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                    alloca_reg, byte_size);
            sym_add(n->let_stmt.name, ty, alloca_reg, true);
            break;
        }

        /* Poly type: alloca + copy from init's alloca, record repr_tag */
        if (ty.kind == TYPE_POLY) {
            g_last_repr = REPR_UNKNOWN;  /* reset before codegen_expr */
            IRValue init_poly = codegen_expr(n->let_stmt.init, ty);
            int n_coeffs = ty.poly_degree + 1;
            const char *el = field_elem_ir(ty.field_prime);

            /* Alloca a new poly slot */
            int alloca_reg = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", alloca_reg, n_coeffs, el);
            FieldInfo *fi_poly = find_field_by_prime(ty.field_prime);
            int poly_bytes = n_coeffs * (fi_poly ? fi_poly->elem_bits / 8 : 1);
            ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                    alloca_reg, poly_bytes);

            /* Copy coefficients from init */
            for (int i = 0; i < n_coeffs; i++) {
                int i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", i64, i);
                int src_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        src_gep, n_coeffs, el, init_poly.reg, i64);
                int dst_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        dst_gep, n_coeffs, el, alloca_reg, i64);
                int coeff = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", coeff, el, src_gep);
                ir_emit("  store %s %%t%d, ptr %%t%d\n", el, coeff, dst_gep);
            }

            Symbol *sym = sym_add(n->let_stmt.name, ty, alloca_reg, true);
            sym->repr_tag = g_last_repr;
            break;
        }

        /* Register type: alloca + copy from init's alloca (always Newton, no repr tracking) */
        if (ty.kind == TYPE_REGISTER) {
            IRValue init_reg = codegen_expr(n->let_stmt.init, ty);
            int n_elems = ty.register_degree + 1;

            /* ---- Dyn register copy ----
             * Layout: { ptr, [d+1 x i64] } — copy carrier ptr + coefficients. */
            if (ty.elem_kind == TYPE_DYNFIELD) {
                char ir_ty[64];
                snprintf(ir_ty, sizeof(ir_ty), "{ ptr, [%d x i64] }", n_elems);
                int struct_size = 8 + n_elems * 8;

                int alloca_reg = ir_tmp();
                ir_emit_alloca("  %%t%d = alloca %s\n", alloca_reg, ir_ty);
                ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                        alloca_reg, struct_size);

                /* Copy carrier ptr */
                int src_carrier_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 0\n",
                        src_carrier_gep, ir_ty, init_reg.reg);
                int carrier = ir_tmp();
                ir_emit("  %%t%d = load ptr, ptr %%t%d\n", carrier, src_carrier_gep);
                int dst_carrier_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 0\n",
                        dst_carrier_gep, ir_ty, alloca_reg);
                ir_emit("  store ptr %%t%d, ptr %%t%d\n", carrier, dst_carrier_gep);

                /* Copy coefficients */
                for (int i = 0; i < n_elems; i++) {
                    int src_gep = ir_tmp();
                    ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 1, i64 %d\n",
                            src_gep, ir_ty, init_reg.reg, i);
                    int dst_gep = ir_tmp();
                    ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 1, i64 %d\n",
                            dst_gep, ir_ty, alloca_reg, i);
                    int coeff = ir_tmp();
                    ir_emit("  %%t%d = load i64, ptr %%t%d\n", coeff, src_gep);
                    ir_emit("  store i64 %%t%d, ptr %%t%d\n", coeff, dst_gep);
                }

                sym_add(n->let_stmt.name, ty, alloca_reg, true);
                break;
            }

            /* ---- Baked register copy (existing) ---- */
            const char *el = field_elem_ir(ty.field_prime);

            /* Alloca a new register slot */
            int alloca_reg = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca [%d x %s]\n", alloca_reg, n_elems, el);
            FieldInfo *fi_reg = find_field_by_prime(ty.field_prime);
            int reg_bytes = n_elems * (fi_reg ? fi_reg->elem_bits / 8 : 1);
            ir_emit("  call void @llvm.memset.p0.i64(ptr %%t%d, i8 0, i64 %d, i1 false)\n",
                    alloca_reg, reg_bytes);

            /* Copy coefficients from init */
            for (int i = 0; i < n_elems; i++) {
                int i64 = ir_tmp();
                ir_emit("  %%t%d = add i64 0, %d\n", i64, i);
                int src_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        src_gep, n_elems, el, init_reg.reg, i64);
                int dst_gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr [%d x %s], ptr %%t%d, i64 0, i64 %%t%d\n",
                        dst_gep, n_elems, el, alloca_reg, i64);
                int coeff = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n", coeff, el, src_gep);
                ir_emit("  store %s %%t%d, ptr %%t%d\n", el, coeff, dst_gep);
            }

            sym_add(n->let_stmt.name, ty, alloca_reg, true);
            break;
        }

        IRValue init = codegen_expr(n->let_stmt.init, ty);

        /* Extension field assignment from integer: construct {value mod p, 0} */
        if (ty.kind == TYPE_FIELD && ty.field_idx >= 0 && ty.field_idx < g_nfields &&
            g_fields[ty.field_idx].field_kind == FIELD_KIND_EXTENSION &&
            init.type.kind != TYPE_FIELD) {
            FieldInfo *ext_fi = &g_fields[ty.field_idx];
            const char *el = ext_fi->elem_ir;
            const char *et = ext_fi->ext_elem_ir;
            /* Reduce the integer value mod p, then truncate to base element width */
            uint64_t lit_val = 0;
            if (n->let_stmt.init->kind == AST_LIT_INT) {
                lit_val = n->let_stmt.init->lit_int.value % ext_fi->prime;
            }
            int tmp0 = ir_tmp();
            ir_emit("  %%t%d = insertvalue %s undef, %s %llu, 0\n",
                    tmp0, et, el, (unsigned long long)lit_val);
            int tmp1 = ir_tmp();
            ir_emit("  %%t%d = insertvalue %s %%t%d, %s 0, 1\n",
                    tmp1, et, tmp0, el);
            init.reg = tmp1;
            init.type = ty;
        }

        /* Integer -> dyn field assignment: strict modulation with runtime p.
         * (Literal inits already reduce inside AST_LIT_INT's dyn path;
         * this covers idents/exprs of integer type.) */
        if (ty.kind == TYPE_DYNFIELD && init.type.kind != TYPE_DYNFIELD) {
            int fv = load_dyn_field_ptr(n->line, n->col);
            int src_bits = type_ir_bitwidth(init.type);
            int wide = init.reg;
            if (src_bits > 0 && src_bits < 64) {
                wide = ir_tmp();
                ir_emit("  %%t%d = %s %s %%t%d to i64\n", wide,
                        type_is_signed(init.type) ? "sext" : "zext",
                        type_to_ir(init.type), init.reg);
            }
            int pp = ir_tmp();
            ir_emit("  %%t%d = getelementptr %%__Field, ptr %%t%d, i32 0, i32 0\n",
                    pp, fv);
            int p = ir_tmp();
            ir_emit("  %%t%d = load i64, ptr %%t%d\n", p, pp);
            int rem = ir_tmp();
            ir_emit("  %%t%d = urem i64 %%t%d, %%t%d\n", rem, wide, p);
            init.reg = rem;
            init.type = ty;
        }

        /* Integer/cross-field -> Field assignment: strict modulation (urem).
         * Triggers for: integer types, or field types with different prime.
         * Safe by default: auto-reduce into target field. */
        if (ty.kind == TYPE_FIELD &&
            (init.type.kind != TYPE_FIELD || init.type.field_prime != ty.field_prime)) {
            FieldInfo *fi = find_field_by_prime(ty.field_prime);
            const char *el = field_elem_ir(ty.field_prime);

            /* Determine intermediate type: must be wide enough for the source value.
             * Use the wider of source elem bits and target wide bits. */
            const char *src_ir = type_to_ir(init.type);
            const char *inter_ir = fi ? fi->wide_ir : "i16";
            int src_bits = 32; /* default for integer types */
            if (init.type.kind == TYPE_FIELD) {
                FieldInfo *src_fi = find_field_by_prime(init.type.field_prime);
                src_bits = src_fi ? src_fi->elem_bits : 8;
            } else if (init.type.kind == TYPE_U8 || init.type.kind == TYPE_I8) {
                src_bits = 8;
            } else if (init.type.kind == TYPE_U16 || init.type.kind == TYPE_I16) {
                src_bits = 16;
            } else if (init.type.kind == TYPE_U64 || init.type.kind == TYPE_I64) {
                src_bits = 64;
            }
            int inter_bits = fi ? fi->elem_bits * 2 : 16; /* wide type bits */
            if (inter_bits < 16) inter_bits = 16;
            if (src_bits > inter_bits) {
                /* Source wider than target wide — use source width for urem */
                inter_bits = src_bits;
                inter_ir = src_ir;
            }

            /* Extend or truncate source to intermediate type */
            int ext = ir_tmp();
            if (src_bits < inter_bits) {
                ir_emit("  %%t%d = zext %s %%t%d to %s\n", ext,
                        src_ir, init.reg, inter_ir);
            } else if (src_bits > inter_bits) {
                ir_emit("  %%t%d = trunc %s %%t%d to %s\n", ext,
                        src_ir, init.reg, inter_ir);
            } else {
                ext = init.reg; /* same width, no conversion needed */
            }

            int rem = ir_tmp();
            if (fi && fi->field_kind == FIELD_KIND_BINARY) {
                /* Binary field: values 0-255 are all valid GF(2^8) elements */
                rem = ext;
            } else {
                ir_emit("  %%t%d = urem %s %%t%d, %llu\n", rem,
                        inter_ir, ext,
                        (unsigned long long)ty.field_prime);
            }
            int trunc = ir_tmp();
            if (inter_bits > fi->elem_bits) {
                ir_emit("  %%t%d = trunc %s %%t%d to %s\n", trunc,
                        inter_ir, rem, el);
            } else {
                trunc = rem;
            }
            init.reg = trunc;
            init.type = ty;
        }

        /* Implicit integer conversion: if declared type differs in width
         * from init type, insert zext/sext (widening) or trunc (narrowing).
         * Covers: let c: i32 = buf[i]; and let s: i32 = signed(dyn_x). */
        if (ty.kind != TYPE_VOID && ty.kind != init.type.kind &&
            ty.kind != TYPE_FIELD && init.type.kind != TYPE_FIELD &&
            ty.kind != TYPE_DYNFIELD && init.type.kind != TYPE_DYNFIELD &&
            ty.kind != TYPE_PTR && init.type.kind != TYPE_PTR &&
            ty.kind != TYPE_STRUCT && init.type.kind != TYPE_STRUCT &&
            ty.kind != TYPE_ENUM && init.type.kind != TYPE_ENUM) {
            const char *src_ir = type_to_ir(init.type);
            const char *dst_ir = type_to_ir(ty);
            if (strcmp(src_ir, dst_ir) != 0) {
                int widened = ir_tmp();
                int src_bits = type_ir_bitwidth(init.type);
                int dst_bits = type_ir_bitwidth(ty);
                /* Use sext for signed source, zext for unsigned */
                bool src_signed = (init.type.kind == TYPE_I8 || init.type.kind == TYPE_I16 ||
                                   init.type.kind == TYPE_I32 || init.type.kind == TYPE_I64);
                if (src_bits > dst_bits && dst_bits > 0) {
                    ir_emit("  %%t%d = trunc %s %%t%d to %s\n", widened,
                            src_ir, init.reg, dst_ir);
                } else {
                    ir_emit("  %%t%d = %s %s %%t%d to %s\n", widened,
                            src_signed ? "sext" : "zext", src_ir, init.reg, dst_ir);
                }
                init.reg = widened;
                init.type = ty;
            }
        }

        if (n->let_stmt.is_mut) {
            /* Mutable: use alloca + store/load */
            int alloca_reg = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca %s\n", alloca_reg, type_to_ir(init.type));
            ir_emit("  store %s %%t%d, ptr %%t%d\n",
                    type_to_ir(init.type), init.reg, alloca_reg);
            sym_add(n->let_stmt.name, init.type, alloca_reg, true);
        } else {
            /* Immutable: just alias the SSA register */
            sym_add(n->let_stmt.name, init.type, init.reg, false);
        }
        break;
    }

    case AST_ASSIGN: {
        Symbol *s = sym_find(n->assign.name);
        if (!s) {
            error(n->line, n->col, "undefined variable '%s'", n->assign.name);
            break;
        }
        /* Check mutability: is_alloca for locals, is_alloca (== is_mut) for globals */
        if (!s->is_alloca) {
            error(n->line, n->col, "cannot assign to immutable variable '%s'", n->assign.name);
            break;
        }
        IRValue val = codegen_expr(n->assign.value, s->type);
        /* Implicit conversion for assignment: widen (zext/sext) or
         * narrow (trunc) when value width differs from target width. */
        if (val.type.kind != s->type.kind &&
            val.type.kind != TYPE_PTR && s->type.kind != TYPE_PTR &&
            val.type.kind != TYPE_STRUCT && s->type.kind != TYPE_STRUCT) {
            const char *src_ir = type_to_ir(val.type);
            const char *dst_ir = type_to_ir(s->type);
            if (strcmp(src_ir, dst_ir) != 0) {
                int widened = ir_tmp();
                int src_bits = type_ir_bitwidth(val.type);
                int dst_bits = type_ir_bitwidth(s->type);
                bool src_signed = (val.type.kind == TYPE_I8 || val.type.kind == TYPE_I16 ||
                                   val.type.kind == TYPE_I32 || val.type.kind == TYPE_I64);
                if (src_bits > dst_bits && dst_bits > 0) {
                    ir_emit("  %%t%d = trunc %s %%t%d to %s\n", widened,
                            src_ir, val.reg, dst_ir);
                } else {
                    ir_emit("  %%t%d = %s %s %%t%d to %s\n", widened,
                            src_signed ? "sext" : "zext", src_ir, val.reg, dst_ir);
                }
                val.reg = widened;
                val.type = s->type;
            }
        }
        if (s->ir_reg < 0) {
            /* Global variable: store to @name */
            ir_emit("  store %s %%t%d, ptr @%s\n",
                    type_to_ir(s->type), val.reg, s->name);
        } else {
            ir_emit("  store %s %%t%d, ptr %%t%d\n",
                    type_to_ir(s->type), val.reg, s->ir_reg);
        }
        break;
    }

    case AST_MATCH: {
        /* match scrutinee { pattern => { body }, ... }
         * Supports enum and integer scrutinees.
         * Lowers to LLVM switch instruction. */

        /* Evaluate scrutinee */
        IRValue scrut = codegen_expr(n->match_expr.scrutinee, fn_ret_type);
        bool is_int_match = (scrut.type.kind != TYPE_ENUM);
        if (is_int_match && scrut.type.kind != TYPE_I8 && scrut.type.kind != TYPE_I16 &&
            scrut.type.kind != TYPE_I32 && scrut.type.kind != TYPE_I64 &&
            scrut.type.kind != TYPE_U8 && scrut.type.kind != TYPE_U16 &&
            scrut.type.kind != TYPE_U32 && scrut.type.kind != TYPE_U64 &&
            scrut.type.kind != TYPE_USIZE) {
            error(n->line, n->col, "match scrutinee must be an enum or integer type");
            break;
        }
        EnumInfo *ei = is_int_match ? NULL : &g_enums[scrut.type.enum_id];
        int narms = n->match_expr.narms;
        int arms_start = n->match_expr.arms_start;

        /* Exhaustiveness check (enum only; integers require _ wildcard) */
        bool has_wildcard = false;
        for (int a = 0; a < narms; a++) {
            if (g_match_arms[arms_start + a].is_wildcard) { has_wildcard = true; break; }
        }

        if (!is_int_match) {
            bool *covered = calloc(ei->nvariants, sizeof(bool));
            for (int a = 0; a < narms; a++) {
                MatchArmInfo *arm = &g_match_arms[arms_start + a];
                if (arm->is_wildcard) continue;
                if (!arm->is_int_lit) {
                    int vidx = enum_variant_index(ei, arm->variant_name);
                    if (vidx >= 0) covered[vidx] = true;
                    else error(n->line, n->col, "enum '%s' has no variant '%s'",
                               ei->name, arm->variant_name);
                }
            }
            if (!has_wildcard) {
                for (int v = 0; v < ei->nvariants; v++) {
                    if (!covered[v]) {
                        error(n->line, n->col,
                              "match is not exhaustive: missing variant %s::%s",
                              ei->name, ei->variants[v].name);
                    }
                }
            }
            free(covered);
        } else {
            /* Integer match: require _ wildcard (can't enumerate integers) */
            if (!has_wildcard) {
                error(n->line, n->col,
                      "integer match requires a wildcard '_' arm");
            }
            /* Check for duplicate integer arm values (LLVM switch requires unique cases) */
            for (int a = 0; a < narms; a++) {
                MatchArmInfo *arm_a = &g_match_arms[arms_start + a];
                if (!arm_a->is_int_lit) continue;
                for (int b = a + 1; b < narms; b++) {
                    MatchArmInfo *arm_b = &g_match_arms[arms_start + b];
                    if (!arm_b->is_int_lit) continue;
                    if (arm_a->int_val == arm_b->int_val) {
                        error(n->line, n->col,
                              "duplicate match arm value %lld",
                              (long long)arm_a->int_val);
                    }
                }
            }
        }

        /* Get the switch operand.
         * Enum: load the tag byte (i8) from the alloca.
         * Integer: use the scrutinee value directly. */
        int switch_reg;
        const char *switch_ty;
        if (is_int_match) {
            switch_reg = scrut.reg;
            switch_ty = type_to_ir(scrut.type);
        } else {
            switch_reg = ir_tmp();
            ir_emit("  %%t%d = load i8, ptr %%t%d\n", switch_reg, scrut.reg);
            switch_ty = "i8";
        }

        /* Labels */
        int lbl_default = ir_label();
        int lbl_end = ir_label();
        int *arm_labels = malloc(sizeof(int) * narms);
        for (int a = 0; a < narms; a++) arm_labels[a] = ir_label();

        /* Build switch instruction */
        /* Find the wildcard arm label (if any) for the default branch */
        int wildcard_label = lbl_default;
        for (int a = 0; a < narms; a++) {
            if (g_match_arms[arms_start + a].is_wildcard) {
                wildcard_label = arm_labels[a];
                break;
            }
        }

        ir_emit("  switch %s %%t%d, label %%L%d [\n", switch_ty, switch_reg, wildcard_label);
        for (int a = 0; a < narms; a++) {
            MatchArmInfo *arm = &g_match_arms[arms_start + a];
            if (arm->is_wildcard) continue;
            if (arm->is_int_lit) {
                ir_emit("    %s %lld, label %%L%d\n",
                        switch_ty, (long long)arm->int_val, arm_labels[a]);
            } else if (!is_int_match) {
                int vidx = enum_variant_index(ei, arm->variant_name);
                if (vidx >= 0) {
                    ir_emit("    %s %d, label %%L%d\n",
                            switch_ty, ei->variants[vidx].tag, arm_labels[a]);
                }
            }
        }
        ir_emit("  ]\n");

        /* Default label: abort (unreachable if exhaustive) */
        ir_emit("L%d:\n", lbl_default);
        ir_emit("  call void @abort()\n");
        ir_emit("  unreachable\n");

        /* Per-arm codegen */
        for (int a = 0; a < narms; a++) {
            MatchArmInfo *arm = &g_match_arms[arms_start + a];
            ir_emit("L%d:\n", arm_labels[a]);
            g_block_terminated = false;
            sym_push_scope();

            /* Destructure: bind payload fields to local names (enum only) */
            if (!is_int_match && !arm->is_wildcard && !arm->is_int_lit && arm->nbindings > 0) {
                int vidx = enum_variant_index(ei, arm->variant_name);
                if (vidx >= 0) {
                    EnumVariant *var = &ei->variants[vidx];
                    int payload_offset = 8;
                    for (int b = 0; b < arm->nbindings && b < var->nfields; b++) {
                        Type ft = var->fields[b];
                        int fsize = 0;
                        switch (ft.kind) {
                            case TYPE_BOOL: case TYPE_U8: case TYPE_I8: fsize = 1; break;
                            case TYPE_U16: case TYPE_I16: fsize = 2; break;
                            case TYPE_U32: case TYPE_I32: fsize = 4; break;
                            case TYPE_U64: case TYPE_I64: case TYPE_USIZE: fsize = 8; break;
                            case TYPE_FIELD: {
                                FieldInfo *fi_m = find_field_by_prime(ft.field_prime);
                                fsize = fi_m ? fi_m->elem_bits / 8 : 1;
                                break;
                            }
                            default: fsize = 8; break;
                        }
                        int align = fsize > 8 ? 8 : fsize;
                        payload_offset = (payload_offset + align - 1) & ~(align - 1);

                        /* GEP to payload field */
                        int gep = ir_tmp();
                        ir_emit("  %%t%d = getelementptr i8, ptr %%t%d, i64 %d\n",
                                gep, scrut.reg, payload_offset);
                        /* Load the value */
                        int val_reg = ir_tmp();
                        ir_emit("  %%t%d = load %s, ptr %%t%d\n",
                                val_reg, type_to_ir(ft), gep);
                        /* Bind as immutable local */
                        sym_add(arm->bindings[b], ft, val_reg, false);

                        payload_offset += fsize;
                    }
                }
            }

            /* Codegen arm body */
            if (arm->body->kind == AST_BLOCK) {
                for (int s = 0; s < arm->body->block.nstmts; s++) {
                    codegen_stmt(arm->body->block.stmts[s], fn_ret_type);
                }
            }
            sym_pop_scope();

            if (!g_block_terminated) {
                ir_emit("  br label %%L%d\n", lbl_end);
            }
        }

        ir_emit("L%d:\n", lbl_end);
        g_block_terminated = false;
        free(arm_labels);
        break;
    }

    case AST_MEMBER_ASSIGN: {
        /* obj.member = expr — struct field write via GEP + store.
         * Supports: direct struct, *T auto-deref, chained expr.member
         *
         * Evaluation order matters: if `expr` reallocates the array backing
         * `obj` (e.g. a growable arena like g_nodes — A6), a destination
         * pointer computed before `expr` would dangle. So we evaluate the
         * object first ONLY to learn its struct type, then evaluate the RHS,
         * then RE-evaluate the object to get a fresh store pointer. */
        Type obj_type = {0};
        Symbol *obj_sym = NULL;

        if (n->member_assign.obj_expr) {
            /* Chained: evaluate sub-expression for its type (pointer discarded;
             * recomputed below after the RHS). */
            Type dummy = {.kind = TYPE_VOID};
            IRValue obj_val0 = codegen_expr(n->member_assign.obj_expr, dummy);
            obj_type = obj_val0.type;
        } else {
            obj_sym = sym_find(n->member_assign.obj_name);
            if (!obj_sym) {
                error(n->line, n->col, "undefined variable '%s'", n->member_assign.obj_name);
                break;
            }
            obj_type = obj_sym->type;
            if (obj_type.kind == TYPE_STRUCT && !obj_sym->is_alloca) {
                error(n->line, n->col, "cannot assign to field of immutable struct '%s'",
                      n->member_assign.obj_name);
                break;
            }
        }

        /* Resolve struct: auto-deref if pointer, direct if struct */
        StructInfo *si = NULL;
        if (obj_type.kind == TYPE_PTR && obj_type.ptr_pointee_kind == TYPE_STRUCT) {
            si = &g_structs[obj_type.ptr_struct_id];
        } else if (obj_type.kind == TYPE_STRUCT) {
            si = &g_structs[obj_type.struct_id];
        } else {
            error(n->line, n->col, "member assign on non-struct type");
            break;
        }

        int fidx = struct_field_index(si, n->member_assign.member);
        if (fidx < 0) {
            error(n->line, n->col, "struct '%s' has no field '%s'",
                  si->name, n->member_assign.member);
            break;
        }
        Type field_ty = si->fields[fidx].type;

        /* RHS first (may grow arenas / move buffers). */
        IRValue val = codegen_expr(n->member_assign.value, field_ty);

        /* Now compute the destination object pointer, fresh. */
        int struct_ptr = 0;
        if (n->member_assign.obj_expr) {
            Type dummy = {.kind = TYPE_VOID};
            IRValue obj_val = codegen_expr(n->member_assign.obj_expr, dummy);
            struct_ptr = obj_val.reg;
        } else {
            struct_ptr = obj_sym->ir_reg;
            if (obj_type.kind == TYPE_PTR && obj_sym->ir_reg < 0) {
                int loaded = ir_tmp();
                ir_emit("  %%t%d = load ptr, ptr @%s\n", loaded, obj_sym->name);
                struct_ptr = loaded;
            } else if (obj_type.kind == TYPE_PTR && obj_sym->is_alloca) {
                int loaded = ir_tmp();
                ir_emit("  %%t%d = load ptr, ptr %%t%d\n", loaded, struct_ptr);
                struct_ptr = loaded;
            }
        }

        int gep = ir_tmp();
        ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i32 0, i32 %d\n",
                gep, si->ir_name, struct_ptr, fidx);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", type_to_ir(field_ty), val.reg, gep);
        break;
    }

    case AST_RETURN: {
        if (n->ret.value) {
            IRValue rv = codegen_expr(n->ret.value, fn_ret_type);
            /* Compound types (struct, enum): codegen returns the alloca ptr.
             * Load the aggregate before ret so LLVM sees the correct type. */
            if (rv.type.kind == TYPE_STRUCT || rv.type.kind == TYPE_ENUM) {
                int loaded = ir_tmp();
                ir_emit("  %%t%d = load %s, ptr %%t%d\n",
                        loaded, type_to_ir(rv.type), rv.reg);
                rv.reg = loaded;
            }
            ir_emit("  ret %s %%t%d\n", type_to_ir(rv.type), rv.reg);
        } else {
            ir_emit("  ret void\n");
        }
        g_block_terminated = true;
        break;
    }

    case AST_EXPR_STMT: {
        codegen_expr(n->expr_stmt.expr, fn_ret_type);
        break;
    }

    case AST_WHILE: {
        /* while cond { body } — native loop, no fuel for native target */
        int lbl_cond = ir_label();
        int lbl_body = ir_label();
        int lbl_end  = ir_label();

        /* Push loop labels for break/continue */
        g_loop_end_labels[g_loop_depth] = lbl_end;
        g_loop_cond_labels[g_loop_depth] = lbl_cond;
        g_loop_depth++;

        /* Condition */
        ir_emit("  br label %%L%d\n", lbl_cond);
        ir_emit("L%d:\n", lbl_cond);
        IRValue cond = codegen_expr(n->while_stmt.cond, (Type){.kind = TYPE_BOOL});
        ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n", cond.reg, lbl_body, lbl_end);

        /* Body */
        ir_emit("L%d:\n", lbl_body);
        g_block_terminated = false;
        sym_push_scope();
        if (n->while_stmt.body->kind == AST_BLOCK) {
            for (int i = 0; i < n->while_stmt.body->block.nstmts; i++) {
                codegen_stmt(n->while_stmt.body->block.stmts[i], fn_ret_type);
            }
        }
        sym_pop_scope();

        /* Loop back to condition */
        if (!g_block_terminated) {
            ir_emit("  br label %%L%d\n", lbl_cond);
        }

        ir_emit("L%d:\n", lbl_end);
        g_block_terminated = false;
        g_loop_depth--;
        break;
    }

    case AST_FOR: {
        /* for i in start..end { body } */
        Type iter_ty = (Type){.kind = TYPE_I32};

        /* EC-1: Pre-scan for field accumulation patterns.
         * If the loop body contains `sum = sum + expr` where sum is a field
         * element, warn when loop_count * (p-1) exceeds p (wraparound). */
        {
            ASTNode *start_node = n->for_stmt.start;
            ASTNode *end_node = n->for_stmt.end;
            /* Only analyze when both bounds are compile-time integer literals */
            if (start_node->kind == AST_LIT_INT && end_node->kind == AST_LIT_INT) {
                uint64_t loop_count = 0;
                if (end_node->lit_int.value > start_node->lit_int.value)
                    loop_count = end_node->lit_int.value - start_node->lit_int.value;

                /* Walk the body looking for field accumulation assignments:
                 *   name = name + expr    (AST_ASSIGN where value is AST_BINARY(OP_ADD)
                 *                          with one side being AST_IDENT matching name) */
                if (loop_count > 0 && n->for_stmt.body->kind == AST_BLOCK) {
                    for (int si = 0; si < n->for_stmt.body->block.nstmts; si++) {
                        ASTNode *stmt = n->for_stmt.body->block.stmts[si];
                        if (stmt->kind != AST_ASSIGN) continue;
                        ASTNode *rhs = stmt->assign.value;
                        if (rhs->kind != AST_BINARY || rhs->binary.op != OP_ADD) continue;

                        /* Check if either side of the addition matches the assign target */
                        const char *target = stmt->assign.name;
                        bool is_accum = false;
                        if (rhs->binary.lhs->kind == AST_IDENT &&
                            !strcmp(rhs->binary.lhs->ident.name, target)) is_accum = true;
                        if (rhs->binary.rhs->kind == AST_IDENT &&
                            !strcmp(rhs->binary.rhs->ident.name, target)) is_accum = true;
                        if (!is_accum) continue;

                        /* Found accumulation. Check if the target is a field type. */
                        Symbol *acc_sym = sym_find(target);
                        if (!acc_sym || acc_sym->type.kind != TYPE_FIELD) continue;

                        uint64_t p = acc_sym->type.field_prime;
                        /* Conservative bound: each iteration adds at most (p-1) */
                        /* Use __uint128_t to avoid overflow in the multiplication */
                        __uint128_t max_accum = (__uint128_t)loop_count * (p - 1);
                        if (max_accum >= p) {
                            uint64_t wraps = (uint64_t)(max_accum / p);
                            warn(n->line, n->col,
                                 "field accumulation in loop may wrap: %llu iterations * "
                                 "(p-1) = %llu*%llu, ~%llu wraparounds mod %llu. "
                                 "Result may lose information",
                                 (unsigned long long)loop_count,
                                 (unsigned long long)loop_count,
                                 (unsigned long long)(p - 1),
                                 (unsigned long long)wraps,
                                 (unsigned long long)p);
                        }
                    }
                }
            }
        }

        /* Parallel dispatch analysis: walk the body once to determine
         * if all iterations are algebraically independent. */
        PForAnalysis pfor = pfor_analyze_body(n->for_stmt.body, n->for_stmt.name,
                                              n->for_stmt.start, n->for_stmt.end);

        /* Dyn context: the loop body's field ops reference the implicit
         * %__field carrier.  Add it as a capture so the worker can load
         * it from the context struct and the dyn binary-op codegen finds
         * it via sym_find("__field") inside the worker scope.  The
         * carrier is read-only post-construction; no aliasing hazard
         * (TYPE_FIELD_VALUE, skipped by the written-array guard). */
        if (pfor.is_independent && pfor.has_field_element && pfor.ncaps > 0) {
            Symbol *dynf = sym_find("__field");
            if (dynf && pfor.ncaps < MAX_PFOR_CAP) {
                PForCapture *cap = &pfor.caps[pfor.ncaps++];
                strcpy(cap->name, "__field");
                cap->type = dynf->type;
                cap->alloca_reg = dynf->ir_reg;
                cap->is_alloca = dynf->is_alloca;
            }
        }

        IRValue start = codegen_expr(n->for_stmt.start, iter_ty);
        IRValue end = codegen_expr(n->for_stmt.end, iter_ty);

        /* #54 guard: the SEED keeps the i32-only iteration space. 64-bit-typed
         * or wide-literal bounds are the self-hosted compiler's width-adoption
         * feature (tvc_self adopts i64); refuse loudly here rather than emit
         * invalid IR (i64 reg in an i32 slot) or a silently misread bound
         * (a [2^31,2^32) literal is a negative i32 bit pattern under slt). */
        {
            bool wide64 =
                start.type.kind == TYPE_I64 || start.type.kind == TYPE_U64 ||
                start.type.kind == TYPE_USIZE ||
                end.type.kind == TYPE_I64 || end.type.kind == TYPE_U64 ||
                end.type.kind == TYPE_USIZE;
            if (n->for_stmt.start->kind == AST_LIT_INT &&
                n->for_stmt.start->lit_int.value >= (1ULL << 31)) wide64 = true;
            if (n->for_stmt.end->kind == AST_LIT_INT &&
                n->for_stmt.end->lit_int.value >= (1ULL << 31)) wide64 = true;
            if (wide64) {
                error(n->line, n->col,
                      "for-loop bounds wider than i32 require the self-hosted "
                      "compiler (tvc_self adopts the i64 iteration space)");
                exit(1);
            }
        }

        /* If the loop is parallelizable, emit dispatch instead of sequential loop */
        if (pfor.is_independent && pfor.has_field_element && pfor.ncaps > 0 &&
            g_npfor_workers < MAX_PFOR) {
            /* Record deferred worker for later emission */
            PForWorker *pw = &g_pfor_workers[g_npfor_workers++];
            pw->id = g_pfor_id++;
            pw->body = n->for_stmt.body;
            strcpy(pw->loop_var, n->for_stmt.name);
            pw->iter_ty = iter_ty;
            pw->fn_ret = fn_ret_type;
            pw->ncaps = pfor.ncaps;
            for (int c = 0; c < pfor.ncaps; c++) {
                pw->caps[c] = pfor.caps[c];
            }

            /* Emit context struct fill on the stack.
             * Layout: cap[0] at offset 0, cap[1] at offset 8, ... */
            int ctx_size = pfor.ncaps * 8;
            int ctx_alloca = ir_tmp();
            ir_emit_alloca("  %%t%d = alloca [%d x i8]\n", ctx_alloca, ctx_size);
            int cap_loaded[MAX_PFOR_CAP];
            for (int c = 0; c < pfor.ncaps; c++) {
                PForCapture *cap = &pfor.caps[c];
                const char *cap_ir = type_to_ir(cap->type);
                int gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr i8, ptr %%t%d, i64 %d\n",
                        gep, ctx_alloca, c * 8);
                /* Get the captured value from the calling function's scope */
                int loaded;
                if (cap->alloca_reg < 0) {
                    /* Global variable: load from @name */
                    loaded = ir_tmp();
                    ir_emit("  %%t%d = load %s, ptr @%s\n", loaded, cap_ir, cap->name);
                } else if (cap->is_alloca) {
                    /* Alloca: load the value from the alloca */
                    loaded = ir_tmp();
                    ir_emit("  %%t%d = load %s, ptr %%t%d\n", loaded, cap_ir, cap->alloca_reg);
                } else {
                    /* Direct SSA register: use the value directly (no load) */
                    loaded = cap->alloca_reg;
                }
                cap_loaded[c] = loaded;
                ir_emit("  store %s %%t%d, ptr %%t%d\n", cap_ir, loaded, gep);
            }

            /* Runtime aliasing guard.  The disjointness proofs assume
             * distinct array NAMES are distinct ARRAYS — false when the
             * caller passes the same buffer twice (f(buf, buf)).  Emit
             * base-pointer equality checks between every WRITTEN array
             * and every other captured pointer; any hit branches to a
             * direct (serial) worker call instead of parallel dispatch.
             * Only base equality is checked: PARTIAL overlap
             * (f(buf, &buf[100])) remains a documented model
             * assumption (needs allocation extents at runtime). */
            int alias_acc = -1;  /* SSA reg of OR-accumulated alias bit */
            for (int wi = 0; wi < pfor.nwrites; wi++) {
                /* Find the capture index of this written array */
                int wcap = -1;
                for (int c = 0; c < pfor.ncaps; c++) {
                    if (!strcmp(pfor.caps[c].name, pfor.writes[wi].arr) &&
                        pfor.caps[c].type.kind == TYPE_PTR) { wcap = c; break; }
                }
                if (wcap < 0) continue;
                /* Skip if an earlier write already covered this capture */
                bool dup = false;
                for (int wj = 0; wj < wi; wj++) {
                    if (!strcmp(pfor.writes[wj].arr, pfor.writes[wi].arr)) { dup = true; break; }
                }
                if (dup) continue;
                for (int c = 0; c < pfor.ncaps; c++) {
                    if (c == wcap) continue;
                    if (pfor.caps[c].type.kind != TYPE_PTR) continue;
                    /* If both are written arrays, check the pair once */
                    bool c_written = false;
                    for (int wj = 0; wj < pfor.nwrites; wj++) {
                        if (!strcmp(pfor.writes[wj].arr, pfor.caps[c].name)) { c_written = true; break; }
                    }
                    if (c_written && c < wcap) continue;
                    int eq = ir_tmp();
                    ir_emit("  %%t%d = icmp eq ptr %%t%d, %%t%d\n",
                            eq, cap_loaded[wcap], cap_loaded[c]);
                    if (alias_acc < 0) {
                        alias_acc = eq;
                    } else {
                        int acc2 = ir_tmp();
                        ir_emit("  %%t%d = or i1 %%t%d, %%t%d\n", acc2, alias_acc, eq);
                        alias_acc = acc2;
                    }
                }
            }

            if (alias_acc >= 0) {
                int lbl_ser = ir_label();
                int lbl_par = ir_label();
                int lbl_done = ir_label();
                ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n",
                        alias_acc, lbl_ser, lbl_par);
                ir_emit("L%d:\n", lbl_ser);
                ir_emit("  call void @__pfor_worker_%d(ptr %%t%d, i32 %%t%d, i32 %%t%d)\n",
                        pw->id, ctx_alloca, start.reg, end.reg);
                ir_emit("  br label %%L%d\n", lbl_done);
                ir_emit("L%d:\n", lbl_par);
                ir_emit("  call void @__parallel_for(ptr @__pfor_worker_%d, ptr %%t%d, i32 %%t%d, i32 %%t%d)\n",
                        pw->id, ctx_alloca, start.reg, end.reg);
                ir_emit("  br label %%L%d\n", lbl_done);
                ir_emit("L%d:\n", lbl_done);
            } else {
                /* No checkable pairs: dispatch directly */
                ir_emit("  call void @__parallel_for(ptr @__pfor_worker_%d, ptr %%t%d, i32 %%t%d, i32 %%t%d)\n",
                        pw->id, ctx_alloca, start.reg, end.reg);
            }
            break;
        }

        /* Sequential loop (unchanged) */
        int lbl_cond = ir_label();
        int lbl_body = ir_label();
        int lbl_incr = ir_label();
        int lbl_end  = ir_label();

        /* Push loop labels for break/continue */
        g_loop_end_labels[g_loop_depth] = lbl_end;
        g_loop_cond_labels[g_loop_depth] = lbl_incr;
        g_loop_depth++;

        /* Alloca for loop var */
        int alloca_i = ir_tmp();
        ir_emit_alloca("  %%t%d = alloca i32\n", alloca_i);
        ir_emit("  store i32 %%t%d, ptr %%t%d\n", start.reg, alloca_i);
        ir_emit("  br label %%L%d\n", lbl_cond);

        /* Condition */
        ir_emit("L%d:\n", lbl_cond);
        int load_i = ir_tmp();
        ir_emit("  %%t%d = load i32, ptr %%t%d\n", load_i, alloca_i);
        int cmp = ir_tmp();
        ir_emit("  %%t%d = icmp slt i32 %%t%d, %%t%d\n", cmp, load_i, end.reg);
        ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n", cmp, lbl_body, lbl_end);

        /* Body */
        ir_emit("L%d:\n", lbl_body);
        g_block_terminated = false;
        sym_push_scope();
        sym_add(n->for_stmt.name, iter_ty, alloca_i, true);

        if (n->for_stmt.body->kind == AST_BLOCK) {
            for (int i = 0; i < n->for_stmt.body->block.nstmts; i++) {
                codegen_stmt(n->for_stmt.body->block.stmts[i], fn_ret_type);
            }
        }
        sym_pop_scope();

        /* Increment (continue target) */
        if (!g_block_terminated) {
            ir_emit("  br label %%L%d\n", lbl_incr);
        }
        ir_emit("L%d:\n", lbl_incr);
        int cur = ir_tmp();
        ir_emit("  %%t%d = load i32, ptr %%t%d\n", cur, alloca_i);
        int nxt = ir_tmp();
        ir_emit("  %%t%d = add i32 %%t%d, 1\n", nxt, cur);
        ir_emit("  store i32 %%t%d, ptr %%t%d\n", nxt, alloca_i);
        ir_emit("  br label %%L%d\n", lbl_cond);

        ir_emit("L%d:\n", lbl_end);
        g_block_terminated = false;

        /* Pop loop labels */
        g_loop_depth--;
        break;
    }

    case AST_INDEX_ASSIGN: {
        /* arr[idx] = val or ptr[idx] = val or expr[idx] = val — indexed write */

        /* Expression-based index assignment: b.data[0] = val */
        if (n->call.name[0] == 0 && n->call.nargs == 3) {
            IRValue base = codegen_expr(n->call.args[2], (Type){0});
            IRValue idx = codegen_expr(n->call.args[0], (Type){.kind = TYPE_I32});
            if (base.type.kind == TYPE_ARRAY) {
                Type elem_type = {.kind = base.type.elem_kind, .field_prime = base.type.field_prime};
                const char *elem_ir = type_to_ir(elem_type);
                IRValue val = codegen_expr(n->call.args[1], elem_type);
                int idx64 = ir_tmp();
                ir_emit("  %%t%d = sext i32 %%t%d to i64\n", idx64, idx.reg);
                int gep = ir_tmp();
                ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 0, i64 %%t%d\n",
                        gep, type_to_ir(base.type), base.reg, idx64);
                ir_emit("  store %s %%t%d, ptr %%t%d\n", elem_ir, val.reg, gep);
                break;
            }
            error(n->line, n->col, "cannot index-assign into non-array expression");
            break;
        }

        Symbol *arr_sym = sym_find(n->call.name);
        if (!arr_sym) {
            error(n->line, n->col, "undefined variable '%s'", n->call.name);
            break;
        }

        if (arr_sym->type.kind == TYPE_PTR) {
            /* Pointer index-assign: ptr[i] = val */
            Type pointee = type_pointee(arr_sym->type);
            const char *pointee_ir = type_to_ir(pointee);

            IRValue idx = codegen_expr(n->call.args[0], (Type){.kind = TYPE_I32});
            IRValue val = codegen_expr(n->call.args[1], pointee);

            /* Width harmonization: the value expression may resolve to a
             * different integer width than the pointee (e.g. an i32 ident
             * stored into *i64, or signed()'s i64 into *i32). */
            {
                int vb = type_ir_bitwidth(val.type);
                int pb = type_ir_bitwidth(pointee);
                if (vb > 0 && pb > 0 && vb != pb) {
                    int conv = ir_tmp();
                    if (vb < pb) {
                        ir_emit("  %%t%d = %s %s %%t%d to %s\n", conv,
                                type_is_signed(val.type) ? "sext" : "zext",
                                type_to_ir(val.type), val.reg, pointee_ir);
                    } else {
                        ir_emit("  %%t%d = trunc %s %%t%d to %s\n", conv,
                                type_to_ir(val.type), val.reg, pointee_ir);
                    }
                    val.reg = conv;
                    val.type = pointee;
                }
            }

            /* Load the pointer value */
            int ptr_val;
            if (arr_sym->ir_reg < 0) {
                ptr_val = ir_tmp();
                ir_emit("  %%t%d = load ptr, ptr @%s\n", ptr_val, arr_sym->name);
            } else if (arr_sym->is_alloca) {
                ptr_val = ir_tmp();
                ir_emit("  %%t%d = load ptr, ptr %%t%d\n", ptr_val, arr_sym->ir_reg);
            } else {
                ptr_val = arr_sym->ir_reg;
            }

            int idx64 = ir_tmp();
            ir_emit("  %%t%d = sext i32 %%t%d to i64\n", idx64, idx.reg);
            int gep = ir_tmp();
            ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 %%t%d\n",
                    gep, pointee_ir, ptr_val, idx64);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", pointee_ir, val.reg, gep);
            break;
        }

        /* Array index-assign (existing path) */
        Type elem_type = {.kind = arr_sym->type.elem_kind, .field_prime = arr_sym->type.field_prime};
        const char *elem_ir = type_to_ir(elem_type);

        IRValue idx = codegen_expr(n->call.args[0], (Type){.kind = TYPE_I32});
        IRValue val = codegen_expr(n->call.args[1], elem_type);

        int idx64 = ir_tmp();
        ir_emit("  %%t%d = sext i32 %%t%d to i64\n", idx64, idx.reg);
        int gep = ir_tmp();
        ir_emit("  %%t%d = getelementptr %s, ptr %%t%d, i64 0, i64 %%t%d\n",
                gep, type_to_ir(arr_sym->type), arr_sym->ir_reg, idx64);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", elem_ir, val.reg, gep);
        break;
    }

    case AST_BREAK: {
        if (g_loop_depth == 0) {
            error(n->line, n->col, "'break' outside of loop");
            break;
        }
        ir_emit("  br label %%L%d\n", g_loop_end_labels[g_loop_depth - 1]);
        g_block_terminated = true;
        break;
    }

    case AST_CONTINUE: {
        if (g_loop_depth == 0) {
            error(n->line, n->col, "'continue' outside of loop");
            break;
        }
        ir_emit("  br label %%L%d\n", g_loop_cond_labels[g_loop_depth - 1]);
        g_block_terminated = true;
        break;
    }

    case AST_BLOCK: {
        sym_push_scope();
        for (int i = 0; i < n->block.nstmts; i++) {
            codegen_stmt(n->block.stmts[i], fn_ret_type);
        }
        sym_pop_scope();
        break;
    }

    default:
        error(n->line, n->col, "unsupported statement kind %d", n->kind);
    }
}

/* ============================================================
 * Top-level codegen
 * ============================================================ */

/* ============================================================
 * Parallel dispatch runtime emission
 *
 * Emits three LLVM IR functions into the module:
 *   __get_num_cores  — env var override, sysconf detection, fallback
 *   __pfor_entry     — pthread entry: unpack {fn,ctx,start,end}, call worker
 *   __parallel_for   — threshold check, spawn threads, join
 *
 * These are emitted once, after field arithmetic functions,
 * before user function codegen.
 * ============================================================ */

/* ============================================================
 * Function purity analysis
 *
 * A function is pure if its body has no observable side effects:
 *   - No I/O calls (print, write, read, read_bytes, open, close, etc.)
 *   - No heap mutation (alloc, free, malloc)
 *   - No global variable assignment
 *   - All called functions are also pure (transitive)
 *
 * Pure functions are safe to call inside auto-parallelized loops.
 * ============================================================ */

/* Known impure builtins — any call to these makes a function impure */
static bool is_impure_builtin(const char *name) {
    return !strcmp(name, "print") || !strcmp(name, "printf") ||
           !strcmp(name, "puts") || !strcmp(name, "putchar") ||
           !strcmp(name, "write") || !strcmp(name, "read") ||
           !strcmp(name, "read_bytes") || !strcmp(name, "open") ||
           !strcmp(name, "close") || !strcmp(name, "creat") ||
           !strcmp(name, "exit") || !strcmp(name, "abort") ||
           !strcmp(name, "alloc") || !strcmp(name, "malloc") ||
           !strcmp(name, "realloc") ||
           !strcmp(name, "free") ||
           !strcmp(name, "field");  /* field(p) mallocs the %__Field */
}

/* Recursive walk to check if an expression has side effects */
static bool expr_is_pure(ASTNode *e) {
    if (!e) return true;
    switch (e->kind) {
    case AST_LIT_INT: case AST_LIT_BOOL: case AST_NULL: case AST_STR_LIT:
    case AST_IDENT: case AST_FIELD_ACCESS:
        return true;
    case AST_BINARY:
        return expr_is_pure(e->binary.lhs) && expr_is_pure(e->binary.rhs);
    case AST_UNARY:
        return expr_is_pure(e->unary.operand);
    case AST_CAST:
        return expr_is_pure(e->cast.expr);
    case AST_INDEX:
        return expr_is_pure(e->index_expr.index) &&
               (e->index_expr.obj_expr ? expr_is_pure(e->index_expr.obj_expr) : true);
    case AST_CALL: {
        const char *fn_name = e->call.name;
        if (is_impure_builtin(fn_name)) return false;
        FuncInfo *fi = find_func(fn_name);
        if (!fi) return false;  /* unknown function: conservative */
        if (fi->is_extern) return false;  /* extern: can't verify */
        if (fi->purity_computed && !fi->is_pure) return false;
        /* Check arguments */
        for (int i = 0; i < e->call.nargs; i++) {
            if (!expr_is_pure(e->call.args[i])) return false;
        }
        return true;
    }
    case AST_STRUCT_LIT: {
        for (int i = 0; i < e->struct_lit.nfields; i++) {
            if (!expr_is_pure(e->struct_lit.field_values[i])) return false;
        }
        return true;
    }
    default:
        return false;  /* conservative: unknown node kind */
    }
}

/* Check if a statement has side effects */
static bool stmt_is_pure(ASTNode *s) {
    if (!s) return true;
    switch (s->kind) {
    case AST_LET:
        return expr_is_pure(s->let_stmt.init);
    case AST_RETURN:
        return s->ret.value ? expr_is_pure(s->ret.value) : true;
    case AST_ASSIGN: {
        /* Assignment to a GLOBAL is a side effect.  Purity is computed
         * before function codegen, so no local scope exists: a hit in
         * the global table means the target is a module-level variable
         * (locals will shadow at codegen time, but a name that exists
         * ONLY as a global here is conservatively treated as a global
         * write — a same-named local would also hit, costing only
         * parallelism, never soundness). */
        for (int gi = g_globals.count - 1; gi >= 0; gi--) {
            if (!strcmp(g_globals.entries[gi].name, s->assign.name))
                return false;
        }
        return expr_is_pure(s->assign.value);
    }
    case AST_INDEX_ASSIGN:
        /* Index assign through arguments is OK (local mutation) */
        for (int i = 0; i < s->call.nargs; i++) {
            if (!expr_is_pure(s->call.args[i])) return false;
        }
        return true;
    case AST_IF:
        return expr_is_pure(s->if_expr.cond) &&
               stmt_is_pure(s->if_expr.then_b) &&
               (s->if_expr.else_b ? stmt_is_pure(s->if_expr.else_b) : true);
    case AST_FOR:
        return expr_is_pure(s->for_stmt.start) &&
               expr_is_pure(s->for_stmt.end) &&
               stmt_is_pure(s->for_stmt.body);
    case AST_WHILE:
        return expr_is_pure(s->while_stmt.cond) &&
               stmt_is_pure(s->while_stmt.body);
    case AST_BLOCK:
        for (int i = 0; i < s->block.nstmts; i++) {
            if (!stmt_is_pure(s->block.stmts[i])) return false;
        }
        return true;
    case AST_EXPR_STMT:
        return expr_is_pure(s->expr_stmt.expr);
    case AST_BREAK: case AST_CONTINUE:
        return true;
    default:
        return false;
    }
}

/* Does this expression (transitively) call a function that writes
 * arrays?  Used to propagate writes_arrays through call chains. */
static bool expr_calls_array_writer(ASTNode *e) {
    if (!e) return false;
    switch (e->kind) {
    case AST_LIT_INT: case AST_LIT_BOOL: case AST_NULL: case AST_STR_LIT:
    case AST_IDENT: case AST_FIELD_ACCESS:
        return false;
    case AST_BINARY:
        return expr_calls_array_writer(e->binary.lhs) ||
               expr_calls_array_writer(e->binary.rhs);
    case AST_UNARY:
        return expr_calls_array_writer(e->unary.operand);
    case AST_CAST:
        return expr_calls_array_writer(e->cast.expr);
    case AST_INDEX:
        return expr_calls_array_writer(e->index_expr.index) ||
               (e->index_expr.obj_expr ?
                expr_calls_array_writer(e->index_expr.obj_expr) : false);
    case AST_CALL: {
        FuncInfo *fi = find_func(e->call.name);
        /* Unknown/extern callees: assume they may write (conservative) */
        if (!fi || fi->is_extern) return true;
        if (fi->writes_arrays) return true;
        for (int i = 0; i < e->call.nargs; i++) {
            if (expr_calls_array_writer(e->call.args[i])) return true;
        }
        return false;
    }
    case AST_STRUCT_LIT:
        for (int i = 0; i < e->struct_lit.nfields; i++) {
            if (expr_calls_array_writer(e->struct_lit.field_values[i])) return true;
        }
        return false;
    default:
        return true;  /* unknown node: conservative */
    }
}

/* Does this statement (transitively) perform any indexed store? */
static bool stmt_writes_arrays(ASTNode *s) {
    if (!s) return false;
    switch (s->kind) {
    case AST_INDEX_ASSIGN:
        return true;  /* the store itself */
    case AST_LET:
        return expr_calls_array_writer(s->let_stmt.init);
    case AST_RETURN:
        return s->ret.value ? expr_calls_array_writer(s->ret.value) : false;
    case AST_ASSIGN:
        return expr_calls_array_writer(s->assign.value);
    case AST_IF:
        return expr_calls_array_writer(s->if_expr.cond) ||
               stmt_writes_arrays(s->if_expr.then_b) ||
               (s->if_expr.else_b ? stmt_writes_arrays(s->if_expr.else_b) : false);
    case AST_FOR:
        return expr_calls_array_writer(s->for_stmt.start) ||
               expr_calls_array_writer(s->for_stmt.end) ||
               stmt_writes_arrays(s->for_stmt.body);
    case AST_WHILE:
        return expr_calls_array_writer(s->while_stmt.cond) ||
               stmt_writes_arrays(s->while_stmt.body);
    case AST_BLOCK:
        for (int i = 0; i < s->block.nstmts; i++) {
            if (stmt_writes_arrays(s->block.stmts[i])) return true;
        }
        return false;
    case AST_EXPR_STMT:
        return expr_calls_array_writer(s->expr_stmt.expr);
    case AST_BREAK: case AST_CONTINUE:
        return false;
    default:
        return true;  /* unknown statement: conservative */
    }
}

/* Compute purity for all registered functions (two-pass fixed point) */
static void compute_all_purity(void) {
    /* Pass 1: initial purity based on body walk */
    for (int i = 0; i < g_nfuncs; i++) {
        if (g_funcs[i].is_extern) {
            g_funcs[i].is_pure = false;
            g_funcs[i].writes_arrays = true;
            g_funcs[i].purity_computed = true;
            continue;
        }
        if (!g_funcs[i].ast || !g_funcs[i].ast->fn_decl.body) {
            g_funcs[i].is_pure = false;
            g_funcs[i].writes_arrays = true;
            g_funcs[i].purity_computed = true;
            continue;
        }
        g_funcs[i].is_pure = stmt_is_pure(g_funcs[i].ast->fn_decl.body);
        g_funcs[i].writes_arrays = stmt_writes_arrays(g_funcs[i].ast->fn_decl.body);
        g_funcs[i].purity_computed = true;
    }
    /* Pass 2: re-evaluate (catches transitive dependencies resolved in pass 1) */
    for (int i = 0; i < g_nfuncs; i++) {
        if (g_funcs[i].is_extern || !g_funcs[i].ast || !g_funcs[i].ast->fn_decl.body)
            continue;
        g_funcs[i].is_pure = stmt_is_pure(g_funcs[i].ast->fn_decl.body);
        g_funcs[i].writes_arrays = stmt_writes_arrays(g_funcs[i].ast->fn_decl.body);
    }
}

static void emit_parallel_runtime(void) {
    if (g_pfor_emitted) return;
    g_pfor_emitted = true;

    /* __get_num_cores: env var → sysconf → fallback 8.
     * sysconf constant derived from -target flag at compile time:
     * macOS (darwin) = 58, Linux = 84, unknown = skip sysconf. */
    ir_emit("\n; --- parallel dispatch runtime ---\n");
    ir_emit("define internal i32 @__get_num_cores() alwaysinline {\n");
    ir_emit("entry:\n");
    ir_emit("  %%env = call ptr @getenv(ptr @.__tv_threads)\n");
    ir_emit("  %%has = icmp ne ptr %%env, null\n");
    ir_emit("  br i1 %%has, label %%parse, label %%detect\n");
    ir_emit("parse:\n");
    ir_emit("  %%n = call i32 @atoi(ptr %%env)\n");
    ir_emit("  %%ok = icmp sgt i32 %%n, 0\n");
    ir_emit("  br i1 %%ok, label %%ret_env, label %%detect\n");
    ir_emit("ret_env:\n");
    ir_emit("  ret i32 %%n\n");
    ir_emit("detect:\n");
    if (g_sysconf_nproc > 0) {
        ir_emit("  %%sc = call i64 @sysconf(i32 %d)\n", g_sysconf_nproc);
        ir_emit("  %%sc_ok = icmp sgt i64 %%sc, 0\n");
        ir_emit("  br i1 %%sc_ok, label %%ret_sc, label %%fb\n");
        ir_emit("ret_sc:\n");
        ir_emit("  %%nc = trunc i64 %%sc to i32\n");
        ir_emit("  ret i32 %%nc\n");
    } else {
        /* Unknown platform: skip sysconf, go straight to fallback */
        ir_emit("  br label %%fb\n");
    }
    ir_emit("fb:\n");
    ir_emit("  ret i32 8\n");
    ir_emit("}\n\n");

    /* __pfor_entry: pthread entry point.
     * arg points to { ptr fn, ptr ctx, i32 start, i32 end }
     * Calls fn(ctx, start, end) and returns null. */
    ir_emit("define internal ptr @__pfor_entry(ptr %%raw) {\n");
    ir_emit("entry:\n");
    ir_emit("  %%fn_pp = getelementptr i8, ptr %%raw, i64 0\n");
    ir_emit("  %%fn = load ptr, ptr %%fn_pp\n");
    ir_emit("  %%ctx_pp = getelementptr i8, ptr %%raw, i64 8\n");
    ir_emit("  %%ctx = load ptr, ptr %%ctx_pp\n");
    ir_emit("  %%start_pp = getelementptr i8, ptr %%raw, i64 16\n");
    ir_emit("  %%start = load i32, ptr %%start_pp\n");
    ir_emit("  %%end_pp = getelementptr i8, ptr %%raw, i64 20\n");
    ir_emit("  %%end = load i32, ptr %%end_pp\n");
    ir_emit("  call void %%fn(ptr %%ctx, i32 %%start, i32 %%end)\n");
    ir_emit("  ret ptr null\n");
    ir_emit("}\n\n");

    /* __parallel_for(fn, ctx, start, end): threshold → serial or spawn threads.
     * fn has signature void(ptr ctx, i32 start, i32 end). */
    ir_emit("define internal void @__parallel_for(ptr %%fn, ptr %%ctx, i32 %%lo, i32 %%hi) {\n");
    ir_emit("entry:\n");
    ir_emit("  %%rng = sub i32 %%hi, %%lo\n");
    ir_emit("  %%big = icmp sgt i32 %%rng, %d\n", PFOR_THRESHOLD);
    ir_emit("  br i1 %%big, label %%par_check, label %%serial\n");

    ir_emit("par_check:\n");
    ir_emit("  %%cores = call i32 @__get_num_cores()\n");
    ir_emit("  %%multi = icmp sgt i32 %%cores, 1\n");
    ir_emit("  br i1 %%multi, label %%setup, label %%serial\n");

    ir_emit("serial:\n");
    ir_emit("  call void %%fn(ptr %%ctx, i32 %%lo, i32 %%hi)\n");
    ir_emit("  ret void\n");

    /* Thread setup: clamp threads to min(cores, range/256, 32) */
    ir_emit("setup:\n");
    ir_emit("  %%max_t = sdiv i32 %%rng, 256\n");
    ir_emit("  %%t1 = icmp slt i32 %%cores, %%max_t\n");
    ir_emit("  %%t2 = select i1 %%t1, i32 %%cores, i32 %%max_t\n");
    ir_emit("  %%t3 = icmp slt i32 %%t2, 32\n");
    ir_emit("  %%t4 = select i1 %%t3, i32 %%t2, i32 32\n");
    ir_emit("  %%nt = icmp sgt i32 %%t4, 1\n");
    ir_emit("  %%nthr = select i1 %%nt, i32 %%t4, i32 1\n");
    /* Allocate thread handles (ptr each on macOS) and arg structs (24 bytes each) */
    ir_emit("  %%nthr64 = sext i32 %%nthr to i64\n");
    ir_emit("  %%tid_sz = mul i64 %%nthr64, 8\n");
    ir_emit("  %%tids = call ptr @malloc(i64 %%tid_sz)\n");
    ir_emit("  %%arg_sz = mul i64 %%nthr64, 24\n");
    ir_emit("  %%args = call ptr @malloc(i64 %%arg_sz)\n");
    ir_emit("  %%chunk = sdiv i32 %%rng, %%nthr\n");
    ir_emit("  br label %%spawn\n");

    /* Spawn loop */
    ir_emit("spawn:\n");
    ir_emit("  %%st = phi i32 [0, %%setup], [%%st_nxt, %%spawn_cont]\n");
    ir_emit("  %%sd = icmp sge i32 %%st, %%nthr\n");
    ir_emit("  br i1 %%sd, label %%join, label %%do_spawn\n");

    ir_emit("do_spawn:\n");
    ir_emit("  %%s_start = mul i32 %%st, %%chunk\n");
    ir_emit("  %%s_off = add i32 %%s_start, %%lo\n");
    ir_emit("  %%st1 = add i32 %%st, 1\n");
    ir_emit("  %%is_last = icmp eq i32 %%st1, %%nthr\n");
    ir_emit("  %%tent = add i32 %%s_off, %%chunk\n");
    ir_emit("  %%s_end = select i1 %%is_last, i32 %%hi, i32 %%tent\n");
    /* Fill arg struct: {fn, ctx, start, end} at args + st*24 */
    ir_emit("  %%aoff = mul i32 %%st, 24\n");
    ir_emit("  %%aoff64 = sext i32 %%aoff to i64\n");
    ir_emit("  %%aptr = getelementptr i8, ptr %%args, i64 %%aoff64\n");
    ir_emit("  store ptr %%fn, ptr %%aptr\n");
    ir_emit("  %%a1 = getelementptr i8, ptr %%aptr, i64 8\n");
    ir_emit("  store ptr %%ctx, ptr %%a1\n");
    ir_emit("  %%a2 = getelementptr i8, ptr %%aptr, i64 16\n");
    ir_emit("  store i32 %%s_off, ptr %%a2\n");
    ir_emit("  %%a3 = getelementptr i8, ptr %%aptr, i64 20\n");
    ir_emit("  store i32 %%s_end, ptr %%a3\n");
    /* pthread_create */
    ir_emit("  %%toff = mul i32 %%st, 8\n");
    ir_emit("  %%toff64 = sext i32 %%toff to i64\n");
    ir_emit("  %%tptr = getelementptr i8, ptr %%tids, i64 %%toff64\n");
    ir_emit("  call i32 @pthread_create(ptr %%tptr, ptr null, ptr @__pfor_entry, ptr %%aptr)\n");
    ir_emit("  br label %%spawn_cont\n");

    ir_emit("spawn_cont:\n");
    ir_emit("  %%st_nxt = add i32 %%st, 1\n");
    ir_emit("  br label %%spawn\n");

    /* Join loop */
    ir_emit("join:\n");
    ir_emit("  %%jt = phi i32 [0, %%spawn], [%%jt_nxt, %%join_cont]\n");
    ir_emit("  %%jd = icmp sge i32 %%jt, %%nthr\n");
    ir_emit("  br i1 %%jd, label %%cleanup, label %%do_join\n");

    ir_emit("do_join:\n");
    ir_emit("  %%jtoff = mul i32 %%jt, 8\n");
    ir_emit("  %%jtoff64 = sext i32 %%jtoff to i64\n");
    ir_emit("  %%jtptr = getelementptr i8, ptr %%tids, i64 %%jtoff64\n");
    ir_emit("  %%tid = load ptr, ptr %%jtptr\n");
    ir_emit("  call i32 @pthread_join(ptr %%tid, ptr null)\n");
    ir_emit("  br label %%join_cont\n");

    ir_emit("join_cont:\n");
    ir_emit("  %%jt_nxt = add i32 %%jt, 1\n");
    ir_emit("  br label %%join\n");

    ir_emit("cleanup:\n");
    ir_emit("  call void @free(ptr %%tids)\n");
    ir_emit("  call void @free(ptr %%args)\n");
    ir_emit("  ret void\n");
    ir_emit("}\n\n");
}

/* ============================================================
 * Loop body analysis for parallel dispatch
 *
 * Single walk over the loop body AST.  Extracts three things
 * from the local structure:
 *   1. EC-1: field accumulation wraparound warning (existing)
 *   2. Independence: is each iteration free of loop-carried deps?
 *   3. Free vars: what symbols from enclosing scope are referenced?
 * ============================================================ */

/* (PForAnalysis typedef is forward-declared near line 3714) */

/* ------------------------------------------------------------
 * Write-index affine classifier.
 *
 * A write arr[idx] is admissible for parallel dispatch only if idx
 * flattens (over +/-) to:   loop_var + lit + (±t1 ±t2 ...)
 * where each tk is a loop-invariant identifier from the enclosing
 * scope and the loop variable appears EXACTLY ONCE with coefficient +1.
 * This makes the iteration→cell map provably injective (stride 1).
 *
 * Default-deny: constant indices, loop-invariant indices, the loop var
 * under * / % >> &, loop-local let bindings, and unknown identifiers
 * are all rejected — the loop stays sequential.  The only failure mode
 * of this analysis is lost parallelism, never a race.
 * ------------------------------------------------------------ */

static bool pfor_is_loop_let(PForAnalysis *out, const char *name) {
    for (int i = 0; i < out->nlets; i++) {
        if (!strcmp(out->lets[i], name)) return true;
    }
    return false;
}

static bool pfor_walk_expr(ASTNode *e, const char *loop_var, PForAnalysis *out);

/* Does this expression reference the loop variable or a loop-local let
 * anywhere in its tree?  Inside a parallel candidate body no AST_ASSIGN
 * is permitted, so any enclosing-scope scalar is constant across
 * iterations — a subtree is loop-invariant iff it avoids the loop var
 * and all let-bound names. */
static bool pfor_expr_uses(ASTNode *e, const char *loop_var, PForAnalysis *out) {
    if (!e) return false;
    switch (e->kind) {
    case AST_LIT_INT: case AST_LIT_BOOL: case AST_NULL: case AST_STR_LIT:
        return false;
    case AST_IDENT:
        return !strcmp(e->ident.name, loop_var) ||
               pfor_is_loop_let(out, e->ident.name);
    case AST_BINARY:
        return pfor_expr_uses(e->binary.lhs, loop_var, out) ||
               pfor_expr_uses(e->binary.rhs, loop_var, out);
    case AST_UNARY:
        return pfor_expr_uses(e->unary.operand, loop_var, out);
    case AST_CAST:
        return pfor_expr_uses(e->cast.expr, loop_var, out);
    default:
        return true;  /* unknown node: assume it varies (conservative) */
    }
}

/* Serialize an expression into a canonical string key for term matching.
 * Two equal keys denote the same loop-invariant value (no assignments
 * are possible inside a parallel candidate body).  Only literals,
 * identifiers, binary/unary ops and casts are serializable — array
 * reads and calls are not (their value cannot be keyed structurally).
 * Returns false on overflow or unsupported node. */
static bool pfor_serialize_expr(ASTNode *e, char *buf, int cap, int *pos) {
#define PFS_PUT(...) do { \
        int _n = snprintf(buf + *pos, (size_t)(cap - *pos), __VA_ARGS__); \
        if (_n < 0 || _n >= cap - *pos) return false; \
        *pos += _n; \
    } while (0)
    if (!e) return false;
    switch (e->kind) {
    case AST_LIT_INT:
        PFS_PUT("%llu", (unsigned long long)e->lit_int.value);
        return true;
    case AST_IDENT:
        PFS_PUT("%s", e->ident.name);
        return true;
    case AST_BINARY:
        /* The ':' terminator makes the op marker uniquely decodable.
         * Without it, "b1" + rhs "63" collides with "b16" + rhs "3"
         * (OP_SUB vs OP_BITAND) — identifiers and literals contain
         * digits, so adjacent digit runs are ambiguous.  ':' cannot
         * appear in idents, literals, or type names, so scanning back
         * from ':' to the nearest 'b' yields exactly one parse. */
        PFS_PUT("(");
        if (!pfor_serialize_expr(e->binary.lhs, buf, cap, pos)) return false;
        PFS_PUT("b%d:", (int)e->binary.op);
        if (!pfor_serialize_expr(e->binary.rhs, buf, cap, pos)) return false;
        PFS_PUT(")");
        return true;
    case AST_UNARY:
        PFS_PUT("u%d(", (int)e->unary.op);
        if (!pfor_serialize_expr(e->unary.operand, buf, cap, pos)) return false;
        PFS_PUT(")");
        return true;
    case AST_CAST:
        /* Include the target type: (x as u8) and (x as i64) are
         * DIFFERENT values when truncation occurs — they must not
         * share a term key. */
        PFS_PUT("c%s(", e->cast.type_name);
        if (!pfor_serialize_expr(e->cast.expr, buf, cap, pos)) return false;
        PFS_PUT(")");
        return true;
    default:
        return false;
    }
#undef PFS_PUT
}

/* Flatten an index expression over +/- into AffineIdx normal form:
 *   loop_var (coeff +1, at most once) + literal + (±loop-invariant terms)
 * Terms are arbitrary loop-invariant serializable subtrees, keyed by
 * canonical serialization (so `half`, `n/2`, `r*8` are all valid terms).
 * Identifiers inside terms are captured via pfor_walk_expr. */
static bool pfor_classify_index(ASTNode *e, const char *loop_var,
                                PForAnalysis *out, AffineIdx *idx,
                                int sign, int *lv_count) {
    if (!e) return false;

    if (e->kind == AST_LIT_INT) {
        idx->lit += (int64_t)sign * (int64_t)e->lit_int.value;
        return true;
    }

    if (e->kind == AST_IDENT && !strcmp(e->ident.name, loop_var)) {
        /* Loop variable: must appear with +1 coefficient, once */
        if (sign != 1) return false;
        (*lv_count)++;
        return (*lv_count == 1);
    }

    if (e->kind == AST_BINARY && e->binary.op == OP_ADD) {
        return pfor_classify_index(e->binary.lhs, loop_var, out, idx, sign, lv_count) &&
               pfor_classify_index(e->binary.rhs, loop_var, out, idx, sign, lv_count);
    }
    if (e->kind == AST_BINARY && e->binary.op == OP_SUB) {
        return pfor_classify_index(e->binary.lhs, loop_var, out, idx, sign, lv_count) &&
               pfor_classify_index(e->binary.rhs, loop_var, out, idx, -sign, lv_count);
    }

    /* Anything else is a candidate TERM: admissible only if it is
     * provably loop-invariant (does not mention the loop var or any
     * loop-local let — enclosing scalars cannot change inside a
     * parallel candidate body because AST_ASSIGN is disallowed) and
     * structurally serializable (no array reads, no calls). */
    if (pfor_expr_uses(e, loop_var, out)) return false;
    char key[MAX_IDENT];
    int pos = 0;
    if (!pfor_serialize_expr(e, key, MAX_IDENT, &pos)) return false;
    if (idx->nterms >= MAX_AFFINE_TERMS) return false;
    strcpy(idx->terms[idx->nterms], key);
    idx->signs[idx->nterms] = sign;
    idx->nterms++;
    /* Collect captures for identifiers inside the term */
    if (!pfor_walk_expr(e, loop_var, out)) return false;
    return true;
}

/* Classify a write index and record it.  Returns false (reject loop)
 * unless the index is stride-1 affine in the loop variable. */
static bool affine_eq(const AffineIdx *a, const AffineIdx *b);

static bool pfor_record_write(const char *arr_name, ASTNode *idx_expr,
                              const char *loop_var, PForAnalysis *out) {
    PForWrite w;
    memset(&w.idx, 0, sizeof(w.idx));
    int lv_count = 0;
    if (!pfor_classify_index(idx_expr, loop_var, out, &w.idx, 1, &lv_count))
        return false;
    if (lv_count != 1) return false;  /* no loop var = non-injective map */
    w.idx.has_lv = true;
    strcpy(w.arr, arr_name);
    /* Dedup identical write forms (same array, same affine form) */
    for (int i = 0; i < out->nwrites; i++) {
        if (!strcmp(out->writes[i].arr, arr_name) &&
            affine_eq(&out->writes[i].idx, &w.idx)) return true;
    }
    if (out->nwrites >= MAX_PFOR_WRITES) return false;
    out->writes[out->nwrites++] = w;
    return true;
}

/* Record a read arr[idx] for hazard analysis.  Reads never reject the
 * loop by themselves — an unclassifiable read is recorded as WILD and
 * only matters if the same array is also written. */
static void pfor_record_read(const char *arr_name, ASTNode *idx_expr,
                             const char *loop_var, PForAnalysis *out) {
    PForRead r;
    memset(&r, 0, sizeof(r));
    strcpy(r.arr, arr_name);
    int lv_count = 0;
    if (pfor_classify_index(idx_expr, loop_var, out, &r.idx, 1, &lv_count) &&
        lv_count <= 1) {
        r.classified = true;
        r.idx.has_lv = (lv_count == 1);
    } else {
        r.classified = false;
    }
    /* Dedup */
    for (int i = 0; i < out->nreads; i++) {
        if (!strcmp(out->reads[i].arr, arr_name)) {
            if (!out->reads[i].classified && !r.classified) return;
            if (out->reads[i].classified && r.classified &&
                affine_eq(&out->reads[i].idx, &r.idx)) return;
        }
    }
    if (out->nreads >= MAX_PFOR_READS) {
        out->reads_overflow = true;
        return;
    }
    out->reads[out->nreads++] = r;
}

/* ------------------------------------------------------------
 * Affine form algebra: equality, difference, footprint disjointness.
 * ------------------------------------------------------------ */

static bool affine_eq(const AffineIdx *a, const AffineIdx *b) {
    if (a->lit != b->lit || a->nterms != b->nterms || a->has_lv != b->has_lv)
        return false;
    bool used[MAX_AFFINE_TERMS] = {false};
    for (int i = 0; i < a->nterms; i++) {
        bool found = false;
        for (int j = 0; j < b->nterms; j++) {
            if (!used[j] && a->signs[i] == b->signs[j] &&
                !strcmp(a->terms[i], b->terms[j])) {
                used[j] = true; found = true; break;
            }
        }
        if (!found) return false;
    }
    return true;
}

/* delta = b - a as a term multiset difference (cancelling matches).
 * Returns false if the result exceeds MAX_AFFINE_TERMS. */
static bool affine_delta(const AffineIdx *a, const AffineIdx *b,
                         int64_t *dlit,
                         char dterms[MAX_AFFINE_TERMS][MAX_IDENT],
                         int dsigns[MAX_AFFINE_TERMS], int *ndt) {
    *dlit = b->lit - a->lit;
    *ndt = 0;
    bool cancelled[MAX_AFFINE_TERMS] = {false};
    /* b's terms, minus matches from a */
    for (int j = 0; j < b->nterms; j++) {
        bool match = false;
        for (int i = 0; i < a->nterms; i++) {
            if (!cancelled[i] && a->signs[i] == b->signs[j] &&
                !strcmp(a->terms[i], b->terms[j])) {
                cancelled[i] = true; match = true; break;
            }
        }
        if (!match) {
            if (*ndt >= MAX_AFFINE_TERMS) return false;
            strcpy(dterms[*ndt], b->terms[j]);
            dsigns[*ndt] = b->signs[j];
            (*ndt)++;
        }
    }
    /* a's uncancelled terms, negated */
    for (int i = 0; i < a->nterms; i++) {
        if (cancelled[i]) continue;
        if (*ndt >= MAX_AFFINE_TERMS) return false;
        strcpy(dterms[*ndt], a->terms[i]);
        dsigns[*ndt] = -a->signs[i];
        (*ndt)++;
    }
    return true;
}

/* Loop bound facts extracted once per analyzed loop */
typedef struct {
    bool    start_is_lit;
    int64_t start_lit;
    bool    end_is_lit;
    int64_t end_lit;
    bool    end_key_ok;            /* end expr serialized successfully */
    char    end_key[MAX_IDENT];    /* canonical key of the end expr */
} PForBounds;

/* Prove footprints of two affine forms disjoint over [start, end).
 *
 * Both stride-1 (has_lv): footprints are translated copies of the
 * iteration interval; disjoint iff |delta| >= trip count.  Provable:
 *   (a) delta is pure literal, start/end literal:  |dlit| >= end - start
 *   (c) delta = ±end_key + dlit with sign-consistent slack and
 *       literal start >= 0:  |delta| = end + |slack| >= end - start
 * Constant read vs stride-1 write (all literals): point-vs-interval.
 * Everything else: not provable -> false (loop stays sequential). */
static bool pfor_disjoint(const AffineIdx *a, const AffineIdx *b,
                          const PForBounds *bd) {
    if (a->has_lv && b->has_lv) {
        int64_t dlit;
        char dterms[MAX_AFFINE_TERMS][MAX_IDENT];
        int dsigns[MAX_AFFINE_TERMS];
        int ndt;
        if (!affine_delta(a, b, &dlit, dterms, dsigns, &ndt)) return false;

        if (ndt == 0) {
            /* Case (a): literal delta vs literal trip count */
            if (!bd->start_is_lit || !bd->end_is_lit) return false;
            int64_t trip = bd->end_lit - bd->start_lit;
            if (trip <= 0) return true;  /* empty loop: vacuously disjoint */
            int64_t mag = dlit < 0 ? -dlit : dlit;
            return mag >= trip;
        }
        if (ndt == 1 && bd->end_key_ok && !strcmp(dterms[0], bd->end_key) &&
            bd->start_is_lit && bd->start_lit >= 0) {
            /* Case (c): delta = ±end + slack.
             *   sign +1: delta = end + dlit >= end - start  iff dlit >= -start
             *   sign -1: |delta| = end - dlit >= end - start iff dlit <= start */
            if (dsigns[0] == 1)  return dlit >= -bd->start_lit;
            if (dsigns[0] == -1) return dlit <= bd->start_lit;
        }
        return false;
    }
    /* Constant form vs stride-1 form: point vs interval, literals only */
    if (!a->has_lv && !b->has_lv) return false;  /* both const: cannot prove distinct */
    const AffineIdx *pt = a->has_lv ? b : a;
    const AffineIdx *iv = a->has_lv ? a : b;
    if (pt->nterms != 0 || iv->nterms != 0) return false;
    if (!bd->start_is_lit || !bd->end_is_lit) return false;
    if (bd->end_lit <= bd->start_lit) return true;  /* empty loop */
    return pt->lit < bd->start_lit + iv->lit ||
           pt->lit >= bd->end_lit + iv->lit;
}

/* Final hazard check, run after the body walk has collected all
 * writes and reads.
 *
 * Write-write (same array): identical forms are fine (same cell within
 * one iteration — sequential inside the body); otherwise footprints
 * must be provably disjoint.
 *
 * Read-write (same array): a read matching a write form exactly reads
 * the iteration's own cell (fine); otherwise it must be provably
 * disjoint from EVERY write form on that array.  Wild reads of a
 * written array reject.  Distinct arrays are assumed non-aliasing
 * (documented capture-model assumption). */
static bool pfor_check_hazards(PForAnalysis *a, ASTNode *start, ASTNode *end) {
    PForBounds bd;
    memset(&bd, 0, sizeof(bd));
    bd.start_is_lit = start && start->kind == AST_LIT_INT;
    if (bd.start_is_lit) bd.start_lit = (int64_t)start->lit_int.value;
    bd.end_is_lit = end && end->kind == AST_LIT_INT;
    if (bd.end_is_lit) bd.end_lit = (int64_t)end->lit_int.value;
    int pos = 0;
    bd.end_key_ok = end && pfor_serialize_expr(end, bd.end_key, MAX_IDENT, &pos);

    /* Write-write */
    for (int i = 0; i < a->nwrites; i++) {
        for (int j = i + 1; j < a->nwrites; j++) {
            if (strcmp(a->writes[i].arr, a->writes[j].arr)) continue;
            /* Identical forms were deduped, so these differ: prove disjoint */
            if (!pfor_disjoint(&a->writes[i].idx, &a->writes[j].idx, &bd))
                return false;
        }
    }

    /* Read-write */
    if (a->reads_overflow && a->nwrites > 0) return false;
    /* #46 mirror: an alias-opaque (body-local-based) read plus any write —
     * the read may alias the written buffer; the name-keyed RAW check
     * below cannot see it. Refuse (lost parallelism, never a race). */
    if (a->private_read && a->nwrites > 0) return false;
    for (int r = 0; r < a->nreads; r++) {
        bool array_written = false;
        for (int w = 0; w < a->nwrites; w++) {
            if (!strcmp(a->reads[r].arr, a->writes[w].arr)) {
                array_written = true; break;
            }
        }
        if (!array_written) continue;
        if (!a->reads[r].classified) return false;  /* wild read of written array */
        /* Own-cell read? */
        bool own_cell = false;
        for (int w = 0; w < a->nwrites; w++) {
            if (!strcmp(a->reads[r].arr, a->writes[w].arr) &&
                affine_eq(&a->reads[r].idx, &a->writes[w].idx)) {
                own_cell = true; break;
            }
        }
        if (own_cell) continue;
        /* Must be disjoint from every write form on this array */
        for (int w = 0; w < a->nwrites; w++) {
            if (strcmp(a->reads[r].arr, a->writes[w].arr)) continue;
            if (!pfor_disjoint(&a->reads[r].idx, &a->writes[w].idx, &bd))
                return false;
        }
    }
    return true;
}

/* Check if an expression tree only uses the loop variable for array indexing
 * and collect referenced symbols from enclosing scope.  Returns false if
 * a loop-carried dependency or unsupported pattern is found. */
static bool pfor_walk_expr(ASTNode *e, const char *loop_var, PForAnalysis *out) {
    if (!e) return true;
    switch (e->kind) {
    case AST_LIT_INT:
    case AST_LIT_BOOL:
    case AST_NULL:
    case AST_STR_LIT:
        return true;

    case AST_IDENT: {
        /* An identifier reference — not an array access */
        const char *name = e->ident.name;
        if (!strcmp(name, loop_var)) return true; /* loop var itself, fine */
        /* Check if this is a captured variable */
        Symbol *sym = sym_find(name);
        if (!sym) return true; /* unknown — conservative, let codegen handle */
        /* Already captured? */
        for (int i = 0; i < out->ncaps; i++) {
            if (!strcmp(out->caps[i].name, name)) return true;
        }
        if (out->ncaps >= MAX_PFOR_CAP) return false; /* too many captures */
        PForCapture *cap = &out->caps[out->ncaps++];
        strcpy(cap->name, name);
        cap->type = sym->type;
        cap->alloca_reg = sym->ir_reg;
        cap->is_alloca = sym->is_alloca;
        return true;
    }

    case AST_BINARY:
        return pfor_walk_expr(e->binary.lhs, loop_var, out) &&
               pfor_walk_expr(e->binary.rhs, loop_var, out);

    case AST_UNARY:
        return pfor_walk_expr(e->unary.operand, loop_var, out);

    case AST_CAST:
        return pfor_walk_expr(e->cast.expr, loop_var, out);

    case AST_INDEX: {
        /* arr[idx] — a READ.  Record it for hazard analysis (reads of a
         * written array must match a write form or be provably disjoint;
         * checked at the end in pfor_check_hazards). */
        if (e->index_expr.obj_expr) return false; /* expression-based: too complex */
        if (!pfor_walk_expr(e->index_expr.index, loop_var, out)) return false;
        /* #46 mirror: a read through a body-local base is alias-opaque.
         * Taint; pfor_check_hazards refuses iff the loop also writes. */
        if (pfor_is_loop_let(out, e->index_expr.name)) out->private_read = true;
        pfor_record_read(e->index_expr.name, e->index_expr.index, loop_var, out);
        /* Add the array name as a capture */
        Symbol *arr = sym_find(e->index_expr.name);
        if (arr) {
            bool found = false;
            for (int c = 0; c < out->ncaps; c++) {
                if (!strcmp(out->caps[c].name, e->index_expr.name)) { found = true; break; }
            }
            if (!found && out->ncaps < MAX_PFOR_CAP) {
                PForCapture *cap = &out->caps[out->ncaps++];
                strcpy(cap->name, e->index_expr.name);
                cap->type = arr->type;
                cap->alloca_reg = arr->ir_reg;
                cap->is_alloca = arr->is_alloca;
            }
            if (arr->type.kind == TYPE_PTR &&
                (arr->type.ptr_pointee_kind == TYPE_FIELD ||
                 arr->type.ptr_pointee_kind == TYPE_DYNFIELD)) {
                out->has_field_element = true;
            }
        }
        return true;
    }

    case AST_CALL: {
        /* Function calls: allow if the called function is proven pure
         * AND performs no indexed stores.  "Pure" alone is insufficient:
         * stmt_is_pure admits writes through pointer parameters (caller
         * memory), which hide reductions — fn bump(acc,v){acc[0]+=v}
         * inside a parallel loop is a single-cell race. */
        const char *fn_name = e->call.name;
        if (is_impure_builtin(fn_name)) return false;
        FuncInfo *fi = find_func(fn_name);
        if (!fi || fi->is_extern) return false;
        if (fi->purity_computed && !fi->is_pure) return false;
        if (fi->purity_computed && fi->writes_arrays) return false;
        /* Pure function: walk arguments for captures */
        for (int i = 0; i < e->call.nargs; i++) {
            if (!pfor_walk_expr(e->call.args[i], loop_var, out)) return false;
        }
        return true;
    }

    default:
        return false;  /* unknown node kind — conservative reject */
    }
}

/* Analyze a loop body for parallel dispatch eligibility.
 * The body must be a block of AST_INDEX_ASSIGN statements
 * where each writes to array[loop_var] and reads from array[loop_var].
 * start/end are the loop bound expressions, used to prove footprint
 * disjointness for multi-write and cross-offset-read patterns. */
static PForAnalysis pfor_analyze_body(ASTNode *body, const char *loop_var,
                                      ASTNode *start, ASTNode *end) {
    PForAnalysis result = {0};
    result.is_independent = true;

    if (body->kind != AST_BLOCK) {
        result.is_independent = false;
        return result;
    }

    for (int i = 0; i < body->block.nstmts; i++) {
        ASTNode *stmt = body->block.stmts[i];

        if (stmt->kind == AST_INDEX_ASSIGN) {
            /* arr[idx] = expr — check that idx uses loop_var only */
            /* Expression-based index assign (obj.data[i] = val): too complex */
            if (stmt->call.name[0] == 0) {
                result.is_independent = false;
                return result;
            }
            /* #46 mirror (soundness): a write through a body-local base is
             * alias-opaque — the name-keyed hazard tables cannot see
             * `let p = out;`, so p-vs-out write forms are never
             * disjointness-checked (race demonstrated in tvc_self; this
             * analyzer shares the shape). A private name never appears as
             * an array base; an unresolvable base is refused for the same
             * reason (previously silently skipped the capture below). */
            if (pfor_is_loop_let(&result, stmt->call.name) ||
                !sym_find(stmt->call.name)) {
                result.is_independent = false;
                return result;
            }
            /* WRITE INDEX: must be stride-1 affine in the loop variable
             * (provably injective).  Constant, loop-invariant, derived
             * (i/2, i%k), or let-bound indices reject the whole loop. */
            if (!pfor_record_write(stmt->call.name, stmt->call.args[0],
                                   loop_var, &result)) {
                result.is_independent = false;
                return result;
            }
            if (!pfor_walk_expr(stmt->call.args[1], loop_var, &result)) {
                result.is_independent = false;
                return result;
            }
            /* Check the target array reference */
            Symbol *arr = sym_find(stmt->call.name);
            if (arr) {
                /* Add target array to captures */
                bool found = false;
                for (int c = 0; c < result.ncaps; c++) {
                    if (!strcmp(result.caps[c].name, stmt->call.name)) {
                        found = true;
                        break;
                    }
                }
                if (!found && result.ncaps < MAX_PFOR_CAP) {
                    PForCapture *cap = &result.caps[result.ncaps++];
                    strcpy(cap->name, stmt->call.name);
                    cap->type = arr->type;
                    cap->alloca_reg = arr->ir_reg;
                    cap->is_alloca = arr->is_alloca;
                }
                if (arr->type.kind == TYPE_PTR &&
                    (arr->type.ptr_pointee_kind == TYPE_FIELD ||
                     arr->type.ptr_pointee_kind == TYPE_DYNFIELD)) {
                    result.has_field_element = true;
                }
            }
            continue;
        }

        if (stmt->kind == AST_LET) {
            /* let x = expr — loop-local binding, re-created each iteration.
             * Walk the init expression for captures + independence check.
             * Record the name: a let-bound ident must never be used as a
             * write index (it can hide a non-injective map, e.g. t = i/2). */
            if (!pfor_walk_expr(stmt->let_stmt.init, loop_var, &result)) {
                result.is_independent = false;
                return result;
            }
            if (result.nlets >= MAX_PFOR_LETS) {
                result.is_independent = false;
                return result;
            }
            strcpy(result.lets[result.nlets++], stmt->let_stmt.name);
            continue;
        }

        if (stmt->kind == AST_EXPR_STMT) {
            /* Expression statement (e.g., bare function call).
             * Walk the expression — if it's a pure call, it's safe. */
            if (!pfor_walk_expr(stmt->expr_stmt.expr, loop_var, &result)) {
                result.is_independent = false;
                return result;
            }
            continue;
        }

        if (stmt->kind == AST_ASSIGN) {
            /* name = expr — this is a loop-carried dependency if
             * the name was assigned in a previous iteration.
             * For now: any non-index assignment kills independence. */
            result.is_independent = false;
            return result;
        }

        /* Any other statement type (if/else, etc.) — reject */
        result.is_independent = false;
        return result;
    }

    /* Final hazard check: write-write footprint disjointness and
     * read-after-write safety over the collected index forms. */
    if (result.is_independent &&
        !pfor_check_hazards(&result, start, end)) {
        result.is_independent = false;
    }

    return result;
}

/* ============================================================
 * Deferred worker function emission
 *
 * After all user functions are codegen'd, emit the __pfor_worker_N
 * functions.  Each one:
 *   - Takes (ptr ctx, i32 start, i32 end)
 *   - Loads captured variables from ctx
 *   - Runs the loop body over [start, end)
 * ============================================================ */

static void emit_pfor_workers(void) {
    for (int w = 0; w < g_npfor_workers; w++) {
        PForWorker *pw = &g_pfor_workers[w];

        /* Save global codegen state */
        int saved_tmp = g_tmp;
        int saved_label = g_label;
        bool saved_bt = g_block_terminated;
        int saved_prelude_len = g_prelude_len;
        bool saved_use_prelude = g_use_prelude;

        /* Reset for worker function */
        g_tmp = 0;
        g_label = 0;
        g_block_terminated = false;
        g_prelude_len = 0;
        g_use_prelude = false;

        /* Function signature: void (ptr ctx, i32 start, i32 end) */
        ir_emit("\ndefine internal void @__pfor_worker_%d(ptr %%p0, i32 %%p1, i32 %%p2) {\n",
                pw->id);
        ir_emit("entry:\n");

        sym_push_scope();

        /* Spill parameters to allocas (same pattern as codegen_fn) */
        int ctx_alloca = ir_tmp();
        ir_emit("  %%t%d = alloca ptr\n", ctx_alloca);
        ir_emit("  store ptr %%p0, ptr %%t%d\n", ctx_alloca);

        int start_alloca = ir_tmp();
        ir_emit("  %%t%d = alloca i32\n", start_alloca);
        ir_emit("  store i32 %%p1, ptr %%t%d\n", start_alloca);

        int end_alloca = ir_tmp();
        ir_emit("  %%t%d = alloca i32\n", end_alloca);
        ir_emit("  store i32 %%p2, ptr %%t%d\n", end_alloca);

        /* Load captured variables from context struct.
         * Context layout: captured[0] at offset 0, captured[1] at offset 8, etc.
         * Each captured variable is stored as its natural type at an 8-byte-aligned slot. */
        int ctx_val = ir_tmp();
        ir_emit("  %%t%d = load ptr, ptr %%t%d\n", ctx_val, ctx_alloca);

        for (int c = 0; c < pw->ncaps; c++) {
            PForCapture *cap = &pw->caps[c];
            const char *cap_ir = type_to_ir(cap->type);
            int gep = ir_tmp();
            ir_emit("  %%t%d = getelementptr i8, ptr %%t%d, i64 %d\n",
                    gep, ctx_val, c * 8);
            int loaded = ir_tmp();
            ir_emit("  %%t%d = load %s, ptr %%t%d\n", loaded, cap_ir, gep);
            /* Alloca for the captured variable (so codegen can load from it) */
            int cap_alloca = ir_tmp();
            ir_emit("  %%t%d = alloca %s\n", cap_alloca, cap_ir);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", cap_ir, loaded, cap_alloca);
            sym_add(cap->name, cap->type, cap_alloca, true);
        }

        /* Set up the loop: for i in start..end { body } */
        int iter_alloca = ir_tmp();
        ir_emit("  %%t%d = alloca i32\n", iter_alloca);
        int start_val = ir_tmp();
        ir_emit("  %%t%d = load i32, ptr %%t%d\n", start_val, start_alloca);
        ir_emit("  store i32 %%t%d, ptr %%t%d\n", start_val, iter_alloca);

        int end_val = ir_tmp();
        ir_emit("  %%t%d = load i32, ptr %%t%d\n", end_val, end_alloca);

        int lbl_cond = ir_label();
        int lbl_body = ir_label();
        int lbl_incr = ir_label();
        int lbl_end  = ir_label();

        ir_emit("  br label %%L%d\n", lbl_cond);

        /* Condition */
        ir_emit("L%d:\n", lbl_cond);
        int load_i = ir_tmp();
        ir_emit("  %%t%d = load i32, ptr %%t%d\n", load_i, iter_alloca);
        int cmp = ir_tmp();
        ir_emit("  %%t%d = icmp slt i32 %%t%d, %%t%d\n", cmp, load_i, end_val);
        ir_emit("  br i1 %%t%d, label %%L%d, label %%L%d\n", cmp, lbl_body, lbl_end);

        /* Body */
        ir_emit("L%d:\n", lbl_body);

        /* Save alloca insertion point for body-internal allocas */
        int alloca_insert_pos = g_ir_len;
        g_prelude_len = 0;
        g_use_prelude = true;

        /* Register loop variable in scope */
        sym_add(pw->loop_var, pw->iter_ty, iter_alloca, true);

        /* Push loop labels for break/continue support */
        g_loop_end_labels[g_loop_depth] = lbl_end;
        g_loop_cond_labels[g_loop_depth] = lbl_incr;
        g_loop_depth++;

        g_block_terminated = false;

        /* Codegen the loop body */
        if (pw->body->kind == AST_BLOCK) {
            for (int i = 0; i < pw->body->block.nstmts; i++) {
                codegen_stmt(pw->body->block.stmts[i], pw->fn_ret);
            }
        }

        g_loop_depth--;
        g_use_prelude = false;

        /* Insert any body-internal allocas into entry block */
        if (g_prelude_len > 0) {
            int body_len = g_ir_len - alloca_insert_pos;
            memmove(g_ir + alloca_insert_pos + g_prelude_len,
                    g_ir + alloca_insert_pos, body_len);
            memcpy(g_ir + alloca_insert_pos, g_prelude, g_prelude_len);
            g_ir_len += g_prelude_len;
        }

        /* Increment */
        if (!g_block_terminated) {
            ir_emit("  br label %%L%d\n", lbl_incr);
        }
        ir_emit("L%d:\n", lbl_incr);
        int cur = ir_tmp();
        ir_emit("  %%t%d = load i32, ptr %%t%d\n", cur, iter_alloca);
        int nxt = ir_tmp();
        ir_emit("  %%t%d = add i32 %%t%d, 1\n", nxt, cur);
        ir_emit("  store i32 %%t%d, ptr %%t%d\n", nxt, iter_alloca);
        ir_emit("  br label %%L%d\n", lbl_cond);

        ir_emit("L%d:\n", lbl_end);

        sym_pop_scope();

        ir_emit("  ret void\n");
        ir_emit("}\n");

        /* Restore codegen state */
        g_tmp = saved_tmp;
        g_label = saved_label;
        g_block_terminated = saved_bt;
        g_prelude_len = saved_prelude_len;
        g_use_prelude = saved_use_prelude;
    }
}

static void codegen_fn(ASTNode *n) {
    Type ret_type = resolve_type(n->fn_decl.ret_type);
    const char *ret_ir = type_to_ir(ret_type);

    /* Determine the function name: use mangled name if in monomorphization context */
    const char *fn_name = n->fn_decl.name;
    if (g_gen_nsub > 0 && g_gen_mangled[0]) {
        fn_name = g_gen_mangled;
    }

    /* Dyn instantiation: the function carries an implicit leading
     * 'ptr %__field' parameter (the runtime Field carrier). */
    bool fn_is_dyn = false;
    for (int gi = 0; gi < g_gen_nsub; gi++) {
        if (!strcmp(g_gen_sub_type[gi], "dyn")) { fn_is_dyn = true; break; }
    }

    /* Determine linkage: #[export] or explicit instantiation -> dso_local,
     * main -> external, else internal. */
    int is_exported = (n->fn_decl.attrs[0] && strstr(n->fn_decl.attrs, "export"));
    int is_main = !strcmp(fn_name, "main");
    const char *linkage = is_main ? "" :
        ((is_exported || g_mono_explicit) ? "dso_local " : "internal ");

    /* Function signature */
    if (is_main) {
        if (n->fn_decl.nparams > 0) {
            /* main(argc: i32, argv: **u8) — emit with parameters */
            ir_emit("\ndefine i32 @main(");
            for (int i = 0; i < n->fn_decl.nparams; i++) {
                if (i > 0) ir_emit(", ");
                Type pt = resolve_type(n->fn_decl.param_types[i]);
                ir_emit("%s %%%s", type_to_ir(pt), n->fn_decl.params[i]);
            }
            ir_emit(") {\n");
        } else {
            ir_emit("\ndefine i32 @main() {\n");
        }
        ir_emit("entry:\n");
    } else {
        ir_emit("\ndefine %s%s @%s(", linkage, ret_ir, fn_name);
        if (fn_is_dyn) {
            ir_emit("ptr %%__field");
            if (n->fn_decl.nparams > 0) ir_emit(", ");
        }
        for (int i = 0; i < n->fn_decl.nparams; i++) {
            if (i > 0) ir_emit(", ");
            Type pt = resolve_type(n->fn_decl.param_types[i]);
            ir_emit("%s %%%s", type_to_ir(pt), n->fn_decl.params[i]);
        }
        ir_emit(") {\n");
        ir_emit("entry:\n");
    }

    /* Reset state for this function */
    g_tmp = 0;
    g_label = 0;
    g_block_terminated = false;

    sym_push_scope();

    /* Dyn: spill the implicit Field carrier so binary-op codegen can
     * reach it via sym_find("__field"). */
    if (fn_is_dyn) {
        Type fvt = {.kind = TYPE_FIELD_VALUE};
        int fv_alloca = ir_tmp();
        ir_emit("  %%t%d = alloca ptr\n", fv_alloca);
        ir_emit("  store ptr %%__field, ptr %%t%d\n", fv_alloca);
        sym_add("__field", fvt, fv_alloca, true);
    }

    /* Register parameters as symbols */
    for (int i = 0; i < n->fn_decl.nparams; i++) {
        Type pt = resolve_type(n->fn_decl.param_types[i]);
        /* Alloca for parameter so it can be loaded by register number */
        int alloca_reg = ir_tmp();
        ir_emit("  %%t%d = alloca %s\n", alloca_reg, type_to_ir(pt));
        ir_emit("  store %s %%%s, ptr %%t%d\n",
                type_to_ir(pt), n->fn_decl.params[i], alloca_reg);
        sym_add(n->fn_decl.params[i], pt, alloca_reg, true);
    }

    /* Save insertion point for deferred allocas, then enable prelude buffer.
     * All ir_emit_alloca() calls during body codegen will write to g_prelude
     * instead of the main IR buffer. After body codegen, we insert the
     * collected allocas here (in the entry block, before any branches). */
    int alloca_insert_pos = g_ir_len;
    g_prelude_len = 0;
    g_use_prelude = true;

    /* Generate body */
    if (n->fn_decl.body->kind == AST_BLOCK) {
        for (int i = 0; i < n->fn_decl.body->block.nstmts; i++) {
            codegen_stmt(n->fn_decl.body->block.stmts[i], ret_type);
        }
    }

    g_use_prelude = false;

    /* Insert collected allocas into the entry block (at the saved position).
     * Shift the body forward to make room for the prelude. */
    if (g_prelude_len > 0) {
        int body_len = g_ir_len - alloca_insert_pos;
        memmove(g_ir + alloca_insert_pos + g_prelude_len,
                g_ir + alloca_insert_pos, body_len);
        memcpy(g_ir + alloca_insert_pos, g_prelude, g_prelude_len);
        g_ir_len += g_prelude_len;
    }

    sym_pop_scope();

    /* Default return (only if body didn't already terminate) */
    if (!g_block_terminated) {
        if (!strcmp(n->fn_decl.name, "main")) {
            ir_emit("  ret i32 0\n");
        } else if (ret_type.kind == TYPE_VOID) {
            ir_emit("  ret void\n");
        } else {
            error(n->line, n->col,
                  "function '%s' may not return a value on all paths",
                  n->fn_decl.name);
            ir_emit("  unreachable\n");
        }
    }

    ir_emit("}\n");
}

/* ============================================================
 * ZK Circuit Builder (#[zk] backend — Scope 5)
 * ============================================================
 *
 * Two-pass compilation for #[zk] functions:
 *   Pass 1: Walk AST -> build gate array + wire map + copy constraints
 *   Pass 2: Walk AST -> emit LLVM IR for companion _zk_prove function
 *
 * The companion function:
 *   1. Allocates circuit arrays (selectors, witness, sigma)
 *   2. Fills gate selectors (compile-time constants)
 *   3. Computes witness values (field arithmetic + stores to wa/wb/wc)
 *   4. Computes sigma arrays (omega powers * permutation structure)
 *   5. Calls plonk_prove
 *   6. Returns proof result
 */

typedef struct { uint64_t q_L, q_R, q_O, q_M, q_C; } ZkGate;
typedef struct { int gate, pos; } ZkWRef;   /* gate idx + wire position (0=a,1=b,2=c) */
typedef struct { char name[MAX_IDENT]; ZkWRef ref; int scope; } ZkWire;
typedef struct { ZkWRef a, b; } ZkCopy;
typedef struct { int gate, pos; } ZkPerm;   /* permutation target */

static ZkGate g_zk_gates[MAX_ZK_GATES];
static int    g_zk_ngates;
static ZkWire g_zk_wires[MAX_ZK_GATES * 2];
static int    g_zk_nwires;
static int    g_zk_scope;
static ZkCopy g_zk_copies[MAX_ZK_COPIES];
static int    g_zk_ncopies;
static ZkPerm g_zk_perm_a[MAX_ZK_GATES];
static ZkPerm g_zk_perm_b[MAX_ZK_GATES];
static ZkPerm g_zk_perm_c[MAX_ZK_GATES];
static int    g_zk_padded_n, g_zk_log_n;
static uint64_t g_zk_prime;
static char   g_zk_fn_prefix[128];
static const char *g_zk_elem_ir;

/* Union-find for copy constraint merging */
static int g_zk_uf[MAX_ZK_GATES * 3];

static void zk_init(uint64_t prime) {
    g_zk_ngates = 0; g_zk_nwires = 0; g_zk_ncopies = 0; g_zk_scope = 0;
    g_zk_prime = prime;
    snprintf(g_zk_fn_prefix, sizeof(g_zk_fn_prefix), "field%llu", (unsigned long long)prime);
    if (prime <= 255) g_zk_elem_ir = "i8";
    else if (prime <= 65535) g_zk_elem_ir = "i16";
    else if (prime <= 4294967295ULL) g_zk_elem_ir = "i32";
    else g_zk_elem_ir = "i64";
}

static ZkWRef zk_ref(int g, int p) { return (ZkWRef){g, p}; }

static int zk_add_gate(uint64_t qL, uint64_t qR, uint64_t qO, uint64_t qM, uint64_t qC) {
    int idx = g_zk_ngates++;
    g_zk_gates[idx] = (ZkGate){qL, qR, qO, qM, qC};
    return idx;
}

static ZkWRef *zk_find(const char *name) {
    for (int i = g_zk_nwires - 1; i >= 0; i--)
        if (!strcmp(g_zk_wires[i].name, name)) return &g_zk_wires[i].ref;
    return NULL;
}

static void zk_track(const char *nm, int g, int p) {
    ZkWire *w = &g_zk_wires[g_zk_nwires++];
    strcpy(w->name, nm); w->ref = zk_ref(g, p); w->scope = g_zk_scope;
}

static void zk_connect(ZkWRef from, int g, int p) {
    ZkWRef to = zk_ref(g, p);
    if (from.gate == g && from.pos == p) return;
    g_zk_copies[g_zk_ncopies++] = (ZkCopy){from, to};
}

static void zk_push(void) { g_zk_scope++; }
static void zk_pop(void) {
    while (g_zk_nwires > 0 && g_zk_wires[g_zk_nwires-1].scope == g_zk_scope)
        g_zk_nwires--;
    g_zk_scope--;
}

/* ---- Pass 1: Circuit builder (no IR emission) ---- */

static ZkWRef zk_build_expr(ASTNode *n) {
    uint64_t p = g_zk_prime, neg1 = p - 1;
    switch (n->kind) {
    case AST_LIT_INT: {
        uint64_t val = n->lit_int.value % p;
        int g = zk_add_gate(0, 0, neg1, 0, val);
        return zk_ref(g, 2);
    }
    case AST_IDENT: {
        ZkWRef *ref = zk_find(n->ident.name);
        if (!ref) { error(n->line, n->col, "#[zk] undefined '%s'", n->ident.name); return zk_ref(-1,-1); }
        return *ref;
    }
    case AST_BINARY: {
        bool lc = (n->binary.lhs->kind == AST_LIT_INT);
        bool rc = (n->binary.rhs->kind == AST_LIT_INT);
        switch (n->binary.op) {
        case OP_ADD: {
            if (rc) {
                ZkWRef lhs = zk_build_expr(n->binary.lhs);
                uint64_t c = n->binary.rhs->lit_int.value % p;
                int g = zk_add_gate(1, 0, neg1, 0, c);
                zk_connect(lhs, g, 0); return zk_ref(g, 2);
            }
            if (lc) {
                ZkWRef rhs = zk_build_expr(n->binary.rhs);
                uint64_t c = n->binary.lhs->lit_int.value % p;
                int g = zk_add_gate(0, 1, neg1, 0, c);
                zk_connect(rhs, g, 1); return zk_ref(g, 2);
            }
            ZkWRef lhs = zk_build_expr(n->binary.lhs);
            ZkWRef rhs = zk_build_expr(n->binary.rhs);
            int g = zk_add_gate(1, 1, neg1, 0, 0);
            zk_connect(lhs, g, 0); zk_connect(rhs, g, 1);
            return zk_ref(g, 2);
        }
        case OP_SUB: {
            if (rc) {
                ZkWRef lhs = zk_build_expr(n->binary.lhs);
                uint64_t c = n->binary.rhs->lit_int.value % p;
                int g = zk_add_gate(1, 0, neg1, 0, (p - c) % p);
                zk_connect(lhs, g, 0); return zk_ref(g, 2);
            }
            ZkWRef lhs = zk_build_expr(n->binary.lhs);
            ZkWRef rhs = zk_build_expr(n->binary.rhs);
            int g = zk_add_gate(1, neg1, neg1, 0, 0);
            zk_connect(lhs, g, 0); zk_connect(rhs, g, 1);
            return zk_ref(g, 2);
        }
        case OP_MUL: {
            if (rc) {
                ZkWRef lhs = zk_build_expr(n->binary.lhs);
                uint64_t c = n->binary.rhs->lit_int.value % p;
                int g = zk_add_gate(c, 0, neg1, 0, 0);
                zk_connect(lhs, g, 0); return zk_ref(g, 2);
            }
            if (lc) {
                ZkWRef rhs = zk_build_expr(n->binary.rhs);
                uint64_t c = n->binary.lhs->lit_int.value % p;
                int g = zk_add_gate(0, c, neg1, 0, 0);
                zk_connect(rhs, g, 1); return zk_ref(g, 2);
            }
            ZkWRef lhs = zk_build_expr(n->binary.lhs);
            ZkWRef rhs = zk_build_expr(n->binary.rhs);
            int g = zk_add_gate(0, 0, neg1, 1, 0);
            zk_connect(lhs, g, 0); zk_connect(rhs, g, 1);
            return zk_ref(g, 2);
        }
        default:
            error(n->line, n->col, "#[zk] unsupported op %d", n->binary.op);
            return zk_ref(-1,-1);
        }
    }
    case AST_IF: {
        /* If/else expression → conditional select in circuit.
         * select(cond, a, b) = cond*(a-b) + b  (4 gates: bool + diff + mul + add) */
        ZkWRef cond_ref = zk_build_expr(n->if_expr.cond);
        /* Boolean constraint: cond*(cond-1) = 0 → q_M=1, q_L=neg1 */
        int bc = zk_add_gate(neg1, 0, 0, 1, 0);
        zk_connect(cond_ref, bc, 0);
        zk_connect(cond_ref, bc, 1);
        /* Build both branches */
        ZkWRef then_ref = zk_build_expr(n->if_expr.then_b);
        ZkWRef else_ref = n->if_expr.else_b ? zk_build_expr(n->if_expr.else_b) : zk_ref(-1,-1);
        if (!n->if_expr.else_b) {
            /* if without else: select between then_val and 0 */
            else_ref.gate = zk_add_gate(0, 0, neg1, 0, 0);
            else_ref.pos = 2;
        }
        /* diff = then - else */
        int gd = zk_add_gate(1, neg1, neg1, 0, 0);
        zk_connect(then_ref, gd, 0);
        zk_connect(else_ref, gd, 1);
        /* sel = cond * diff */
        int gs = zk_add_gate(0, 0, neg1, 1, 0);
        zk_connect(cond_ref, gs, 0);
        zk_connect(zk_ref(gd, 2), gs, 1);
        /* result = sel + else */
        int gr = zk_add_gate(1, 1, neg1, 0, 0);
        zk_connect(zk_ref(gs, 2), gr, 0);
        zk_connect(else_ref, gr, 1);
        return zk_ref(gr, 2);
    }
    default:
        error(n->line, n->col, "#[zk] unsupported expr kind %d", n->kind);
        return zk_ref(-1,-1);
    }
}

static ZkWRef g_zk_ret_ref;

static void zk_build_stmt(ASTNode *n, Type ret_type) {
    switch (n->kind) {
    case AST_LET: {
        ZkWRef ref = zk_build_expr(n->let_stmt.init);
        zk_track(n->let_stmt.name, ref.gate, ref.pos);
        break;
    }
    case AST_ASSIGN: {
        ZkWRef ref = zk_build_expr(n->assign.value);
        zk_track(n->assign.name, ref.gate, ref.pos);
        break;
    }
    case AST_FOR: {
        /* For loops must have compile-time constant bounds for ZK unrolling */
        if (n->for_stmt.start->kind != AST_LIT_INT || n->for_stmt.end->kind != AST_LIT_INT)
            error(n->line, n->col, "#[zk] for loop bounds must be compile-time constants");
        int start = (int)n->for_stmt.start->lit_int.value;
        int end = (int)n->for_stmt.end->lit_int.value;
        for (int iter = start; iter < end; iter++) {
            zk_push();
            /* Create a constant gate for the loop variable value */
            uint64_t p = g_zk_prime, neg1 = p - 1;
            int g = zk_add_gate(0, 0, neg1, 0, (uint64_t)iter % p);
            zk_track(n->for_stmt.name, g, 2);
            /* Process body */
            if (n->for_stmt.body->kind == AST_BLOCK) {
                for (int s = 0; s < n->for_stmt.body->block.nstmts; s++)
                    zk_build_stmt(n->for_stmt.body->block.stmts[s], ret_type);
            } else {
                zk_build_stmt(n->for_stmt.body, ret_type);
            }
            zk_pop();
        }
        break;
    }
    case AST_RETURN:
        if (n->ret.value) g_zk_ret_ref = zk_build_expr(n->ret.value);
        break;
    case AST_BLOCK:
        zk_push();
        for (int i = 0; i < n->block.nstmts; i++)
            zk_build_stmt(n->block.stmts[i], ret_type);
        zk_pop();
        break;
    case AST_EXPR_STMT:
        zk_build_expr(n->expr_stmt.expr);
        break;
    default:
        error(n->line, n->col, "#[zk] unsupported stmt kind %d", n->kind);
        break;
    }
}

/* ---- Permutation builder ---- */

static int zk_uf_find(int x) {
    while (g_zk_uf[x] != x) { g_zk_uf[x] = g_zk_uf[g_zk_uf[x]]; x = g_zk_uf[x]; }
    return x;
}

static void zk_build_perm(void) {
    int n = g_zk_padded_n, total = n * 3;
    for (int i = 0; i < total; i++) g_zk_uf[i] = i;
    for (int i = 0; i < g_zk_ncopies; i++) {
        int a = g_zk_copies[i].a.gate * 3 + g_zk_copies[i].a.pos;
        int b = g_zk_copies[i].b.gate * 3 + g_zk_copies[i].b.pos;
        int ra = zk_uf_find(a), rb = zk_uf_find(b);
        if (ra != rb) g_zk_uf[ra] = rb;
    }
    /* Default: identity */
    for (int i = 0; i < n; i++) {
        g_zk_perm_a[i] = (ZkPerm){i, 0};
        g_zk_perm_b[i] = (ZkPerm){i, 1};
        g_zk_perm_c[i] = (ZkPerm){i, 2};
    }
    /* Form cycles per equivalence class */
    int members[MAX_ZK_GATES * 3];
    bool visited[MAX_ZK_GATES * 3];
    memset(visited, 0, sizeof(bool) * (size_t)total);
    for (int i = 0; i < total; i++) {
        int root = zk_uf_find(i);
        if (visited[root]) continue;
        visited[root] = true;
        int nm = 0;
        for (int j = 0; j < total; j++)
            if (zk_uf_find(j) == root) members[nm++] = j;
        if (nm <= 1) continue;
        for (int j = 0; j < nm; j++) {
            int src = members[j], dst = members[(j+1) % nm];
            int sg = src/3, sp = src%3;
            ZkPerm tgt = {dst/3, dst%3};
            if (sp == 0) g_zk_perm_a[sg] = tgt;
            else if (sp == 1) g_zk_perm_b[sg] = tgt;
            else g_zk_perm_c[sg] = tgt;
        }
    }
}

/* ---- Pass 2: Witness IR emitter ----
 * Walks the same AST as Pass 1 in the same order.
 * Emits field arithmetic + stores to wa/wb/wc arrays.
 * Gate counter g_zk_gctr stays in sync with Pass 1's gates.
 */

static int g_zk_gctr;

static int zk_emit_expr(ASTNode *n) {
    uint64_t p = g_zk_prime;
    const char *el = g_zk_elem_ir;
    const char *fp = g_zk_fn_prefix;

    switch (n->kind) {
    case AST_LIT_INT: {
        uint64_t val = n->lit_int.value % p;
        int g = g_zk_gctr++;
        int vr = ir_tmp();
        ir_emit("  %%t%d = add %s 0, %llu\n", vr, el, (unsigned long long)val);
        /* wc[g] = val */
        int wp = ir_tmp();
        ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", wp, el, g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, vr, wp);
        return vr;
    }
    case AST_IDENT: {
        /* Load from the variable's alloca */
        Symbol *s = sym_find(n->ident.name);
        if (!s) { error(n->line, n->col, "#[zk] witness: undefined '%s'", n->ident.name); return 0; }
        int r = ir_tmp();
        if (s->is_alloca)
            ir_emit("  %%t%d = load %s, ptr %%t%d\n", r, el, s->ir_reg);
        else
            ir_emit("  %%t%d = add %s 0, %%t%d\n", r, el, s->ir_reg);
        return r;
    }
    case AST_BINARY: {
        bool lc = (n->binary.lhs->kind == AST_LIT_INT);
        bool rc = (n->binary.rhs->kind == AST_LIT_INT);

        /* Constant-folded add: a + C */
        if (n->binary.op == OP_ADD && rc) {
            int lr = zk_emit_expr(n->binary.lhs);
            uint64_t c = n->binary.rhs->lit_int.value % p;
            int g = g_zk_gctr++;
            int cr = ir_tmp();
            ir_emit("  %%t%d = add %s 0, %llu\n", cr, el, (unsigned long long)c);
            int rr = ir_tmp();
            ir_emit("  %%t%d = call %s @%s_add(%s %%t%d, %s %%t%d)\n", rr, el, fp, el, lr, el, cr);
            /* wa[g] = lhs, wc[g] = result */
            int wa = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wa, i32 %d\n", wa, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, lr, wa);
            int wc = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", wc, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, rr, wc);
            return rr;
        }
        if (n->binary.op == OP_ADD && lc) {
            int rr0 = zk_emit_expr(n->binary.rhs);
            uint64_t c = n->binary.lhs->lit_int.value % p;
            int g = g_zk_gctr++;
            int cr = ir_tmp();
            ir_emit("  %%t%d = add %s 0, %llu\n", cr, el, (unsigned long long)c);
            int rr = ir_tmp();
            ir_emit("  %%t%d = call %s @%s_add(%s %%t%d, %s %%t%d)\n", rr, el, fp, el, cr, el, rr0);
            int wb = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wb, i32 %d\n", wb, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, rr0, wb);
            int wc = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", wc, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, rr, wc);
            return rr;
        }

        /* Constant-folded sub: a - C */
        if (n->binary.op == OP_SUB && rc) {
            int lr = zk_emit_expr(n->binary.lhs);
            uint64_t c = n->binary.rhs->lit_int.value % p;
            int g = g_zk_gctr++;
            int cr = ir_tmp();
            ir_emit("  %%t%d = add %s 0, %llu\n", cr, el, (unsigned long long)c);
            int rr = ir_tmp();
            ir_emit("  %%t%d = call %s @%s_sub(%s %%t%d, %s %%t%d)\n", rr, el, fp, el, lr, el, cr);
            int wa = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wa, i32 %d\n", wa, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, lr, wa);
            int wc = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", wc, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, rr, wc);
            return rr;
        }

        /* Constant-folded mul: a * C or C * a */
        if (n->binary.op == OP_MUL && rc) {
            int lr = zk_emit_expr(n->binary.lhs);
            uint64_t c = n->binary.rhs->lit_int.value % p;
            int g = g_zk_gctr++;
            int cr = ir_tmp();
            ir_emit("  %%t%d = add %s 0, %llu\n", cr, el, (unsigned long long)c);
            int rr = ir_tmp();
            ir_emit("  %%t%d = call %s @%s_mul(%s %%t%d, %s %%t%d)\n", rr, el, fp, el, lr, el, cr);
            int wa = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wa, i32 %d\n", wa, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, lr, wa);
            int wc = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", wc, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, rr, wc);
            return rr;
        }
        if (n->binary.op == OP_MUL && lc) {
            int rr0 = zk_emit_expr(n->binary.rhs);
            uint64_t c = n->binary.lhs->lit_int.value % p;
            int g = g_zk_gctr++;
            int cr = ir_tmp();
            ir_emit("  %%t%d = add %s 0, %llu\n", cr, el, (unsigned long long)c);
            int rr = ir_tmp();
            ir_emit("  %%t%d = call %s @%s_mul(%s %%t%d, %s %%t%d)\n", rr, el, fp, el, cr, el, rr0);
            int wb = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wb, i32 %d\n", wb, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, rr0, wb);
            int wc = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", wc, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, rr, wc);
            return rr;
        }

        /* General binary: both operands are variables */
        {
            int lr = zk_emit_expr(n->binary.lhs);
            int rr0 = zk_emit_expr(n->binary.rhs);
            int g = g_zk_gctr++;
            const char *op_fn = "add";
            if (n->binary.op == OP_SUB) op_fn = "sub";
            if (n->binary.op == OP_MUL) op_fn = "mul";
            int rr = ir_tmp();
            ir_emit("  %%t%d = call %s @%s_%s(%s %%t%d, %s %%t%d)\n",
                    rr, el, fp, op_fn, el, lr, el, rr0);
            /* Store wa[g], wb[g], wc[g] */
            int wa = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wa, i32 %d\n", wa, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, lr, wa);
            int wb = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wb, i32 %d\n", wb, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, rr0, wb);
            int wc = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", wc, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, rr, wc);
            return rr;
        }
    }
    case AST_IF: {
        /* If/else expression witness: evaluate cond, both branches, then select.
         * Must stay gate-synchronized with zk_build_expr's AST_IF. */
        int cond_r = zk_emit_expr(n->if_expr.cond);

        /* Boolean constraint gate (syncs with Pass 1) */
        int bc_g = g_zk_gctr++;
        int bc_wa = ir_tmp();
        ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wa, i32 %d\n", bc_wa, el, bc_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, cond_r, bc_wa);
        int bc_wb = ir_tmp();
        ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wb, i32 %d\n", bc_wb, el, bc_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, cond_r, bc_wb);
        /* wc for bool: cond*(cond-1) */
        int bc_sq = ir_tmp();
        ir_emit("  %%t%d = call %s @%s_mul(%s %%t%d, %s %%t%d)\n", bc_sq, el, fp, el, cond_r, el, cond_r);
        int bc_one = ir_tmp();
        ir_emit("  %%t%d = add %s 0, 1\n", bc_one, el);
        int bc_sub = ir_tmp();
        ir_emit("  %%t%d = call %s @%s_sub(%s %%t%d, %s %%t%d)\n", bc_sub, el, fp, el, bc_sq, el, cond_r);
        int bc_wc = ir_tmp();
        ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", bc_wc, el, bc_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, bc_sub, bc_wc);

        /* Evaluate both branches */
        int then_r = zk_emit_expr(n->if_expr.then_b);
        int else_r;
        if (n->if_expr.else_b) {
            else_r = zk_emit_expr(n->if_expr.else_b);
        } else {
            /* No else: zero constant gate (syncs with Pass 1) */
            int zg = g_zk_gctr++;
            else_r = ir_tmp();
            ir_emit("  %%t%d = add %s 0, 0\n", else_r, el);
            int zwc = ir_tmp();
            ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", zwc, el, zg);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, else_r, zwc);
        }

        /* diff = then - else (gate syncs with Pass 1) */
        int diff_g = g_zk_gctr++;
        int diff_r = ir_tmp();
        ir_emit("  %%t%d = call %s @%s_sub(%s %%t%d, %s %%t%d)\n", diff_r, el, fp, el, then_r, el, else_r);
        int dwa = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wa, i32 %d\n", dwa, el, diff_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, then_r, dwa);
        int dwb = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wb, i32 %d\n", dwb, el, diff_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, else_r, dwb);
        int dwc = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", dwc, el, diff_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, diff_r, dwc);

        /* sel = cond * diff (gate syncs with Pass 1) */
        int sel_g = g_zk_gctr++;
        int sel_r = ir_tmp();
        ir_emit("  %%t%d = call %s @%s_mul(%s %%t%d, %s %%t%d)\n", sel_r, el, fp, el, cond_r, el, diff_r);
        int swa = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wa, i32 %d\n", swa, el, sel_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, cond_r, swa);
        int swb = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wb, i32 %d\n", swb, el, sel_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, diff_r, swb);
        int swc = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", swc, el, sel_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, sel_r, swc);

        /* result = sel + else (gate syncs with Pass 1) */
        int res_g = g_zk_gctr++;
        int res_r = ir_tmp();
        ir_emit("  %%t%d = call %s @%s_add(%s %%t%d, %s %%t%d)\n", res_r, el, fp, el, sel_r, el, else_r);
        int rwa = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wa, i32 %d\n", rwa, el, res_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, sel_r, rwa);
        int rwb = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wb, i32 %d\n", rwb, el, res_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, else_r, rwb);
        int rwc = ir_tmp(); ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", rwc, el, res_g);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", el, res_r, rwc);
        return res_r;
    }
    default:
        error(n->line, n->col, "#[zk] witness: unsupported expr %d", n->kind);
        return 0;
    }
}

static void zk_emit_stmt(ASTNode *n, Type ret_type) {
    switch (n->kind) {
    case AST_LET: {
        int vr = zk_emit_expr(n->let_stmt.init);
        int ar = ir_tmp();
        ir_emit("  %%t%d = alloca %s\n", ar, g_zk_elem_ir);
        ir_emit("  store %s %%t%d, ptr %%t%d\n", g_zk_elem_ir, vr, ar);
        Type ft = {.kind = TYPE_FIELD, .field_prime = g_zk_prime};
        sym_add(n->let_stmt.name, ft, ar, true);
        break;
    }
    case AST_ASSIGN: {
        int vr = zk_emit_expr(n->assign.value);
        /* Find the existing alloca and store the new value */
        Symbol *s = sym_find(n->assign.name);
        if (!s) { error(n->line, n->col, "#[zk] witness: undefined '%s'", n->assign.name); break; }
        if (s->is_alloca) {
            ir_emit("  store %s %%t%d, ptr %%t%d\n", g_zk_elem_ir, vr, s->ir_reg);
        } else {
            error(n->line, n->col, "#[zk] witness: cannot assign to non-mutable '%s'", n->assign.name);
        }
        break;
    }
    case AST_FOR: {
        int start = (int)n->for_stmt.start->lit_int.value;
        int end = (int)n->for_stmt.end->lit_int.value;
        const char *el = g_zk_elem_ir;
        for (int iter = start; iter < end; iter++) {
            sym_push_scope();
            /* Constant gate for loop variable (syncs with Pass 1) */
            int g = g_zk_gctr++;
            int vr = ir_tmp();
            ir_emit("  %%t%d = add %s 0, %d\n", vr, el, iter);
            int wp = ir_tmp();
            ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", wp, el, g);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, vr, wp);
            /* Alloca for loop variable */
            int ar = ir_tmp();
            ir_emit("  %%t%d = alloca %s\n", ar, el);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, vr, ar);
            Type ft = {.kind = TYPE_FIELD, .field_prime = g_zk_prime};
            sym_add(n->for_stmt.name, ft, ar, true);
            /* Process body */
            if (n->for_stmt.body->kind == AST_BLOCK) {
                for (int s = 0; s < n->for_stmt.body->block.nstmts; s++)
                    zk_emit_stmt(n->for_stmt.body->block.stmts[s], ret_type);
            } else {
                zk_emit_stmt(n->for_stmt.body, ret_type);
            }
            sym_pop_scope();
        }
        break;
    }
    case AST_RETURN:
        if (n->ret.value) zk_emit_expr(n->ret.value);
        break;
    case AST_BLOCK:
        sym_push_scope();
        for (int i = 0; i < n->block.nstmts; i++)
            zk_emit_stmt(n->block.stmts[i], ret_type);
        sym_pop_scope();
        break;
    case AST_EXPR_STMT:
        zk_emit_expr(n->expr_stmt.expr);
        break;
    default:
        error(n->line, n->col, "#[zk] witness: unsupported stmt %d", n->kind);
        break;
    }
}

/* ---- Companion function emitter ---- */

static void codegen_zk_fn(ASTNode *fn) {
    Type ret_type = resolve_type(fn->fn_decl.ret_type);
    if (ret_type.kind != TYPE_FIELD && fn->fn_decl.nparams > 0)
        ret_type = resolve_type(fn->fn_decl.param_types[0]);
    if (ret_type.kind != TYPE_FIELD) {
        error(fn->line, fn->col, "#[zk] function must operate on a field type");
        return;
    }
    uint64_t p = ret_type.field_prime;
    zk_init(p);
    const char *el = g_zk_elem_ir;

    /* ---- Pass 1: build circuit ---- */
    /* Each parameter gets an "input gate" (all-zero selectors, unconstrained) */
    for (int i = 0; i < fn->fn_decl.nparams; i++) {
        int g = zk_add_gate(0, 0, 0, 0, 0);
        zk_track(fn->fn_decl.params[i], g, 2);
    }
    g_zk_ret_ref = zk_ref(-1, -1);
    if (fn->fn_decl.body->kind == AST_BLOCK)
        for (int i = 0; i < fn->fn_decl.body->block.nstmts; i++)
            zk_build_stmt(fn->fn_decl.body->block.stmts[i], ret_type);

    /* Pad to power of 2 (min 8) */
    g_zk_padded_n = 8; g_zk_log_n = 3;
    while (g_zk_padded_n < g_zk_ngates) { g_zk_padded_n <<= 1; g_zk_log_n++; }

    /* Build permutation */
    zk_build_perm();

    /* ---- Pass 2: emit companion _zk_prove function ---- */

    /* Emit extern declarations for plonk_prove/verify (once per module) */
    {
        static bool zk_externs_emitted = false;
        if (!zk_externs_emitted) {
            bool has_prove = false, has_verify = false;
            for (int fi = 0; fi < g_nfuncs; fi++) {
                if (!strcmp(g_funcs[fi].name, "plonk_prove")) has_prove = true;
                if (!strcmp(g_funcs[fi].name, "plonk_verify")) has_verify = true;
            }
            ir_emit("\n; ZK companion externs\n");
            if (!has_prove)
                ir_emit("declare i32 @plonk_prove(i32,i32,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,i32,ptr,ptr,ptr,ptr)\n");
            if (!has_verify)
                ir_emit("declare i32 @plonk_verify(i32,i32,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,ptr,i32,ptr,ptr,ptr,ptr,ptr,ptr,i32,ptr,ptr,ptr,ptr)\n");
            zk_externs_emitted = true;
        }
    }

    int n = g_zk_padded_n;
    int log_n = g_zk_log_n;
    int nbytes = n * 8;  /* each Goldilocks element is 8 bytes */

    /* Function signature: fn_name_zk_prove(original_params..., proof_buffers...) -> i32 */
    ir_emit("\ndefine i32 @%s_zk_prove(", fn->fn_decl.name);
    for (int i = 0; i < fn->fn_decl.nparams; i++) {
        if (i > 0) ir_emit(", ");
        ir_emit("%s %%arg_%s", el, fn->fn_decl.params[i]);
    }
    if (fn->fn_decl.nparams > 0) ir_emit(", ");
    /* Proof buffer parameters */
    ir_emit("ptr %%zk_eval_out, ptr %%zk_fri_roots, ptr %%zk_fri_challenges, ");
    ir_emit("ptr %%zk_fri_final, ptr %%zk_fri_qvals, ptr %%zk_fri_qpaths, ");
    ir_emit("ptr %%zk_fri_qidx, ptr %%zk_fri_nrounds, i32 %%zk_nqueries, ");
    ir_emit("ptr %%zk_rc_init, ptr %%zk_rc_term, ptr %%zk_rc_int, ptr %%zk_diag");
    ir_emit(") {\nentry:\n");

    g_tmp = 0; g_label = 0;

    /* Allocate circuit arrays */
    ir_emit("  ; Allocate circuit arrays (%d gates)\n", n);
    const char *arrs[] = {"wa","wb","wc","q_L","q_R","q_O","q_M","q_C","sig_a","sig_b","sig_c"};
    for (int a = 0; a < 11; a++) {
        (void)ir_tmp();  /* consume register number for naming consistency */
        ir_emit("  %%zk_%s = call ptr @malloc(i64 %d)\n", arrs[a], nbytes);
        ir_emit("  call void @llvm.memset.p0.i64(ptr %%zk_%s, i8 0, i64 %d, i1 false)\n", arrs[a], nbytes);
    }

    /* Fill gate selectors (compile-time constants) */
    ir_emit("  ; Gate selectors\n");
    for (int g = 0; g < g_zk_ngates; g++) {
        ZkGate *gate = &g_zk_gates[g];
        const char *sel_names[] = {"q_L","q_R","q_O","q_M","q_C"};
        uint64_t sel_vals[] = {gate->q_L, gate->q_R, gate->q_O, gate->q_M, gate->q_C};
        for (int s = 0; s < 5; s++) {
            if (sel_vals[s] == 0) continue;
            int gep = ir_tmp();
            ir_emit("  %%t%d = getelementptr %s, ptr %%zk_%s, i32 %d\n", gep, el, sel_names[s], g);
            ir_emit("  store %s %llu, ptr %%t%d\n", el, (unsigned long long)sel_vals[s], gep);
        }
    }

    /* Compute witness values */
    ir_emit("  ; Witness computation\n");
    sym_push_scope();
    /* Register parameters */
    for (int i = 0; i < fn->fn_decl.nparams; i++) {
        int ar = ir_tmp();
        ir_emit("  %%t%d = alloca %s\n", ar, el);
        ir_emit("  store %s %%arg_%s, ptr %%t%d\n", el, fn->fn_decl.params[i], ar);
        Type ft = {.kind = TYPE_FIELD, .field_prime = p};
        sym_add(fn->fn_decl.params[i], ft, ar, true);
        /* Store param value to wc of input gate */
        ir_emit("  %%t%d = getelementptr %s, ptr %%zk_wc, i32 %d\n", ir_tmp(), el, i);
        ir_emit("  store %s %%arg_%s, ptr %%t%d\n", el, fn->fn_decl.params[i], g_tmp - 1);
    }
    g_zk_gctr = fn->fn_decl.nparams;  /* skip input gates */
    if (fn->fn_decl.body->kind == AST_BLOCK)
        for (int i = 0; i < fn->fn_decl.body->block.nstmts; i++)
            zk_emit_stmt(fn->fn_decl.body->block.stmts[i], ret_type);
    sym_pop_scope();

    /* Compute sigma arrays using omega and permutation structure */
    ir_emit("  ; Sigma arrays (permutation)\n");
    /* Compute omega = generator^((p-1)/n) via repeated squaring */
    /* omega = 7^(2^(64-log_n)) / 7^(2^(32-log_n)) */
    /* We emit field arithmetic IR for this */
    int gen_r = ir_tmp();
    ir_emit("  %%t%d = add %s 0, 7\n", gen_r, el);  /* generator = 7 */
    /* hi = 7^(2^(64-log_n)): square (64-log_n) times */
    int hi = gen_r;
    for (int i = 0; i < 64 - log_n; i++) {
        int next = ir_tmp();
        ir_emit("  %%t%d = call %s @%s_mul(%s %%t%d, %s %%t%d)\n",
                next, el, g_zk_fn_prefix, el, hi, el, hi);
        hi = next;
    }
    /* lo = 7^(2^(32-log_n)): square (32-log_n) times */
    int lo = gen_r;
    /* Need a fresh copy of 7 since gen_r was consumed by squaring loop */
    lo = ir_tmp();
    ir_emit("  %%t%d = add %s 0, 7\n", lo, el);
    for (int i = 0; i < 32 - log_n; i++) {
        int next = ir_tmp();
        ir_emit("  %%t%d = call %s @%s_mul(%s %%t%d, %s %%t%d)\n",
                next, el, g_zk_fn_prefix, el, lo, el, lo);
        lo = next;
    }
    /* omega = hi / lo */
    int omega_r = ir_tmp();
    ir_emit("  %%t%d = call %s @%s_div(%s %%t%d, %s %%t%d)\n",
            omega_r, el, g_zk_fn_prefix, el, hi, el, lo);

    /* Compute omega powers: op[0]=1, op[i]=op[i-1]*omega */
    int *op_regs = malloc(n * sizeof(int));
    op_regs[0] = ir_tmp();
    ir_emit("  %%t%d = add %s 0, 1\n", op_regs[0], el);
    for (int i = 1; i < n; i++) {
        op_regs[i] = ir_tmp();
        ir_emit("  %%t%d = call %s @%s_mul(%s %%t%d, %s %%t%d)\n",
                op_regs[i], el, g_zk_fn_prefix, el, op_regs[i-1], el, omega_r);
    }

    /* k1, k2 constants */
    int k1_r = ir_tmp(); ir_emit("  %%t%d = add %s 0, 13\n", k1_r, el);
    int k2_r = ir_tmp(); ir_emit("  %%t%d = add %s 0, 17\n", k2_r, el);

    /* Fill sigma arrays from compile-time permutation structure */
    for (int i = 0; i < n; i++) {
        /* sigma_a[i] = identity(perm_a[i].gate, perm_a[i].pos) */
        ZkPerm *pa = &g_zk_perm_a[i], *pb = &g_zk_perm_b[i], *pc = &g_zk_perm_c[i];
        /* identity(gate, pos) = omega^gate if pos==0, k1*omega^gate if pos==1, k2*omega^gate if pos==2 */
        ZkPerm *perms[3] = {pa, pb, pc};
        const char *sig_names[3] = {"sig_a", "sig_b", "sig_c"};
        for (int col = 0; col < 3; col++) {
            ZkPerm *pm = perms[col];
            int val_r;
            if (pm->pos == 0) {
                val_r = op_regs[pm->gate];
            } else if (pm->pos == 1) {
                val_r = ir_tmp();
                ir_emit("  %%t%d = call %s @%s_mul(%s %%t%d, %s %%t%d)\n",
                        val_r, el, g_zk_fn_prefix, el, k1_r, el, op_regs[pm->gate]);
            } else {
                val_r = ir_tmp();
                ir_emit("  %%t%d = call %s @%s_mul(%s %%t%d, %s %%t%d)\n",
                        val_r, el, g_zk_fn_prefix, el, k2_r, el, op_regs[pm->gate]);
            }
            int gep = ir_tmp();
            ir_emit("  %%t%d = getelementptr %s, ptr %%zk_%s, i32 %d\n", gep, el, sig_names[col], i);
            ir_emit("  store %s %%t%d, ptr %%t%d\n", el, val_r, gep);
        }
    }
    free(op_regs);

    /* Call plonk_prove */
    ir_emit("  ; Call plonk_prove\n");
    int result = ir_tmp();
    ir_emit("  %%t%d = call i32 @plonk_prove(i32 %d, i32 %d, ", result, n, log_n);
    ir_emit("ptr %%zk_q_L, ptr %%zk_q_R, ptr %%zk_q_O, ptr %%zk_q_M, ptr %%zk_q_C, ");
    ir_emit("ptr %%zk_wa, ptr %%zk_wb, ptr %%zk_wc, ");
    ir_emit("ptr %%zk_sig_a, ptr %%zk_sig_b, ptr %%zk_sig_c, ");
    ir_emit("ptr %%zk_eval_out, ");
    ir_emit("ptr %%zk_fri_roots, ptr %%zk_fri_challenges, ptr %%zk_fri_final, ");
    ir_emit("ptr %%zk_fri_qvals, ptr %%zk_fri_qpaths, ");
    ir_emit("ptr %%zk_fri_qidx, ptr %%zk_fri_nrounds, i32 %%zk_nqueries, ");
    ir_emit("ptr %%zk_rc_init, ptr %%zk_rc_term, ptr %%zk_rc_int, ptr %%zk_diag)\n");

    /* Free arrays */
    for (int a = 0; a < 11; a++)
        ir_emit("  call void @free(ptr %%zk_%s)\n", arrs[a]);

    ir_emit("  ret i32 %%t%d\n", result);
    ir_emit("}\n");

    fprintf(stderr, "[zk] %s: %d gates (padded %d), %d copies, %d wires\n",
            fn->fn_decl.name, g_zk_ngates, n, g_zk_ncopies, g_zk_nwires);
}

/* Resolve a top-level global initializer to constant bits (mirror of
 * tvc_self.tv global_const_init — the two compilers must agree). Handles:
 * int literal; +|-|* of constants (the `0 - 1` negative idiom); ident -> a
 * PRIOR non-mut top-level `let` (recursive, index-decreasing, cycle-free).
 * Returns 1 on success; 0 = not a compile-time constant (caller diagnoses). */
static int global_const_init(ASTNode *prog, int upto, ASTNode *init, uint64_t *val) {
    if (init->kind == AST_LIT_INT) { *val = init->lit_int.value; return 1; }
    if (init->kind == AST_UNARY && init->unary.op == OP_NEG) {
        uint64_t nv;
        if (!global_const_init(prog, upto, init->unary.operand, &nv)) return 0;
        *val = (uint64_t)0 - nv;
        return 1;
    }
    if (init->kind == AST_BINARY) {
        OpKind op = init->binary.op;
        if (op == OP_ADD || op == OP_SUB || op == OP_MUL) {
            uint64_t lv, rv;
            if (global_const_init(prog, upto, init->binary.lhs, &lv) &&
                global_const_init(prog, upto, init->binary.rhs, &rv)) {
                *val = (op == OP_ADD) ? lv + rv : (op == OP_SUB) ? lv - rv : lv * rv;
                return 1;
            }
        }
        return 0;
    }
    if (init->kind == AST_IDENT) {
        for (int j = 0; j < upto; j++) {
            ASTNode *d = prog->program.decls[j];
            if (d->kind == AST_LET && d->let_stmt.is_global &&
                !strcmp(d->let_stmt.name, init->ident.name)) {
                if (!d->let_stmt.is_mut)
                    return global_const_init(prog, j, d->let_stmt.init, val);
                return 0;   /* a `var` global's initial value is not a constant contract */
            }
        }
        return 0;
    }
    return 0;
}

static void codegen_program(ASTNode *prog) {
    /* Module header */
    ir_emit("; Traveler compiler output\n");
    ir_emit("target triple = \"%s\"\n\n", g_target_triple);

    /* String constants */
    ir_emit("@.fmt_u = private constant [4 x i8] c\"%%u\\0A\\00\"\n");
    ir_emit("@.fmt_d = private constant [4 x i8] c\"%%d\\0A\\00\"\n");
    ir_emit("@.fmt_llu = private constant [6 x i8] c\"%%llu\\0A\\00\"\n");
    ir_emit("@.fmt_lld = private constant [6 x i8] c\"%%lld\\0A\\00\"\n\n");

    /* External declarations */
    ir_emit("declare i32 @printf(i8*, ...)\n");
    ir_emit("declare i64 @read(i32, ptr, i64)\n");
    ir_emit("declare i64 @write(i32, ptr, i64)\n");
    ir_emit("declare void @llvm.memset.p0.i64(ptr, i8, i64, i1)\n");
    ir_emit("declare void @abort()\n");
    ir_emit("declare ptr @malloc(i64)\n");
    ir_emit("declare ptr @realloc(ptr, i64)\n");
    ir_emit("declare void @free(ptr)\n");

    /* Parallel dispatch runtime declarations */
    ir_emit("declare i32 @pthread_create(ptr, ptr, ptr, ptr)\n");
    ir_emit("declare i32 @pthread_join(ptr, ptr)\n");
    ir_emit("declare i64 @sysconf(i32)\n");
    ir_emit("declare ptr @getenv(ptr)\n");
    ir_emit("declare i32 @atoi(ptr)\n");
    ir_emit("@.__tv_threads = private constant [17 x i8] c\"TRAVELER_THREADS\\00\"\n");
    /* Miller-Rabin witnesses for deterministic u64 primality (dyn fields) */
    ir_emit("@__mr_witnesses = private constant [12 x i64] "
            "[i64 2, i64 3, i64 5, i64 7, i64 11, i64 13, "
            "i64 17, i64 19, i64 23, i64 29, i64 31, i64 37]\n");

    /* First pass: register all field declarations and function signatures */
    for (int i = 0; i < prog->program.ndecls; i++) {
        ASTNode *d = prog->program.decls[i];
        if (d->kind == AST_FIELD_DECL) {
            if (d->field_decl.field_kind == FIELD_KIND_BINARY) {
                register_binfield(d->field_decl.name, d->field_decl.degree,
                                  d->field_decl.poly, d->line, d->col);
            } else if (d->field_decl.field_kind == FIELD_KIND_EXTENSION) {
                register_extfield(d->field_decl.name, d->field_decl.prime,
                                  d->field_decl.degree, d->line, d->col);
            } else {
                register_field(d->field_decl.name, d->field_decl.prime,
                              d->line, d->col);
            }
        }
        if (d->kind == AST_FN_DECL || d->kind == AST_EXTERN_FN) {
            if (g_nfuncs >= MAX_FUNCS) cap_overflow("function registry (g_funcs)", MAX_FUNCS);
            FuncInfo *fi = &g_funcs[g_nfuncs++];
            strcpy(fi->name, d->fn_decl.name);
            strcpy(fi->ret_type, d->fn_decl.ret_type);
            fi->nparams = d->fn_decl.nparams;
            fi->is_extern = (d->kind == AST_EXTERN_FN);
            for (int j = 0; j < d->fn_decl.nparams; j++) {
                strcpy(fi->param_types[j], d->fn_decl.param_types[j]);
            }
            /* Generic function support */
            fi->is_generic = (d->fn_decl.ngen > 0);
            fi->ngen = d->fn_decl.ngen;
            fi->ast = d;  /* store AST pointer for monomorphization re-codegen */
            for (int j = 0; j < d->fn_decl.ngen; j++) {
                strcpy(fi->gen_names[j], d->fn_decl.gen_names[j]);
                strcpy(fi->gen_bounds[j], d->fn_decl.gen_bounds[j]);
            }
        }
        if (d->kind == AST_STRUCT_DECL) {
            /* Find the pre-registered entry (from parse time) and populate fields */
            StructInfo *si = find_struct(d->struct_decl.name);
            if (!si) {
                /* Should not happen — parser pre-registers, but handle gracefully */
                if (g_nstructs >= MAX_FIELDS) cap_overflow("struct registry (g_structs)", MAX_FIELDS);
                si = &g_structs[g_nstructs++];
                strcpy(si->name, d->struct_decl.name);
                snprintf(si->ir_name, MAX_IDENT, "%%%s", d->struct_decl.name);
            }
            si->nfields = d->struct_decl.nfields;
            si->byte_size = 0;
            for (int j = 0; j < si->nfields; j++) {
                strcpy(si->fields[j].name, d->struct_decl.field_names[j]);
                strcpy(si->fields[j].type_name, d->struct_decl.field_types[j]);
                si->fields[j].type = resolve_type(d->struct_decl.field_types[j]);
            }
        }
    }

    /* Emit puts if needed by print() and not already extern'd by user */
    {
        bool has_puts = false;
        for (int i = 0; i < g_nfuncs; i++) {
            if (g_funcs[i].is_extern && !strcmp(g_funcs[i].name, "puts")) has_puts = true;
        }
        if (!has_puts) ir_emit("declare i32 @puts(ptr)\n");
    }

    /* Resolve enum variant types and compute layout */
    for (int i = 0; i < g_nenums; i++) {
        EnumInfo *ei = &g_enums[i];
        int max_payload = 0;
        for (int v = 0; v < ei->nvariants; v++) {
            EnumVariant *var = &ei->variants[v];
            var->payload_size = 0;
            for (int f = 0; f < var->nfields; f++) {
                var->fields[f] = resolve_type(var->field_types[f]);
                Type ft = var->fields[f];
                int fsz = 0;
                switch (ft.kind) {
                    case TYPE_BOOL: case TYPE_U8: case TYPE_I8: fsz = 1; break;
                    case TYPE_U16: case TYPE_I16: fsz = 2; break;
                    case TYPE_U32: case TYPE_I32: fsz = 4; break;
                    case TYPE_U64: case TYPE_I64: case TYPE_USIZE: fsz = 8; break;
                    case TYPE_FIELD: {
                        FieldInfo *fi = find_field_by_prime(ft.field_prime);
                        fsz = fi ? fi->elem_bits / 8 : 1;
                        break;
                    }
                    default:
                        /* #31 mirror: pointers 8; struct payloads their FULL
                           laid-out size (a >8-byte struct payload truncated
                           under the old flat 8); enum payloads total_size. */
                        fsz = type_sizeof(ft);
                        if (fsz <= 0) fsz = 8; /* forward-ref enum floor */
                        break;
                }
                /* Align to natural size */
                int align = fsz > 8 ? 8 : fsz;
                var->payload_size = (var->payload_size + align - 1) & ~(align - 1);
                var->payload_size += fsz;
            }
            if (var->payload_size > max_payload) max_payload = var->payload_size;
        }
        /* Total: 8 (tag + padding) + max payload, rounded up to 8 */
        ei->total_size = 8 + max_payload;
        ei->total_size = (ei->total_size + 7) & ~7;
    }

    /* Emit field arithmetic functions */
    for (int i = 0; i < g_nfields; i++) {
        if (g_fields[i].field_kind == FIELD_KIND_BINARY) {
            emit_gf256_tables();
            emit_gf256_funcs();
        } else if (g_fields[i].field_kind == FIELD_KIND_PRIME) {
            emit_field_funcs(g_fields[i].prime);
            if (g_fields[i].ntt_max_log > 0) emit_ntt_tables(&g_fields[i]);
        } else if (g_fields[i].field_kind == FIELD_KIND_EXTENSION) {
            /* Base field functions must be emitted first */
            emit_field_funcs(g_fields[i].prime);
            emit_extfield_funcs(&g_fields[i]);
            if (g_fields[i].ntt_max_log > 0) emit_ntt_tables(&g_fields[i]);
        }
    }

    /* Emit poly conversion functions for every prime field */
    for (int i = 0; i < g_nfields; i++) {
        if (g_fields[i].field_kind == FIELD_KIND_PRIME) {
            emit_poly_conv_funcs(&g_fields[i]);
        }
    }

    /* Emit parallel dispatch runtime (always; workers are deferred) */
    emit_parallel_runtime();

    /* Emit dynamic field runtime (always; internal funcs DCE'd if unused).
     * The %__Field type must precede the functions that reference it. */
    emit_dynfield_type();
    emit_dynfield_funcs();

    /* Emit struct type declarations */
    for (int i = 0; i < g_nstructs; i++) {
        StructInfo *si = &g_structs[i];
        ir_emit("\n%s = type { ", si->ir_name);
        for (int j = 0; j < si->nfields; j++) {
            if (j > 0) ir_emit(", ");
            ir_emit("%s", type_to_ir(si->fields[j].type));
        }
        ir_emit(" }\n");
    }

    /* Emit enum type declarations: opaque blob { i8 tag, [N x i8] pad+payload } */
    for (int i = 0; i < g_nenums; i++) {
        EnumInfo *ei = &g_enums[i];
        int pad_bytes = ei->total_size - 1;  /* total_size includes the tag byte */
        if (pad_bytes < 1) pad_bytes = 1;
        ir_emit("\n%s = type { i8, [%d x i8] }\n", ei->ir_name, pad_bytes);
    }

    /* Emit extern "C" function declarations.
     * Skip names that collide with compiler-emitted builtins. */
    for (int i = 0; i < g_nfuncs; i++) {
        if (!g_funcs[i].is_extern) continue;
        FuncInfo *efi = &g_funcs[i];
        /* Skip builtins already declared unconditionally by the compiler:
         * printf, read, write, abort, malloc, free, puts.
         * Other libc functions (putchar, strlen, strcmp, etc.) are NOT
         * auto-declared — user extern declarations are needed for those. */
        if (!strcmp(efi->name, "printf") ||
            !strcmp(efi->name, "read") || !strcmp(efi->name, "write") ||
            !strcmp(efi->name, "malloc") || !strcmp(efi->name, "free") ||
            !strcmp(efi->name, "realloc") || !strcmp(efi->name, "getenv") ||
            !strcmp(efi->name, "abort")) continue;
        Type eret = resolve_type(efi->ret_type);
        ir_emit("declare %s @%s(", type_to_ir(eret), efi->name);
        for (int j = 0; j < efi->nparams; j++) {
            if (j > 0) ir_emit(", ");
            Type pt = resolve_type(efi->param_types[j]);
            ir_emit("%s", type_to_ir(pt));
        }
        ir_emit(")\n");
    }

    /* Emit global variables and register them in the global symbol table */
    for (int i = 0; i < prog->program.ndecls; i++) {
        ASTNode *d = prog->program.decls[i];
        if (d->kind == AST_LET && d->let_stmt.is_global) {
            Type ty = resolve_type(d->let_stmt.type_name);
            const char *ir_ty = type_to_ir(ty);

            /* Determine initial value — must be a compile-time constant.
             * Mirrors tvc_self global_const_init: literal, +|-|* fold, ident ->
             * prior non-mut let. The old fallthrough SILENTLY zero-initialized
             * (known-issues #52); non-constants now diagnose. */
            if (d->let_stmt.init->kind == AST_NULL) {
                ir_emit("@%s = global ptr null\n", d->let_stmt.name);
            } else {
                uint64_t val = 0;
                if (!global_const_init(prog, i, d->let_stmt.init, &val)) {
                    error(d->line, d->col,
                          "global initializer must be a compile-time constant: "
                          "an int literal (incl. negated), null, +|-|* of constants, or a prior `let` constant");
                    exit(1);
                }
                if (ty.kind == TYPE_FIELD) val %= ty.field_prime;
                ir_emit("@%s = global %s %lld\n",
                        d->let_stmt.name, ir_ty, (long long)val);
            }

            /* Register in global symbol table.
             * ir_reg = -1 signals "use @name directly" in codegen.
             * We use a special marker to distinguish globals. */
            Symbol *gs = sym_add_global(d->let_stmt.name, ty, -(i + 1), d->let_stmt.is_mut);
            (void)gs;
        }
    }

    /* Compute function purity for auto-parallelization.
     * Must run after function registration (first pass) so all functions
     * are in the registry, before codegen (second pass) so the purity
     * flags are available when loop analysis runs. */
    compute_all_purity();

    /* Process explicit instantiation declarations BEFORE function codegen.
     * These only register monomorphization requests (no emission), so
     * call sites in regular functions can find the mangled names —
     * required for same-file calls to dyn instances, whose implicit
     * leading Field param cannot be inferred at the call site. Bodies
     * are emitted in the deferred pass below. */
    for (int i = 0; i < prog->program.ndecls; i++) {
        ASTNode *d = prog->program.decls[i];
        if (d->kind == AST_INSTANTIATE) {
            FuncInfo *generic_fi = find_func(d->fn_decl.name);
            if (!generic_fi || !generic_fi->is_generic) {
                error(d->line, d->col, "'%s' is not a generic function", d->fn_decl.name);
                continue;
            }
            /* Build concrete type array and monomorphize */
            const char *mangled = monomorphize(generic_fi,
                (const char (*)[MAX_IDENT])d->fn_decl.gen_names);
            /* Mark this instantiation for external linkage */
            MonoEntry *me = find_mono(generic_fi->name,
                (const char (*)[MAX_IDENT])d->fn_decl.gen_names, d->fn_decl.ngen);
            if (me) me->is_explicit = true;
            (void)mangled;
        }
    }

    /* Second pass: codegen functions (skip generic — they are templates) */
    for (int i = 0; i < prog->program.ndecls; i++) {
        ASTNode *d = prog->program.decls[i];
        if (d->kind == AST_FN_DECL && d->fn_decl.ngen == 0) {
            codegen_fn(d);
        }
    }

    /* Third pass: emit deferred monomorphized generic function bodies.
     * These were registered during the second pass when generic calls
     * were encountered, but codegen was deferred to avoid emitting
     * function definitions inside other functions. */
    emit_monomorphized_fns();

    /* Fourth pass: emit ZK companion functions for #[zk]-annotated functions */
    for (int i = 0; i < prog->program.ndecls; i++) {
        ASTNode *d = prog->program.decls[i];
        if (d->kind == AST_FN_DECL && d->fn_decl.ngen == 0 &&
            d->fn_decl.attrs[0] && strstr(d->fn_decl.attrs, "zk")) {
            codegen_zk_fn(d);
        }
    }

    /* Fifth pass: emit deferred parallel-for worker functions.
     * These were registered during AST_FOR codegen when the loop
     * body was proven algebraically independent by the type system. */
    emit_pfor_workers();

    /* Emit string constants (collected during codegen) */
    for (int i = 0; i < g_nstrings; i++) {
        int slen = g_strings[i].len;
        ir_emit("\n@.str.%d = private unnamed_addr constant [%d x i8] c\"",
                g_strings[i].id, slen + 1);
        for (int j = 0; j < slen; j++) {
            unsigned char ch = (unsigned char)g_strings[i].data[j];
            if (ch >= 32 && ch < 127 && ch != '"' && ch != '\\') {
                ir_emit("%c", ch);
            } else {
                ir_emit("\\%02X", ch);
            }
        }
        ir_emit("\\00\"\n"); /* null terminator */
    }
}

/* ============================================================
 * Main
 * ============================================================ */

static char *read_file(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) { fprintf(stderr, "error: cannot open '%s'\n", path); exit(1); }
    fseek(f, 0, SEEK_END);
    long len = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc(len + 1);
    if (!buf) { fprintf(stderr, "out of memory\n"); exit(1); }
    fread(buf, 1, len, f);
    buf[len] = 0;
    fclose(f);
    return buf;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: tvc <input.tv> [-o output.ll]\n");
        return 1;
    }

    const char *input_path = argv[1];
    const char *output_path = NULL;

    for (int i = 2; i < argc; i++) {
        if (!strcmp(argv[i], "-o") && i + 1 < argc) {
            output_path = argv[++i];
        } else if (!strcmp(argv[i], "-target") && i + 1 < argc) {
            strncpy(g_target_triple, argv[++i], sizeof(g_target_triple) - 1);
            /* Derive sysconf constant from target triple */
            if (strstr(g_target_triple, "darwin")) {
                g_sysconf_nproc = 58;
            } else if (strstr(g_target_triple, "linux")) {
                g_sysconf_nproc = 84;
            } else {
                g_sysconf_nproc = -1;  /* unknown platform: skip sysconf */
            }
        }
    }

    /* Default output: replace .tv with .ll */
    char default_out[512];
    if (!output_path) {
        strcpy(default_out, input_path);
        char *dot = strrchr(default_out, '.');
        if (dot) strcpy(dot, ".ll");
        else strcat(default_out, ".ll");
        output_path = default_out;
    }

    g_filename = input_path;
    g_source = read_file(input_path);

    /* Lex */
    lex(g_source);

    if (g_has_errors) return 1;

    /* Parse */
    ASTNode *prog = parse_program();

    if (g_has_errors) return 1;

    /* Codegen */
    codegen_program(prog);

    if (g_has_errors) return 1;

    /* Write output */
    FILE *out = fopen(output_path, "w");
    if (!out) {
        fprintf(stderr, "error: cannot write '%s'\n", output_path);
        return 1;
    }
    fwrite(g_ir, 1, g_ir_len, out);
    fclose(out);

    fprintf(stderr, "wrote %s (%d bytes)\n", output_path, g_ir_len);
    return 0;
}
