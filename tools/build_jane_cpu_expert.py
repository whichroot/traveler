"""Build Jane's existing CPU benchmark against Traveler's SIMD expert adapter."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('jane', type=Path)
p.add_argument('out', type=Path)
p.add_argument('--tvc', default=str(root/'src/bootstrap/out/stage1'))
p.add_argument('--llc', default='llc')
p.add_argument('--opt', default='opt')
p.add_argument('--cc', default='clang')
p.add_argument('--cuda-lib', default='/run/opengl-driver/lib')
p.add_argument('--no-link', action='store_true')
a = p.parse_args()
jane = a.jane.resolve()
out = a.out.resolve()
out.mkdir(parents=True, exist_ok=True)


def run(*args):
    subprocess.run(list(map(str, args)), check=True)


with tempfile.TemporaryDirectory(prefix='traveler-jane-build-') as directory:
    temp = Path(directory)
    modules = {}

    def module(path):
        path = path.resolve()
        if path == jane/'src/kernels/cpu_expert.tv':
            return root/'examples/jane_cpu_expert.tv'
        if path in modules:
            return modules[path]
        destination = temp/f'module{len(modules)}.tv'
        modules[path] = destination

        def replace(match):
            name = match.group(1)
            if 'device/traveler/' in name:
                target = root/name.split('device/traveler/', 1)[1]
            else:
                target = module(path.parent/name)
            return f'import "{target}";'

        destination.write_text(re.sub(r'import\s+"([^"\n]+)"\s*;', replace, path.read_text()))
        return destination

    source = module(jane/'src/runtime/cpubench.tv')
    for name, profile, cpu in (('o1','o1',None), ('o3','o3','x86-64'), ('spr','o3','sapphirerapids')):
        ir = out/f'cpubench_simd.{name}.ll'
        obj = out/f'cpubench_simd.{name}.o'
        asm = out/f'cpubench_simd.{name}.s'
        exe = out/f'jane_cpubench_simd_{name}'
        flags = ('-mcpu',cpu) if cpu else ()
        run(a.tvc,source,'--emit','ir','--opt-level',profile,*flags,'-opt',a.opt,
            '-target','x86_64-linux-gnu','-o',ir)
        target = (f'-mcpu={cpu}',) if cpu else ()
        optimization = '-O2' if name == 'o1' else '-O3'
        run(a.llc,optimization,*target,'-filetype=obj',ir,'-o',obj)
        run(a.llc,optimization,*target,ir,'-o',asm)
        if not a.no_link:
            run(a.cc,'-no-pie','-pthread',obj,f'-L{a.cuda_lib}',f'-Wl,-rpath,{a.cuda_lib}','-lcuda','-o',exe)
        text = asm.read_text()
        counts = {op: len(re.findall(r'^\s*'+op+r'\b', text, re.M))
                  for op in ('vpdpbusd','vpdpbusds','vpmadd52luq','vpmadd52huq')}
        if name == 'spr':
            assert counts['vpdpbusd'] and counts['vpmadd52luq'] and counts['vpmadd52huq'], counts
            assert not counts['vpdpbusds'], counts
        print(json.dumps({'program': str(exe), 'linked': not a.no_link, **counts}))
