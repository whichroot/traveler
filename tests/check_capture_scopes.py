"""Lexical capture discovery preserves nested scopes and snapshots."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

tvc, llc, opt, linker = sys.argv[1:5]
SOURCE = '''
struct Pair { a:i64, b:i64 }
enum Choice { Value(i32), Empty }
fn main(){
    let outer=|x:i32| -> i32 {
        let middle=|y:i32| -> i32 {let inner=|z:i32| -> i32 x+y+z;return inner(4);};
        return middle(3);
    };print(outer(2));
    let x:i32=40;
    let branch=|a:i32| -> i32 {if a>0 {let x:i32=1;print(x);}return x;};print(branch(1));
    let siblings=|a:i32| -> i32 {
        let first=|x:i32| -> i32 x+1;
        let second=|b:i32| -> i32 x+b;
        return first(a)+second(2);
    };print(siblings(3));
    let shadow=|a:i32| -> i32 {let x=x+a;return x;};print(shadow(2));
    var big:i128=18446744073709551617;
    var pair=Pair{a:1099511627776,b:3};
    let snapshots=|a:i32| -> i128 {
        let inner=|b:i32| -> i128 big+(pair.a as i128)+(pair.b as i128);
        return inner(a);
    };
    big=0;pair.a=0;print(snapshots(0));
    let loop_body=|a:i32| -> i32 {var sum:i32=0;for i in 0..a {sum=sum+x;}return sum;};print(loop_body(2));
    let assigned=|a:i32| -> i32 {var sum:i32=a;sum=x;return sum;};print(assigned(2));
    let aggregate=|a:i32| -> i64 {let v=Pair{a:x as i64,b:2};return v.a;};print(aggregate(0));
    let enummed=|a:i32| -> i32 {
        let v=Choice::Value(x);
        match v { Choice::Value(x)=>{return x;}, Choice::Empty=>{return 0;} }
        return 0;
    };print(enummed(0));
    let matched=|a:i32| -> i32 {match a {0=>{return x;},_=>{return 2;}}return 0;};print(matched(0));
    var values:[i32;2]=0;let p:*i32=&values[0];let index:i32=1;
    let stores=|a:i32| -> i32 {p[index]=x;return a;};print(stores(3));print(values[1]);
    let loop_shadow=|a:i32| -> i32 {for x in 0..a {print(x);}return x;};print(loop_shadow(1));
}
'''
EXPECTED = '9\n1\n40\n46\n42\n18446745173221179396\n80\n40\n40\n40\n40\n3\n40\n0\n40\n'


def run(*args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory(prefix='capture-scopes-') as tmp:
    d = Path(tmp)
    source, ir = d / 'case.tv', d / 'case.ll'
    source.write_text(SOURCE)
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
    source.write_text('''fn main(){let x:i32=40;let f=|a:i32| -> i32 {
        var sum:i32=0;for i in 0..a {sum=sum+x;}return sum;};print(f(2));}''')
    assert run(tvc, '--eval', source) == '80\n'
    for count, kind in ((65, 'captures'), (513, 'bindings')):
        decls = ''.join(f'let v{i}:i32={i};' for i in range(count))
        expr = '+'.join(f'v{i}' for i in range(count))
        body = f'{decls}let f=|a:i32| -> i32 {expr};' if kind == 'captures' else f'let f=|a:i32| -> i32 {{{decls}return a;}};'
        source.write_text('fn main(){' + body + '}')
        p = subprocess.run([tvc, str(source), '-o', str(ir)], capture_output=True, text=True, timeout=60)
        assert p.returncode != 0 and 'closure ' + kind in p.stderr, p.stderr
print('capture scopes PASS: transitive/sibling/shadowed bindings, wide aggregate snapshots and capacity diagnostics; raw/O1/O3')
