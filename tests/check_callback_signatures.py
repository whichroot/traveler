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
}
GOOD = PREFIX + '''
fn noop(x:i32){print(x);}
fn factory()->fn(i64)->i64{return &wide_value;}
fn generic<T>(f:fn(T)->T,x:T)->T{return f(x);}
fn main(){
    var f:fn(i64)->i64=null;f=factory();print(f(3));print(consume(f));
    let input:i64=1099511627776;print(generic(&wide_value,input));
    let v:fn(i32)->void=&noop;
    let c:fn(fn(i64)->i64)->i64=&consume;print(c(f));
}
'''
EXPECTED = '4\n4\n1099511627777\n4\n'


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
    for profile in ('raw', 'O1', 'O3'):
        selected = ir
        if profile != 'raw':
            selected = d / 'opt.ll'
            run(opt, '-passes=default<' + profile + '>', '-S', ir, '-o', selected)
        run(llc, '-filetype=obj', selected, '-o', d / 'case.o')
        run(linker, *flags, d / 'case.o', '-o', d / 'case')
        assert run(d / 'case') == EXPECTED, profile
print('callback signatures PASS: incompatible transfers refused; concrete/generic/nested/null/void controls; raw/O1/O3')
