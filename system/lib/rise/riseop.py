"""Rise – Strumenti del tecnico: ponte verso rise-op (che gira come amministratore).

L'interfaccia chiama op.*; qui si inoltra a  sudo -n /usr/local/bin/rise-op <comando>  con i
parametri in JSON sullo standard input, e si restituisce la risposta.
"""
import json
import subprocess

TOOL = '/usr/local/bin/rise-op'


def _call(cmd, a=None, timeout=120):
    try:
        p = subprocess.run(['sudo', '-n', TOOL, cmd], input=json.dumps(a or {}),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'message': 'Il disco non risponde (tempo scaduto).'}
    except OSError as e:
        return {'ok': False, 'message': str(e)}
    out = (p.stdout or '').strip()
    if not out:
        tail = (p.stderr or '').strip().splitlines()[-1:] or ['']
        return {'ok': False, 'message': 'Strumento non disponibile: %s' % tail[0][-200:]}
    try:
        return json.loads(out)
    except ValueError:
        return {'ok': False, 'message': out[-300:]}


def _mk(cmd, timeout=120):
    return lambda a=None: _call(cmd, a, timeout)


CMDS = {
    'op.disks': _mk('disks'),
    'op.smart': _mk('smart', 60),
    'op.mount': _mk('mount', 90),
    'op.unmount': _mk('unmount', 90),
    'op.clone': _mk('clone'),
    'op.recover': _mk('recover'),
    'op.bootinfo': _mk('bootinfo', 90),
    'op.bootfix': _mk('bootfix', 360),
    'op.winUsers': _mk('winUsers', 90),
    'op.winClear': _mk('winClear', 90),
    'op.bitlocker': _mk('bitlocker', 120),
    'op.report': _mk('report', 90),
    'op.scan': _mk('scan'),
    'op.wipe': _mk('wipe'),
    'op.status': _mk('status', 20),
    'op.stop': _mk('stop', 20),
}
