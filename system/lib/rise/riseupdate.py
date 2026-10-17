"""Rise – aggiornamenti dal web: controllo, installazione (con rise-update come amministratore) e stato."""
import json
import os
import subprocess
import threading
import time

TOOL = '/usr/local/bin/rise-update'
STATUS = '/var/lib/rise-update/status.json'
_last = {'t': 0, 'r': None}
_lock = threading.Lock()


def _live():
    try:
        return ' boot=casper' in open('/proc/cmdline').read() or os.path.isdir('/cdrom/casper')
    except OSError:
        return False


def check(a=None):
    """C'è una versione nuova? (il risultato resta valido 10 minuti, salvo controllo forzato)"""
    with _lock:
        if _last['r'] and time.time() - _last['t'] < 600 and not (a or {}).get('force'):
            return _last['r']
    try:
        r = subprocess.run(['python3', TOOL, 'check'], capture_output=True, text=True, timeout=40)
        res = json.loads(r.stdout or '{}')
    except Exception as e:
        res = {'ok': False, 'message': 'Non riesco a controllare gli aggiornamenti (%s).' % e}
    res['live'] = _live()
    with _lock:
        _last.update(t=time.time(), r=res)
    return res


def status(_=None):
    try:
        st = json.load(open(STATUS))
    except (OSError, ValueError):
        st = {}
    running = False
    try:
        pid = int(open('/var/lib/rise-update/lock').read() or 0)
        os.kill(pid, 0)
        running = True
    except (OSError, ValueError):
        pass
    st['running'] = running
    return {'ok': True, 'status': st}


def apply(_=None):
    if status()['status'].get('running'):
        return {'ok': True}
    try:
        subprocess.Popen(['sudo', '-n', TOOL, 'apply'], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=open('/tmp/rise-aggiornamento.log', 'a'))
    except OSError as e:
        return {'ok': False, 'message': 'Non riesco ad avviare l’aggiornamento: %s' % e}
    with _lock:
        _last['r'] = None
    return {'ok': True}


def rollback(_=None):
    r = subprocess.run(['sudo', '-n', TOOL, 'rollback'], capture_output=True, text=True, timeout=300)
    return {'ok': r.returncode == 0, 'message': (r.stderr or '').strip()[-200:]}


def restart_ui(_=None):
    """Riavvia solo l'interfaccia (barra, desktop, finestre di Rise): l'aggiornamento entra in funzione
    senza riavviare il computer (dalla chiavetta, riavviare lo cancellerebbe)."""
    try:
        subprocess.Popen(['sh', '-c', 'sleep 1; pkill -f "python3 [^ ]*rise-shell$"; pkill -f risewake.py; sleep 1.5; '
                          'exec rise-shell >/tmp/rise-shell.log 2>&1'],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return {'ok': False, 'message': str(e)}
    return {'ok': True}


CMDS = {'ui.restart': restart_ui, 'update.check': check, 'update.status': status, 'update.apply': apply, 'update.rollback': rollback}
