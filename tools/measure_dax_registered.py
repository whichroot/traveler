"""Measure the native DAX registration probe with RAPL and per-DIMM temperatures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import threading
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('probe', type=Path)
p.add_argument('output', type=Path)
p.add_argument('--device', default='/dev/dax0.0')
p.add_argument('--seconds', type=int, default=20)
p.add_argument('--window-mib', type=int, default=16384)
p.add_argument('--workers', type=int, default=16)
p.add_argument('--modes', default='1,2,2,1')
p.add_argument('--smart', default='/tmp/nvdimm_smart')
p.add_argument('--cool-max', type=float, default=70)
p.add_argument('--cool-timeout', type=int, default=900)
a = p.parse_args()
domains = [x for x in Path('/sys/class/powercap').glob('intel-rapl:*') if (x/'energy_uj').exists()]
limits = {x.name: int((x/'max_energy_range_uj').read_text()) for x in domains}
names = {x.name: (x/'name').read_text().strip() for x in domains}


def energy():
    return {x.name: int((x/'energy_uj').read_text()) for x in domains}


def temperatures():
    text = subprocess.check_output([a.smart, '/dev/nmem0', '/dev/nmem1', '/dev/nmem2', '/dev/nmem3'], text=True)
    rows = []
    for line in text.splitlines():
        media = re.search(r'media_C ([\d.]+)', line)
        controller = re.search(r'ctrl_C ([\d.]+)', line)
        assert media and controller and 'status 0 ' in line, line
        rows.append({'dimm': line.split()[0], 'media_c': float(media[1]), 'controller_c': float(controller[1]), 'raw': line})
    assert len(rows) == 4, text
    return rows


results = {'host': platform.uname()._asdict(), 'probe_sha256': hashlib.sha256(a.probe.read_bytes()).hexdigest(),
           'config': {'seconds': a.seconds, 'window_mib': a.window_mib, 'workers': a.workers,
                      'cool_max_c': a.cool_max, 'burst': 7, 'record_bytes': 17547264, 'poll_us': 13},
           'energy_domains': names, 'runs': []}
for mode in map(int, a.modes.split(',')):
    assert mode in (1, 2)
    deadline = time.monotonic()+a.cool_timeout
    while True:
        initial = temperatures()
        hottest = max(x['media_c'] for x in initial)
        if hottest <= a.cool_max:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Cooling timed out at {hottest} C')
        print(f'Cooling: hottest PMem {hottest} C', flush=True)
        time.sleep(15)
    command = [str(a.probe.resolve()), a.device, str(mode), str(a.seconds), str(a.window_mib), str(a.workers)]
    row = {'mode': mode, 'initial_temperatures': initial, 'samples': [], 'stdout': []}
    active = threading.Event()
    readings = {}
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          env={**os.environ, 'TRAVELER_THREADS': '1'}) as child:
        def reader():
            for line in child.stdout:
                line = line.strip()
                row['stdout'].append(line)
                if line == 'benchmark_begin':
                    readings['begin'] = energy()
                    readings['begin_time'] = time.monotonic()
                    active.set()
                if line == 'benchmark_end':
                    readings['end'] = energy()
                    readings['end_time'] = time.monotonic()
                    active.clear()
        worker = threading.Thread(target=reader)
        worker.start()
        timeout = time.monotonic()+a.seconds+120
        while child.poll() is None:
            row['samples'].append({'time': time.monotonic(), 'benchmark': active.is_set(), 'temperatures': temperatures()})
            if time.monotonic() > timeout:
                child.kill()
                raise RuntimeError('Native probe timed out')
            time.sleep(2)
        worker.join()
        row['returncode'] = child.returncode
    row['final_temperatures'] = temperatures()
    lines = row['stdout']
    for key in ('registration_ns', 'verified_records', 'readonly_after_registration', 'records', 'bytes', 'wall_ns', 'cpu_ns'):
        if key in lines:
            row[key] = int(lines[lines.index(key)+1])
    if 'begin' in readings and 'end' in readings:
        row['energy_j'] = {key: ((readings['end'][key]-value) % limits[key])/1e6 for key,value in readings['begin'].items()}
        row['energy_interval_s'] = readings['end_time']-readings['begin_time']
    results['runs'].append(row)
    a.output.write_text(json.dumps(results, indent=2)+'\n')
    assert row['returncode'] == 0 and 'native_checked_registration_PASS' in lines, row
    summary = {'mode': mode, 'gbps': row['bytes']/row['wall_ns'], 'cpu_cores': row['cpu_ns']/row['wall_ns'],
               'energy_j_per_gb': {names[k]: v/(row['bytes']/1e9) for k,v in row.get('energy_j', {}).items()},
               'start_media_c': [x['media_c'] for x in initial],
               'end_media_c': [x['media_c'] for x in row['final_temperatures']]}
    print(json.dumps(summary), flush=True)
