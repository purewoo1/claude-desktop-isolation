"""Collect bounded status records on a Linux cluster. No process arguments are queried."""
from datetime import datetime, timezone
import os
import re
import subprocess
import sys
from xml.etree import ElementTree


def integer_field(text):
    match = re.fullmatch(r'\s*(\d+)(?:\s+(?:MiB|%))?\s*', text)
    return int(match[1]) if match else None


def gpu_process_details(pids):
    if not pids:
        return {}
    # Request only PID, age and executable name; never read process arguments.
    result = subprocess.run(
        ['/usr/bin/ps', '-p', ','.join(map(str, sorted(pids))),
         '-o', 'pid=', '-o', 'etimes=', '-o', 'comm='],
        capture_output=True, text=True, errors='replace', timeout=2,
        env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
    )
    details = {}
    for row in result.stdout.splitlines():
        fields = row.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            details[int(fields[0])] = (int(fields[1]), os.path.basename(fields[2])[:128])
    return details


def gpu_status():
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
    data = subprocess.check_output(['/usr/bin/nvidia-smi', '-q', '-x'], env=env, timeout=5)
    gpus = ElementTree.fromstring(data).findall('gpu')
    index_rows = subprocess.check_output(
        ['/usr/bin/nvidia-smi', '--query-gpu=index,uuid', '--format=csv,noheader,nounits'],
        env=env, timeout=3, text=True,
    )
    indices = {}
    for row in index_rows.splitlines():
        index, delimiter, uuid = row.partition(',')
        if delimiter and index.strip().isdigit():
            indices[uuid.strip()] = int(index)
    pids = {pid for gpu in gpus for process in gpu.findall('processes/process_info')
            if (pid := integer_field(process.findtext('pid', ''))) is not None and pid > 0}
    try:
        details = gpu_process_details(pids)
    except (OSError, subprocess.SubprocessError):
        details = {}

    inventory = []
    for gpu in gpus:
        uuid = gpu.findtext('uuid', '').strip()
        process_node = gpu.find('processes')
        processes = []
        for process in gpu.findall('processes/process_info'):
            pid = integer_field(process.findtext('pid', ''))
            if pid is None or pid <= 0:
                continue
            age, name = details.get(pid, (None, None))
            if not name:
                driver_name = process.findtext('process_name', '').strip()
                if driver_name and driver_name not in ('N/A', '[Not Found]'):
                    name = os.path.basename(driver_name)[:128]
            kind = process.findtext('type', '').strip()
            processes.append({'pid': pid, 'name': name, 'type': kind if kind in ('C', 'G', 'C+G', 'M') else None,
                              'elapsed_seconds': age, 'memory_used_mib': integer_field(process.findtext('used_memory', ''))})
        inventory.append({
            'index': indices.get(uuid), 'uuid': uuid, 'name': gpu.findtext('product_name', '').strip(),
            'memory_total_mib': integer_field(gpu.findtext('fb_memory_usage/total', '')),
            'memory_used_mib': integer_field(gpu.findtext('fb_memory_usage/used', '')),
            'memory_free_mib': integer_field(gpu.findtext('fb_memory_usage/free', '')),
            'utilization_percent': integer_field(gpu.findtext('utilization/gpu_util', '')),
            'processes': processes if processes or (process_node is not None and not
                                                   (process_node.text or '').strip()) else None,
        })
    if not inventory:
        raise ValueError('No NVIDIA GPUs reported')
    return sorted(inventory, key=lambda gpu: gpu['index'] if gpu['index'] is not None else sys.maxsize)


def node_status(mounts):
    status = {'host': os.uname().nodename, 'sample_time': datetime.now(timezone.utc).isoformat()}
    try:
        status['gpus'] = gpu_status()
    except (OSError, ValueError, subprocess.SubprocessError, ElementTree.ParseError):
        status.update(gpus=None, gpu_error='NVIDIA status unavailable')
    memory = {}
    wanted = {'MemTotal': 'total_bytes', 'MemAvailable': 'available_bytes',
              'SwapTotal': 'swap_total_bytes', 'SwapFree': 'swap_free_bytes'}
    with open('/proc/meminfo', encoding='ascii') as stream:
        for row in stream:
            key, _, value = row.partition(':')
            if key in wanted:
                memory[wanted[key]] = int(value.split()[0]) * 1024
    status['memory'] = memory
    status['filesystems'] = []
    for mount in mounts:
        try:
            if os.path.islink(mount) or not os.path.ismount(mount):
                continue
            info = os.statvfs(mount)
            status['filesystems'].append({'mount': mount, 'total_bytes': info.f_blocks * info.f_frsize,
                                          'available_bytes': info.f_bavail * info.f_frsize})
        except OSError:
            status['filesystems'].append({'mount': mount, 'error': 'Filesystem status unavailable'})
    return status
