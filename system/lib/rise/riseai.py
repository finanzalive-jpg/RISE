"""Rise – Assistente: intelligenza artificiale che gira sul computer, senza internet.

Motore: llama.cpp (llama-server, dal pacchetto Ubuntu llama.cpp-tools, licenza MIT).
Modelli: Qwen3.5 (Alibaba, licenza Apache 2.0) in formato GGUF compresso a 4 bit, scaricati
da Hugging Face (unsloth/Qwen3.5-*-GGUF) solo quando l'utente lo chiede.

Il desktop parla direttamente con llama-server su http://127.0.0.1:8737 (API compatibile OpenAI).
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.request

import risestore

HOME = os.path.expanduser('~')
MODEL_DIR = os.path.join(HOME, '.local', 'share', 'rise', 'models')   # vedi _dir(): dalla chiavetta si salva lì


def _dir():
    global MODEL_DIR
    MODEL_DIR, _ = risestore.data_dir('models')
    return MODEL_DIR
PORT = 8737
HF = 'https://huggingface.co/{repo}/resolve/main/{file}'

# id: (nome mostrato, repository, file, dimensione indicativa in GB, RAM minima consigliata in GB)
MODELS = {
    '0.8b': ('Qwen3.5 0.8B · leggerissimo', 'unsloth/Qwen3.5-0.8B-GGUF', 'Qwen3.5-0.8B-Q4_K_M.gguf', 0.6, 3),
    '2b':   ('Qwen3.5 2B · equilibrato', 'unsloth/Qwen3.5-2B-GGUF', 'Qwen3.5-2B-Q4_K_M.gguf', 1.3, 5),
    '4b':   ('Qwen3.5 4B · più bravo', 'unsloth/Qwen3.5-4B-GGUF', 'Qwen3.5-4B-Q4_K_M.gguf', 2.7, 8),
    '9b':   ('Qwen3.5 9B · il migliore', 'unsloth/Qwen3.5-9B-GGUF', 'Qwen3.5-9B-Q4_K_M.gguf', 5.7, 14),
}

_dl = {}          # id -> {'done', 'total', 'error', 'running', 'cancel'}
_srv = {'proc': None, 'model': None}
_lock = threading.Lock()


def _mem_gb():
    try:
        for l in open('/proc/meminfo'):
            if l.startswith('MemTotal:'):
                return int(l.split()[1]) / 1048576
    except OSError:
        pass
    return 4


def _live():
    return os.path.exists('/run/casper') or os.path.exists('/cdrom/casper')


def _path(mid):
    return os.path.join(_dir(), MODELS[mid][2])


def _split_first(mid):
    """Modello diviso in pezzi (per le chiavette FAT32, che non accettano file oltre 4 GB)."""
    base = os.path.join(_dir(), MODELS[mid][2][:-5])
    for n in range(2, 6):
        p = '%s-00001-of-%05d.gguf' % (base, n)
        if os.path.exists(p):
            return p
    return None


def _model_file(mid):
    p = _path(mid)
    return p if os.path.exists(p) else _split_first(mid)


def _elsewhere(mid):
    """Il file del modello arrivato in altro modo: mandato con Rise Drop/LocalSend, su un'altra chiavetta, ecc."""
    import glob
    name = MODELS[mid][2].lower()
    places = [os.path.join(HOME, d) for d in ('Download', 'Downloads', 'Scaricati', 'Scrivania', 'Desktop', 'Documenti')]
    places += glob.glob('/media/*') + glob.glob('/media/*/*') + glob.glob('/mnt/*')
    for d in places:
        for sub in ('', 'rise-dati/models', 'models'):
            dd = os.path.join(d, sub)
            try:
                for f in os.listdir(dd):
                    if f.lower() == name:
                        p = os.path.join(dd, f)
                        if os.path.dirname(os.path.abspath(p)) != os.path.abspath(_dir()):
                            return p
            except OSError:
                pass
    return None


def _import(mid, src):
    """Copia il modello dove Rise lo tiene (sulla chiavetta, se si usa Rise dalla chiavetta)."""
    st = _dl[mid]
    dst = _path(mid)
    part = dst + '.part'
    try:
        total = os.path.getsize(src)
        st.update(done=0, total=total, phase='import')
        if risestore.too_big(_dir(), total):
            # chiavetta FAT32: si salva in pezzi da 3,5 GB che llama.cpp legge insieme
            if not _splitter():
                raise IOError('il file è più grande di 4 GB e non entra su una chiavetta FAT32')
            st['phase'] = 'split'
            dst0 = dst[:-5]
            for pre in (['sudo', '-n'], []):
                subprocess.run(pre + [_splitter(), '--split', '--split-max-size', '3500M', src, dst0],
                               capture_output=True, timeout=7200)
                if _split_first(mid):
                    break
            if not _split_first(mid):
                raise IOError('non riesco a salvarlo sulla chiavetta')
            st['done'] = total
            risestore.sync()
            return
        free = shutil.disk_usage(_dir()).free
        if free < total + 5e8:
            raise IOError('non c’è abbastanza spazio (servono %.1f GB, liberi %.1f GB)' % (total / 1e9, free / 1e9))
        with open(src, 'rb') as i, risestore.open_write(part) as o:
            while True:
                if st.get('cancel'):
                    raise InterruptedError('annullato')
                buf = i.read(4 << 20)
                if not buf:
                    break
                o.write(buf)
                st['done'] += len(buf)
        risestore.replace(part, dst)
        risestore.sync()
        if src.startswith(HOME + os.sep):        # era nella memoria del computer: si libera spazio
            try:
                os.remove(src)
            except OSError:
                pass
    except InterruptedError:
        risestore.remove(part)
        _skip.add(mid)                       # annullato: non lo si ricopia finché non si rimette la chiavetta
    except Exception as e:
        risestore.remove(part)
        _skip.add(mid)
        st['error'] = 'Non riesco a copiare il modello: %s' % e
    finally:
        st['running'] = False
        st.pop('phase', None)


_last_mount = [0]


def _mount_sticks():
    """Le chiavette inserite vengono aperte (in sola lettura) per cercare i modelli; al massimo ogni 10 s."""
    if time.time() - _last_mount[0] < 10:
        return
    _last_mount[0] = time.time()
    try:
        import risesys
        for d in risesys.fs_roots({}).get('drives', []):
            if d.get('removable') and not d.get('mount') and d.get('label', '').lower() not in ('writable', 'casper-rw', 'rise-dati'):
                risesys.fs_mount({'dev': d['dev']})
    except Exception:
        pass


_skip = set()
_skip_src = {}


def _check_imports():
    if not all(_model_file(m) for m in MODELS):
        _mount_sticks()
    if any((d or {}).get('running') for d in _dl.values()):
        return                               # una copia alla volta
    for mid in ('9b', '4b', '2b', '0.8b'):   # prima il modello più bravo che questo computer regge
        if _model_file(mid) or MODELS[mid][4] > _mem_gb() + 0.5:
            continue
        src = _elsewhere(mid)
        if not src:
            _skip.discard(mid)               # chiavetta tolta: alla prossima si riprova
            continue
        if mid in _skip:
            continue
        if src:
            with _lock:
                if (_dl.get(mid) or {}).get('running'):
                    continue
                _dl[mid] = {'running': True, 'done': 0, 'total': 0, 'error': None, 'cancel': False}
            threading.Thread(target=_import, args=(mid, src), daemon=True).start()
            return


def _splitter():
    for n in ('llama-gguf-split', '/opt/llama/llama-gguf-split', '/usr/libexec/llama.cpp/llama-gguf-split'):
        p = shutil.which(n) or (n if os.path.isfile(n) and os.access(n, os.X_OK) else None)
        if p:
            return p
    return None


def _engine():
    for name in ('llama-server', '/usr/libexec/llama.cpp/llama-server', '/usr/lib/llama.cpp/llama-server'):
        p = shutil.which(name) or (name if os.path.isfile(name) and os.access(name, os.X_OK) else None)
        if p:
            return p
    return None


def _physical_cores():
    """Core veri (senza hyper-threading): con più thread i processori vecchi vanno più piano."""
    try:
        cores = {}
        phys = core = None
        for l in open('/proc/cpuinfo'):
            if l.startswith('physical id'):
                phys = l.split(':')[1].strip()
            elif l.startswith('core id'):
                core = l.split(':')[1].strip()
                cores[(phys, core)] = 1
        n = len(cores)
    except OSError:
        n = 0
    return max(1, min(8, n or (os.cpu_count() or 2)))


def _discrete_gpu():
    import glob
    for d in glob.glob('/sys/class/drm/card*/device/vendor'):
        try:
            if open(d).read().strip() in ('0x10de', '0x1002'):      # NVIDIA, AMD
                return True
        except OSError:
            pass
    return False


def recommended():
    """Il modello giusto per questo computer (dalla chiavetta i file stanno in memoria: si sta più bassi)."""
    ram = _mem_gb() - (1.5 if _live() else 0)
    best = '0.8b'
    # senza una scheda video vera (solo grafica integrata) il 9B è troppo lento per parlarci: al massimo il 4B
    top = ('0.8b', '2b', '4b', '9b') if _discrete_gpu() else ('0.8b', '2b', '4b')
    for mid in top:
        if ram >= MODELS[mid][4] and (MODELS[mid][3] < 3.9 or not risestore.too_big(_dir(), 5e9) or _splitter()):
            best = mid
    return best


def status(_=None):
    _check_imports()
    models = []
    for mid, (name, repo, f, gb, ram) in MODELS.items():
        p = _path(mid)
        d = _dl.get(mid) or {}
        models.append({'id': mid, 'name': name, 'gb': gb, 'ram': ram,
                       'have': bool(_model_file(mid)), 'downloading': bool(d.get('running')),
                       'done': d.get('done', 0), 'total': d.get('total', 0), 'error': d.get('error'), 'phase': d.get('phase')})
    proc = _srv['proc']
    running = bool(proc and proc.poll() is None)
    return {'engine': bool(_engine()), 'models': models, 'recommended': recommended(),
            'ramGb': round(_mem_gb(), 1), 'live': _live(), 'running': running,
            'gpu': _gpu_ok(), 'onUsb': risestore.data_dir('models')[1], 'usbFree': risestore.usb_free_gb(),
            'model': _srv['model'] if running else None, 'port': PORT}


# ---------- scaricamento ----------
def _download(mid):
    name, repo, f, gb, ram = MODELS[mid]
    d = _dir()
    dst, part = _path(mid), _path(mid) + '.part'
    st = _dl[mid]
    try:
        have = os.path.getsize(part) if os.path.exists(part) else 0
        req = urllib.request.Request(HF.format(repo=repo, file=f), headers={'User-Agent': 'Rise/0.5'})
        if have:
            req.add_header('Range', 'bytes=%d-' % have)
        with urllib.request.urlopen(req, timeout=30) as r:
            if have and r.status != 206:      # il server non riprende: si ricomincia
                have = 0
            total = int(r.headers.get('Content-Length') or 0) + have
            if risestore.too_big(d, total):
                if not _splitter():
                    raise IOError('questo modello è più grande di 4 GB e non entra su una chiavetta FAT32')
                if have:                       # si ricomincia nella memoria del computer
                    risestore.remove(part)
                    raise IOError('riprova: lo scarico in un altro modo')
                return _download_split(mid, r, total, st)
            st.update(done=have, total=total)
            with risestore.open_write(part, append=bool(have)) as out:
                while True:
                    if st.get('cancel'):
                        raise InterruptedError('annullato')
                    buf = r.read(1 << 20)
                    if not buf:
                        break
                    out.write(buf)
                    st['done'] += len(buf)
        if st['total'] and st['done'] < st['total']:
            raise IOError('scaricamento interrotto')
        risestore.replace(part, dst)
        risestore.sync()
    except InterruptedError:
        st['error'] = None
        risestore.remove(part)
    except Exception as e:
        st['error'] = 'Non riesco a scaricare il modello. Controlla la connessione e riprova. (%s)' % e
    finally:
        st['running'] = False


def _download_split(mid, r, total, st):
    """FAT32: si scarica in memoria e si salva sulla chiavetta in pezzi da 3,5 GB (llama.cpp li legge insieme)."""
    import tempfile
    tmpd = tempfile.mkdtemp(prefix='rise-modello-', dir=os.path.join(HOME, '.cache') if os.path.isdir(os.path.join(HOME, '.cache')) else None)
    tmp = os.path.join(tmpd, MODELS[mid][2])
    try:
        if shutil.disk_usage(tmpd).free < total + 2e8:
            raise IOError('serve più memoria libera per preparare questo modello')
        st.update(done=0, total=total)
        with open(tmp, 'wb') as out:
            while True:
                if st.get('cancel'):
                    raise InterruptedError('annullato')
                buf = r.read(1 << 20)
                if not buf:
                    break
                out.write(buf)
                st['done'] += len(buf)
        if st['done'] < total:
            raise IOError('scaricamento interrotto')
        st['phase'] = 'split'
        dst = _path(mid)[:-5]
        rc = subprocess.run(['sudo', '-n', _splitter(), '--split', '--split-max-size', '3500M', tmp, dst],
                            capture_output=True, timeout=3600).returncode
        if rc != 0 or not _split_first(mid):
            rc = subprocess.run([_splitter(), '--split', '--split-max-size', '3500M', tmp, dst],
                                capture_output=True, timeout=3600).returncode
        if not _split_first(mid):
            raise IOError('non riesco a salvarlo sulla chiavetta')
        risestore.sync()
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
        st.pop('phase', None)


def download(a):
    mid = a.get('model')
    if mid not in MODELS:
        return {'ok': False, 'message': 'Modello sconosciuto.'}
    with _lock:
        if (_dl.get(mid) or {}).get('running'):
            return {'ok': True}
        free = shutil.disk_usage(_dir()).free / 1e9
        if free < MODELS[mid][3] + 0.3:
            return {'ok': False, 'message': 'Non c’è abbastanza spazio (servono circa %.1f GB).' % MODELS[mid][3]}
        _dl[mid] = {'running': True, 'done': 0, 'total': 0, 'error': None, 'cancel': False}
    threading.Thread(target=_download, args=(mid,), daemon=True).start()
    return {'ok': True}


def cancel(a):
    d = _dl.get(a.get('model'))
    if d:
        d['cancel'] = True
    return {'ok': True}


def delete(a):
    mid = a.get('model')
    if mid not in MODELS:
        return {'ok': False}
    if _srv['model'] == mid:
        stop()
    import glob
    for p in [_path(mid), _path(mid) + '.part'] + glob.glob(_path(mid)[:-5] + '-0000*-of-0000*.gguf'):
        risestore.remove(p)
    return {'ok': True}


# ---------- motore ----------
def _healthy(timeout=1.0):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/health' % PORT, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _gpu_ok():
    if os.environ.get('RISE_SAFE_GPU') == '1' or os.path.exists(os.path.expanduser('~/.config/rise/ai-cpu-only')):
        return False
    return bool(glob_any(['/usr/lib/*/ggml/*/libggml-vulkan*', '/usr/lib/*/ggml/*vulkan*', '/usr/lib/*/libggml-vulkan*', '/usr/lib/*/ggml*/libggml-vulkan*',
                          '/opt/llama/libggml-vulkan*']))


def glob_any(patterns):
    import glob
    return any(glob.glob(p) for p in patterns)


def start(a):
    mid = a.get('model') or recommended()
    if mid not in MODELS or not _model_file(mid):
        return {'ok': False, 'message': 'Il modello non è ancora stato scaricato.'}
    eng = _engine()
    if not eng:
        return {'ok': False, 'message': 'Il motore dell’Assistente (llama.cpp) non è installato.'}
    with _lock:
        p = _srv['proc']
        if p and p.poll() is None and _srv['model'] == mid:
            return {'ok': True, 'port': PORT}
        stop()
        threads = _physical_cores()
        # Una domanda alla volta (-np 1): i modelli Qwen3.5 hanno una memoria "ricorrente" che, con più domande
        # in parallelo sullo stesso motore, può produrre caratteri senza senso.
        # Pieno regime: ragionamento acceso, contesto ampio, impostazioni consigliate da Qwen
        # per la modalità "thinking" (temperatura 0.6, top-p 0.95, top-k 20).
        ctx = '32768' if mid in ('4b', '9b') else '16384'
        cmd = [eng, '-m', _model_file(mid), '--host', '127.0.0.1', '--port', str(PORT),
               '-c', ctx, '-t', str(threads), '--jinja',
               '--temp', '0.6', '--top-p', '0.95', '--top-k', '20', '--min-p', '0',
               '--presence-penalty', '1.5', '--no-webui', '-np', '1']
        # Scheda video: con i chip recenti (Intel Iris/UHD dal 2015, AMD, NVIDIA) llama.cpp usa Vulkan e
        # legge le domande molte volte più in fretta. Sulle schede Intel vecchie (RISE_SAFE_GPU) solo processore.
        if _gpu_ok():
            mode = _gpu_mode()
            if mode == 'half':     # metà strati sulla grafica, metà sul processore: grafica meno carica
                cmd += ['-ngl', str({'0.8b': 12, '2b': 14, '4b': 18, '9b': 16}.get(mid, 16))]
            else:
                cmd += ['-ngl', '99']
        log = open('/tmp/rise-assistente.log', 'w')
        env = dict(os.environ)
        env.pop('LD_PRELOAD', None)
        _srv['proc'] = subprocess.Popen(cmd, stdout=log, stderr=log, env=env, start_new_session=True,
                                        preexec_fn=lambda: os.nice(5))
        _srv['model'] = mid
    for _ in range(240):                     # caricare il modello può richiedere qualche decina di secondi
        if _srv['proc'].poll() is not None:
            tail = ''
            try:
                tail = open('/tmp/rise-assistente.log').read()[-300:]
            except OSError:
                pass
            if 'unknown argument' in tail or 'invalid argument' in tail or _gpu_ok():
                if _gpu_ok():
                    try:
                        os.makedirs(os.path.expanduser('~/.config/rise'), exist_ok=True)
                        open(os.path.expanduser('~/.config/rise/ai-cpu-only'), 'w').write('la scheda video non ha funzionato\n')
                    except OSError:
                        pass
                return _start_plain(eng, mid)
            last = tail.strip().splitlines()[-1] if tail.strip() else ''
            return {'ok': False, 'message': ('L’Assistente non è partito. ' + last).strip()}
        if _healthy():
            return {'ok': True, 'port': PORT, 'gpu': _gpu_ok()}
        time.sleep(0.5)
    return {'ok': False, 'message': 'L’Assistente ci mette troppo a partire.'}


def _start_plain(eng, mid):
    """Versioni di llama-server che non conoscono qualche opzione: si riparte con quelle essenziali."""
    cmd = [eng, '-m', _model_file(mid), '--host', '127.0.0.1', '--port', str(PORT), '-c', '8192', '--jinja',
           '-t', str(_physical_cores()), '-ngl', '0', '-np', '1']
    log = open('/tmp/rise-assistente.log', 'a')
    env = dict(os.environ)
    env.pop('LD_PRELOAD', None)
    _srv['proc'] = subprocess.Popen(cmd, stdout=log, stderr=log, env=env, start_new_session=True)
    for _ in range(240):
        if _srv['proc'].poll() is not None:
            return {'ok': False, 'message': 'L’Assistente non è partito.'}
        if _healthy():
            return {'ok': True, 'port': PORT}
        time.sleep(0.5)
    return {'ok': False, 'message': 'L’Assistente ci mette troppo a partire.'}


def stop(_=None):
    p = _srv['proc']
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _srv['proc'] = None
    _srv['model'] = None
    return {'ok': True}


def prepare_usb(_=None):
    return risestore.prepare_usb()


GPU_FILE = os.path.expanduser('~/.config/rise/ai-gpu-mode')


def _gpu_mode():
    """'full' tutta sulla grafica (più veloce), 'half' metà grafica e metà processore, 'cpu' solo processore."""
    if os.path.exists(os.path.expanduser('~/.config/rise/ai-cpu-only')):
        return 'cpu'
    try:
        m = open(GPU_FILE).read().strip()
        return m if m in ('full', 'half', 'cpu') else 'full'
    except OSError:
        return 'full'


def gpu_mode(a):
    """Dove lavora l'Assistente. Se lo schermo lampeggia mentre pensa: 'half' o 'cpu'.
    Cambiando si spegne il motore: riparte riaprendo l'Assistente."""
    cpu_flag = os.path.expanduser('~/.config/rise/ai-cpu-only')
    m = a.get('mode')
    if m is None and 'gpu' in a:
        m = 'full' if a['gpu'] else 'cpu'
    if m in ('full', 'half', 'cpu'):
        os.makedirs(os.path.dirname(GPU_FILE), exist_ok=True)
        with open(GPU_FILE, 'w') as f:
            f.write(m)
        if m == 'cpu':
            open(cpu_flag, 'w').close()
        else:
            try:
                os.remove(cpu_flag)
            except OSError:
                pass
        stop({})
    avail = bool(glob_any(['/usr/lib/*/ggml/*/libggml-vulkan*', '/usr/lib/*/ggml/*vulkan*', '/usr/lib/*/libggml-vulkan*',
                           '/usr/lib/*/ggml*/libggml-vulkan*', '/opt/llama/libggml-vulkan*']))
    return {'ok': True, 'mode': _gpu_mode(), 'gpu': _gpu_mode() != 'cpu', 'available': avail}


def diag(_=None):
    """Informazioni per capire perché l'Assistente è lento o non risponde (Impostazioni → Assistente → Diagnosi)."""
    def rd(f, n=4000):
        try:
            return open(f, errors='replace').read()[-n:]
        except OSError:
            return ''
    def sh(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=8).stdout
        except Exception as e:
            return str(e)
    p = _srv['proc']
    gpu = ''
    for d in sorted(__import__('glob').glob('/sys/class/drm/card*/device')):
        gpu += '%s %s\n' % (rd(d + '/vendor').strip(), rd(d + '/device').strip())
    return {'ok': True, 'cmdline': rd('/proc/cmdline').strip(), 'running': bool(p and p.poll() is None),
            'model': _srv.get('model'), 'mode': _gpu_mode(), 'vulkan': _gpu_ok(),
            'mem': sh(['free', '-h']), 'gpu': gpu, 'cores': _physical_cores(),
            'log': rd('/tmp/rise-assistente.log', 3000),
            'upd': rd('/var/lib/rise-update/status.json', 600) + '\n' + rd('/tmp/rise-aggiornamento.log', 600),
            'desk': sh(['sh', '-c', 'echo "sessione: $XDG_CURRENT_DESKTOP $XDG_SESSION_TYPE"; ps -eo pid,etime,args | grep -E "rise-shell|risewake|cinnamon --|nemo-desktop" | grep -v grep | cut -c1-120; '
                        'python3 -c "import Xlib" 2>&1 | tail -1; ls /var/lib/rise/ 2>&1; tail -5 /tmp/rise-cinnamon-setup.log 2>/dev/null; '
                        'echo "--- rise-shell:"; tail -25 /tmp/rise-shell.log 2>/dev/null; echo "--- X:"; tail -8 /tmp/rise-x.log 2>/dev/null']),
            'grub': sh(['sh', '-c', 'ls /etc/default/grub.d/ 2>&1; grep -c "enable_fbc=0" /boot/grub/grub.cfg 2>&1; ls -la /boot/grub/grub.cfg 2>&1']),
            'kernel': sh(['sh', '-c', 'journalctl -k -b --no-pager -p warning 2>/dev/null | grep -iE "i915|drm|gpu|oom|killed" | tail -15'])}


CMDS = {'ai.diag': diag, 'ai.prepareUsb': prepare_usb, 'ai.gpu': gpu_mode, 'ai.status': status, 'ai.download': download, 'ai.cancel': cancel, 'ai.delete': delete,
        'ai.start': start, 'ai.stop': stop}
