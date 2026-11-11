"""Console-owned Windows API session: closing the window stops its child processes."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
SERVER = ROOT
LOGS = ROOT / '.cache' / 'route-startup'
NGROK = ROOT / '.cache' / 'ngrok' / 'ngrok.exe'


def own_process_tree():
    # Attach supervisor before spawning: all children inherit the job.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class Basic(ctypes.Structure):
        _fields_ = [('per_process', ctypes.c_longlong), ('per_job', ctypes.c_longlong),
                    ('flags', wintypes.DWORD), ('min_ws', ctypes.c_size_t),
                    ('max_ws', ctypes.c_size_t), ('active', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                    ('scheduling', wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                    ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                    ('peak_job', ctypes.c_size_t)]
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = Extended()
    limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    # Non-inheritable handle stays open until the supervisor exits, including
    # forced console closure; Windows then terminates its entire child tree.
    return job


def read_json(url):
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.load(response)


def main():
    for path in (SERVER / 'api_server.py', NGROK):
        if not path.is_file():
            raise RuntimeError(f'Missing file: {path}')
    for port in (8082, 8083, 4040):
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                raise RuntimeError(f'Port {port} already in use. Close the previous server/ngrok window first. No existing process was changed.')
    job = own_process_tree()
    LOGS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    children, files = [], []
    def launch(name, command, cwd):
        output = (LOGS / f'{name}-{stamp}.log').open('ab')
        files.append(output)
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(process)
        return process
    try:
        api = launch('api', [sys.executable, '-u', 'api_server.py', '--port', '8082', '--enable-tunnel', '--tunnel-port', '8083'], SERVER)
        for _ in range(30):
            if api.poll() is not None:
                raise RuntimeError('API exited. Check logs.')
            try:
                if read_json('http://127.0.0.1:8083/v1/health').get('ok'):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError('API startup timed out.')
        tunnel = launch('ngrok', [str(NGROK), 'http', '127.0.0.1:8083', '--log=stdout', '--log-format=json'], ROOT)
        base = None
        for _ in range(30):
            if tunnel.poll() is not None:
                raise RuntimeError('ngrok exited. Check logs.')
            try:
                for item in read_json('http://127.0.0.1:4040/api/tunnels').get('tunnels', []):
                    if item.get('proto') == 'https' and item.get('config', {}).get('addr') in ('http://127.0.0.1:8083', '127.0.0.1:8083'):
                        base = item['public_url']
                if base:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        if not base:
            raise RuntimeError('ngrok startup timed out.')
        (LOGS / 'current-api-url.txt').write_text(base + '\n', encoding='utf-8')
        print(f'API base URL: {base}\nRequest logs: http://127.0.0.1:8082\nLogs: {LOGS}', flush=True)
        print('KEEP THIS WINDOW OPEN. Closing it stops BOTH the API and ngrok.\nPress Ctrl+C to stop. The saved API token is unchanged.', flush=True)
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('A service exited. Stopping the remaining service; check logs.')
            time.sleep(.5)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for output in files:
            output.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nAPI and ngrok stopped.', flush=True)
    except Exception as error:
        print(f'Startup/session failed: {error}\nLogs: {LOGS}', flush=True)
        sys.exit(1)
