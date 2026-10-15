"""Callback transfers preserve complete source signatures."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
PREFIX = '''
fn narrow(x:i32)->i32{return x+1;}
fn wide_value(x:i64)->i64{return x+1;}
fn pair(x:i32,y:i32)->i32{return x+y;}
fn consume(f:fn(i64)->i64)->i64{return f(3);}
'''
BAD = {
    'let': 'fn main(){let f:fn(i64)->i64=&narrow;}',
    'assign': 'fn main(){var f:fn(i64)->i64=&wide_value;f=&narrow;}',
    'argument': 'fn main(){print(consume(&narrow));}',
    'return': 'fn factory()->fn(i64)->i64{return &narrow;}fn main(){}',
    'arity': 'fn main(){let f:fn(i32)->i32=&pair;}',
    'struct': 'struct Slot { f:fn(i64)->i64 } fn main(){let s=Slot{f:&narrow};}',
    'enum': 'enum Slot { Callback(fn(i64)->i64) } fn main(){let s=Slot::Callback(&narrow);}',
    'indirect': 'fn main(){let c:fn(fn(i64)->i64)->i64=&consume;print(c(&narrow));}',
    'closure': 'fn main(){let c=|f:fn(i64)->i64| -> i64 f(3);print(c(&narrow));}',
    'signedness': 'fn main(){let f:fn(u32)->u32=&narrow;}',
    'return_only': 'fn main(){let f:fn(i32)->i64=&narrow;}',
    'parameter_only': 'fn main(){let f:fn(i64)->i32=&narrow;}',
    'family': 'fn bounded(x:Int<5>)->Int<5>{return x;}fn main(){let f:fn(Int<10>)->Int<10> = &bounded;}',
    'void': 'fn main(){let f:fn(i32)=&narrow;}',
    'nested': 'fn main(){let f:fn(fn(i32)->i32)->i64=&consume;}',
    'pointer_alias': 'fn main(){var f:fn(i32)->i32=&narrow;let p:*fn(i64)->i64=&f;}',
    'pointer_store': 'fn main(){var f:fn(i64)->i64=&wide_value;let p=&f;p[0]=&narrow;}',
    'array_store': 'fn main(){var a:[fn(i64)->i64;2]=0;a[1]=&narrow;}',
    'array_alias': 'fn main(){var a:[fn(i32)->i32;2]=0;let p:*fn(i64)->i64=&a[0];}',
    'member_store': 'struct Slot { f:fn(i64)->i64 }fn main(){var s=Slot{f:&wide_value};s.f=&narrow;}',
    'member_alias': 'struct Slot { f:fn(i32)->i32 }fn main(){var s=Slot{f:&narrow};let p:*fn(i64)->i64=&s.f;}',
    'generic_copy': 'struct Box<T>{value:T}fn main(){let a:Box<fn(i32)->i32> = Box{value:&narrow};let b:Box<fn(i64)->i64> = a;}',
    'generic_alias': 'struct Box<T>{value:T}fn main(){var a:Box<fn(i32)->i32> = Box{value:&narrow};let b:*Box<fn(i64)->i64> = &a;}',
    'generic_enum_copy': 'enum Slot<T>{V(T)}fn main(){let a:Slot<fn(i32)->i32> = Slot::V(&narrow);let b:Slot<fn(i64)->i64> = a;}',
    'cast': 'fn main(){let f=(&narrow) as fn(i64)->i64;}',
    'alias_cast': 'fn main(){var f:fn(i32)->i32=&narrow;let p=(&f) as *fn(i64)->i64;}',
}
GOOD = PREFIX + '''
fn noop(x:i32){print(x);}
fn factory()->fn(i64)->i64{return &wide_value;}
fn generic<T>(f:fn(T)->T,x:T)->T{return f(x);}
struct Slot { f:fn(i64)->i64 }
fn main(){
    var f:fn(i64)->i64=null;f=factory();print(f(3));print(consume(f));
    let empty=(null as fn(i64)->i64);
    let input:i64=1099511627776;print(generic(&wide_value,input));
    let v:fn(i32)->void=&noop;v(7);
    let c:fn(fn(i64)->i64)->i64=&consume;print(c(f));
    let p=&f;p[0]=&wide_value;print(f(9));
    var a:[fn(i64)->i64;2]=0;a[1]=f;let q=&a[1];q[0]=&wide_value;let af=a[1];print(af(10));
    var s=Slot{f:f};s.f=&wide_value;let sf=s.f;print(sf(11));
}
'''
EXPECTED = '4\n4\n1099511627777\n7\n4\n10\n11\n12\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='callback-signatures-') as tmp:
    d = Path(tmp)
    source, ir = d / 'case.tv', d / 'case.ll'
    for name, body in BAD.items():
        source.write_text(PREFIX + body)
        p = subprocess.run([tvc, str(source), '-o', str(ir)], capture_output=True, text=True, timeout=60)
        assert p.returncode != 0 and 'incompatible function-pointer signature' in p.stderr, (name, p.returncode, p.stderr)
    source.write_text(GOOD)
    run(tvc, source, '-o', ir)
    flags = shlex.split(os.environ.get('TRAVELER_LINK_FLAGS', '-no-pie' if sys.platform == 'linux' else ''))
    configs = [(llc, opt, [], ('raw', 'O1', 'O3'))]
    llvm14 = os.environ.get('LLVM14')
    if llvm14:
        triple = next(line.split(':', 1)[1].strip() for line in run(llc, '--version').splitlines() if 'Default target:' in line)
        configs.append((str(Path(llvm14) / 'bin/llc'), str(Path(llvm14) / 'bin/opt'), ['-mtriple=' + triple], ('raw', 'O3')))
    for compiler, optimizer, target, profiles in configs:
        if target:
            run(tvc, '-target', 'tpc', source, '-o', ir)
        for profile in profiles:
            selected = ir
            if profile != 'raw':
                selected = d / 'opt.ll'
                run(optimizer, *target, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
            run(compiler, *target, '-filetype=obj', selected, '-o', d / 'case.o')
            run(linker, *flags, d / 'case.o', '-o', d / 'case')
            assert run(d / 'case') == EXPECTED, (compiler, profile)
print('callback signatures PASS: incompatible transfers refused; concrete/generic/nested/null/void controls; raw/O1/O3')
