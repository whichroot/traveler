#!/usr/bin/env python3
"""Prepare a CUDA package from a closed source snapshot, with verified cache reuse."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import time

import cuda_package as package


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def execute(*args, cwd=None):
    return subprocess.run(list(map(str, args)), cwd=cwd, check=True, capture_output=True, text=True).stdout


def executable(name):
    path = shutil.which(str(name))
    package.require(path is not None, f'executable not found: {name}')
    return Path(path).resolve()


def inventory(compiler, root, source):
    paths = json.loads(execute(compiler, source, '--dependencies', cwd=root))
    package.require(type(paths) is list and paths, 'dependency query')
    result = {}
    for name in paths:
        package.require(type(name) is str and not Path(name).is_absolute(), 'relative imports required')
        path = (root/name).resolve()
        package.require(path.is_relative_to(root), 'dependency outside source root')
        relative = path.relative_to(root).as_posix()
        package.require(Path(os.path.abspath(root/name)) == path, 'symlink dependencies refused')
        data = path.read_bytes()
        package.require(len(data) < 16777216, 'source file limit')
        result[relative] = data
    return result


def validate_blob(blob, sm):
    package.require(blob[:8] == package.MAGIC and len(blob) >= 12, 'package prefix')
    size = struct.unpack('<I', blob[8:12])[0]
    package.require(0 < size <= package.MAX_HEADER, 'package header limit')
    header = json.loads(blob[12:12+size], object_pairs_hook=package.unique_object)
    package.require(set(header) == {'schema', 'abi', 'sm', 'ptx_major', 'ptx_minor', 'ptx_bytes', 'ptx_sha256', 'kernels'}, 'package fields')
    package.require(header['schema'] == 'traveler.cuda.package.v1' and type(header['abi']) is int and header['abi'] == 1, 'package ABI')
    ptx = blob[12+size:]
    package.require(type(header['sm']) is int and header['sm'] == sm, 'package target')
    package.require(0 < len(ptx) <= package.MAX_PTX and b'\0' not in ptx, 'PTX limit')
    package.require(type(header['ptx_bytes']) is int and header['ptx_bytes'] == len(ptx) and header['ptx_sha256'] == digest(ptx), 'PTX digest')
    target = re.search(rb'(?m)^\s*\.target sm_(\d+)\s*$', ptx)
    version = re.search(rb'(?m)^\s*\.version (\d+)\.(\d+)\s*$', ptx)
    package.require(target and int(target[1]) == sm and version, 'PTX identity')
    v = tuple(map(int, version.groups()))
    package.require(all(type(header[k]) is int for k in ('ptx_major', 'ptx_minor')) and v == (header['ptx_major'], header['ptx_minor']), 'PTX version')
    kernels = header['kernels']
    package.require(type(kernels) is list and 1 <= len(kernels) <= 64, 'kernel count')
    for kernel in kernels:
        package.validate_kernel(kernel)
        minimum_sm, minimum_ptx = package.kernel_requirements(kernel)
        if kernel['profile'] in ('cooperative-warp-v1', 'cooperative-atomic-v1') or any(
                'count_parameter' in v.get('footprint', {}) for v in kernel['parameters']):
            minimum_sm = max(minimum_sm, 70)
        package.require(sm >= minimum_sm and v >= minimum_ptx, 'target capabilities')
    symbols = [k['symbol'].encode() for k in kernels]
    package.require(len(set(symbols)) == len(symbols) and set(re.findall(rb'\.visible \.entry (\w+)\(', ptx)) == set(symbols), 'entry symbols')
    return header


def publish(path, data):
    fd, staged = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(staged, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(staged): os.unlink(staged)


def prepare(root, source, output, cache, compiler, llc, sm, toolchain_id):
    started = time.monotonic_ns()
    root = Path(root).resolve(); source = Path(source)
    package.require(not source.is_absolute() and '..' not in source.parts, 'source must be root-relative')
    package.require(package.integer(sm, 50, 999) and type(toolchain_id) is str and bool(toolchain_id), 'target/toolchain identity')
    compiler = executable(compiler); llc = executable(llc)
    files = inventory(compiler, root, source.as_posix())
    tools = {'compiler': digest(compiler.read_bytes()), 'llc': digest(llc.read_bytes()),
             'llc_version': execute(llc, '--version'), 'closure': toolchain_id,
             'packager': digest(Path(package.__file__).read_bytes()), 'preparer': digest(Path(__file__).read_bytes())}
    request = {'schema': 'traveler.cuda.prepare.v1', 'abi': 1, 'source': source.as_posix(),
               'files': {p: digest(b) for p, b in sorted(files.items())}, 'tools': tools,
               'options': {'sm': sm, 'target': 'nvptx64-nvidia-cuda', 'optimization': 'none'}}
    key = digest(encoded(request)); cache = Path(cache); output = Path(output)
    cache.mkdir(parents=True, exist_ok=True)
    entry = cache/(key+'.json'); status = 'miss'; blob = None
    if entry.exists():
        try:
            package.require(entry.stat().st_size <= 2*(package.MAX_PTX+package.MAX_HEADER)+1048576, 'cache entry limit')
            record = json.loads(entry.read_bytes(), object_pairs_hook=package.unique_object)
            package.require(set(record) == {'request', 'package', 'sha256', 'artifact'}, 'cache fields')
            package.require(encoded(record['request']) == encoded(request), 'cache request identity')
            candidate = base64.b64decode(record['package'], validate=True)
            package.require(record['sha256'] == digest(candidate), 'cache package digest')
            header = validate_blob(candidate, sm)
            package.require(record['artifact'] == digest(encoded({'request': key, 'package': header})), 'artifact identity')
            blob = candidate; status = 'hit'
        except (ValueError, TypeError, KeyError, OSError):
            status = 'rebuild-corrupt'
    discovered = time.monotonic_ns()
    if blob is None:
        with tempfile.TemporaryDirectory(prefix='traveler-prepare-') as temporary:
            snapshot = Path(temporary)/'source'; snapshot.mkdir()
            for relative, data in files.items():
                path = snapshot/relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            package.require(inventory(compiler, snapshot, source.as_posix()) == files, 'snapshot dependency mismatch')
            ir = Path(temporary)/'device.ll'; result = Path(temporary)/'device.tvcp'
            execute(compiler, source.as_posix(), '--emit-gpu-nvptx', '-o', ir, cwd=snapshot)
            package.build(ir, result, str(llc), sm)
            blob = result.read_bytes(); header = validate_blob(blob, sm)
        package.require(tools['compiler'] == digest(compiler.read_bytes()) and tools['llc'] == digest(llc.read_bytes()), 'tool changed during preparation')
        record = {'request': request, 'package': base64.b64encode(blob).decode(), 'sha256': digest(blob),
                  'artifact': digest(encoded({'request': key, 'package': header}))}
        publish(entry, encoded(record))
    publish(output, blob)
    finished = time.monotonic_ns()
    return {'schema': 'traveler.cuda.prepared.v1', 'key': key, 'artifact': record['artifact'],
            'package_sha256': digest(blob), 'cache': status, 'dependencies': len(files),
            'discovery_ns': discovered-started, 'prepare_publish_ns': finished-discovered}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source'); p.add_argument('--root', required=True); p.add_argument('-o', required=True)
    p.add_argument('--cache', required=True); p.add_argument('--compiler', required=True)
    p.add_argument('--llc', default='llc'); p.add_argument('--sm', type=int, required=True)
    p.add_argument('--toolchain-id', required=True, help='immutable identity of the toolchain dependency closure')
    a = p.parse_args()
    try:
        print(json.dumps(prepare(a.root, a.source, a.o, a.cache, a.compiler, a.llc, a.sm, a.toolchain_id), sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        p.exit(1, f'cuda prepare: {detail}\n')


if __name__ == '__main__': main()
