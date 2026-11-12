"""Daily KST refresh trigger while the API server process is running."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess
import sys
import threading

from facility_refresh_job import write_json, utc_now

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        raise ValueError('Refresh time must be HH:MM (KST)')
    return tuple(map(int, value.split(':')))


def next_run(now, at='00:00'):
    hour, minute = parse_time(at)
    now = now.astimezone(KST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def is_due(now, target):
    # Do not perform a stale midnight run after a long suspend or clock jump.
    return 0 <= (now - target).total_seconds() <= 300


class FacilityRefreshSchedule:
    def __init__(self, database, *, at='00:00', clock=None, launch=None):
        parse_time(at)
        self.database = Path(database).resolve()
        self.at = at
        self.clock = clock or (lambda: datetime.now(KST))
        self.launch = launch or self._launch
        self.stop_event = threading.Event()
        self.thread = None

    def _launch(self, target):
        options = {'cwd': str(ROOT), 'stdin': subprocess.DEVNULL,
                   'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if sys.platform == 'win32':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        # No shell and no credentials in the command line. A separate process
        # lets an already-started batch finish safely if the server shuts down.
        result = subprocess.run([sys.executable, str(ROOT / 'facility_refresh_job.py'),
                                 '--database', str(self.database), '--scheduled-date', target.date().isoformat()],
                                **options)
        return result.returncode

    def _loop(self):
        target = next_run(self.clock(), self.at)
        while not self.stop_event.is_set():
            now = self.clock()
            remaining = (target - now).total_seconds()
            if remaining > 0:
                self.stop_event.wait(min(60, remaining))
                continue
            if self.stop_event.is_set():
                return
            if is_due(now, target):
                try:
                    code = self.launch(target)
                    self._log('completed' if code == 0 else 'failed', target, code)
                except Exception:
                    self._log('launch_failed', target, 1)
            else:
                self._log('skipped_late', target, None)
            target = next_run(self.clock(), self.at)

    @staticmethod
    def _log(status, target, code):
        try:
            write_json(ROOT / '.cache/facilities/schedule-last.json', {
                'status': status, 'scheduled_for': target.isoformat(), 'finished_at': utc_now(),
                'exit_code': code})
        except OSError:
            pass  # Job logs and existing DB state remain independently inspectable.

    def start(self):
        if self.thread is not None:
            raise RuntimeError('Scheduler already started')
        self.thread = threading.Thread(target=self._loop, name='facility-refresh-schedule', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
