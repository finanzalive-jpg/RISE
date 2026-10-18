"""Rise – funzioni di sistema richiamate dal desktop (rete, audio, file, hardware…).
Ogni funzione riceve un dizionario di argomenti e restituisce dati serializzabili in JSON."""
import base64, json, os, pwd, re, subprocess, threading, time

HOME = os.path.expanduser('~')
USER = pwd.getpwuid(os.getuid()).pw_name


def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:  # comando mancante o scaduto
        return 1, '', str(e)


def root(cmd, timeout=20):
    """Esegue come amministratore (sudo senza password sulla chiavetta live)."""
    rc, out, err = run(cmd, timeout)
    if rc != 0:
        rc, out, err = run(['sudo', '-n'] + cmd, timeout)
    return rc, out, err


def nm_fields(line):
    # nmcli -t separa i campi con ":" e scrive i ":" interni come "\:"
    parts = re.split(r'(?<!\\):', line)
    return [p.replace('\\:', ':').replace('\\\\', '\\') for p in parts]


# ---------------- Rete ----------------
def net_status(_):
    rc, out, _ = run(['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device'])
    devs = []
    for l in out.splitlines():
        f = nm_fields(l)
        if len(f) >= 4:
            devs.append({'device': f[0], 'type': f[1], 'state': f[2], 'connection': f[3]})
    wifi = [d for d in devs if d['type'] == 'wifi']
    eth = [d for d in devs if d['type'] == 'ethernet']
    _, radio, _ = run(['nmcli', 'radio', 'wifi'])
    active = next((d for d in devs if d['type'] in ('wifi', 'ethernet') and d['state'].startswith('connected')), None)
    conn = 'none'
    if active:
        _, conn, _ = run(['nmcli', 'networking', 'connectivity', 'check'], timeout=12)
    return {'nm': rc == 0, 'wifiDevice': bool(wifi), 'wifiName': wifi[0]['device'] if wifi else '', 'wifiOn': radio.strip() == 'enabled',
            'ethernet': bool(eth), 'ethernetCable': any(d['state'] != 'unavailable' for d in eth),
            'active': active, 'connectivity': conn or 'unknown'}


def net_scan(_):
    rc, out, err = run(['nmcli', '-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list', '--rescan', 'yes'], timeout=30)
    nets = {}
    for l in out.splitlines():
        f = nm_fields(l)
        if len(f) < 4 or not f[1]:
            continue
        ssid = f[1]
        sig = int(f[2]) if f[2].isdigit() else 0
        sec = '' if f[3] in ('', '--') else f[3]
        cur = nets.get(ssid)
        if not cur or sig > cur['signal'] or f[0] == '*':
            nets[ssid] = {'ssid': ssid, 'signal': sig, 'secure': bool(sec), 'security': sec,
                          'inUse': f[0] == '*' or (cur or {}).get('inUse', False)}
    return sorted(nets.values(), key=lambda n: (not n['inUse'], -n['signal']))


def sudo(cmd, timeout=20):
    """Come root se possibile (sulla chiavetta live sudo non chiede password), altrimenti come utente."""
    rc, out, err = run(['sudo', '-n'] + cmd, timeout)
    if rc != 0 and err.startswith('sudo:'):
        rc, out, err = run(cmd, timeout)
    return rc, out, err


def net_connect(a):
    ssid, pw = a.get('ssid', ''), a.get('password', '')
    if not ssid:
        return {'ok': False, 'message': 'Rete non indicata.'}
    # toglie eventuali profili vecchi o rimasti a metà con lo stesso nome
    _, out, _ = run(['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'])
    for line in out.splitlines():
        f = nm_fields(line)
        if len(f) >= 2 and f[0] == ssid and 'wireless' in f[1]:
            sudo(['nmcli', 'connection', 'delete', 'id', ssid])
    # come amministratore: la password viene salvata dal sistema e non serve nessun "portachiavi"
    rc, out, err = sudo(['nmcli', 'device', 'wifi', 'connect', ssid] + (['password', pw] if pw else []), 60)
    msg = err or out
    if rc != 0 and pw and ('key-mgmt' in msg or 'Secrets were required' in msg or 'property is missing' in msg):
        # secondo tentativo con un profilo scritto a mano (WPA2/WPA3 personale)
        # (WPA2 prima, poi WPA3 per i router più recenti)
        for km in ('wpa-psk', 'sae'):
            sudo(['nmcli', 'connection', 'delete', 'id', ssid])
            rc2, out2, err2 = sudo(['nmcli', 'connection', 'add', 'type', 'wifi', 'ifname', '*', 'con-name', ssid,
                                    'ssid', ssid, 'wifi-sec.key-mgmt', km, 'wifi-sec.psk', pw,
                                    'connection.autoconnect', 'yes'])
            if rc2 != 0:
                continue
            rc, out, err = sudo(['nmcli', 'connection', 'up', 'id', ssid], 60)
            msg = err or out
            if rc == 0:
                break
        if rc != 0:
            sudo(['nmcli', 'connection', 'delete', 'id', ssid])
    if rc == 0:
        return {'ok': True}
    if 'Secrets were required' in msg or 'invalid' in msg.lower() and 'psk' in msg.lower():
        msg = ('La rete non ha accettato la password. Controllala con il pulsante "mostra": '
               'se contiene simboli, verifica in Impostazioni → Tastiera che la tastiera sia quella giusta.')
    elif 'No network with SSID' in msg:
        msg = 'La rete non è più raggiungibile. Avvicinati al router e riprova.'
    elif 'Not authorized' in msg or 'insufficient privileges' in msg.lower():
        msg = 'Rise non ha il permesso di cambiare la rete. Riavvia il computer e riprova.'
    elif 'timeout' in msg.lower() or 'timed out' in msg.lower():
        msg = 'Il router non risponde. Avvicinati al router e riprova.'
    else:
        msg = 'Non riesco a collegarmi: ' + msg
    if pw:  # la password non deve mai comparire nei messaggi
        msg = msg.replace(pw, '••••')
    return {'ok': False, 'message': msg}


def net_radio(a):
    rc, out, err = run(['nmcli', 'radio', 'wifi', 'on' if a.get('on') else 'off'])
    return {'ok': rc == 0, 'message': err}


def net_diag(_):
    steps = []
    st = net_status(None)
    # 1. scheda di rete
    if not st['nm']:
        root(['systemctl', 'restart', 'NetworkManager'])
        time.sleep(3)
        st = net_status(None)
    has = st['wifiDevice'] or st['ethernet']
    steps.append({'name': 'Scheda di rete del computer', 'ok': has,
                  'detail': ('Wi‑Fi' if st['wifiDevice'] else '') + (' e cavo' if st['wifiDevice'] and st['ethernet'] else ('cavo' if st['ethernet'] else '')) if has else 'non trovata: probabilmente manca il driver'})
    if not has:
        return {'steps': steps, 'ok': False, 'summary': 'Non trovo nessuna scheda di rete. Apri Sistema e hardware per vedere il dettaglio.'}
    # 2. collegamento
    fixed = ''
    if st['wifiDevice'] and not st['wifiOn'] and not st['active']:
        run(['nmcli', 'radio', 'wifi', 'on'])
        time.sleep(4)
        fixed = 'il Wi‑Fi era spento, l’ho acceso'
        st = net_status(None)
    if not st['active'] and st['wifiDevice']:
        run(['nmcli', 'device', 'connect', st['wifiName']], timeout=20)
        st = net_status(None)
    act = st['active']
    steps.append({'name': 'Collegamento alla rete', 'ok': bool(act),
                  'detail': (fixed + ' · ' if fixed else '') + (('Wi‑Fi ' + act['connection']) if act and act['type'] == 'wifi' else ('cavo di rete' if act else 'non collegato: scegli una rete qui sotto'))})
    if not act:
        return {'steps': steps, 'ok': False, 'summary': 'Il computer non è collegato a nessuna rete. Scegli la tua rete Wi‑Fi qui sotto o collega il cavo.'}
    # 3. router
    _, route, _ = run(['ip', 'route', 'show', 'default'])
    m = re.search(r'default via (\S+)', route)
    gw = m.group(1) if m else ''
    rc = run(['ping', '-c', '1', '-W', '2', gw])[0] if gw else 1
    steps.append({'name': 'Router', 'ok': rc == 0, 'detail': ('risponde (' + gw + ')') if rc == 0 else 'non risponde'})
    if rc != 0:
        root(['nmcli', 'device', 'reapply', act['device']])
        return {'steps': steps, 'ok': False, 'summary': 'Il router non risponde. Prova a spegnerlo e riaccenderlo, poi controlla di nuovo.'}
    # 4. internet
    rc = run(['ping', '-c', '1', '-W', '3', '1.1.1.1'])[0]
    if rc != 0 and st.get('connectivity') == 'full':
        rc = 0  # alcune reti bloccano il ping ma internet funziona
    steps.append({'name': 'Uscita verso internet', 'ok': rc == 0, 'detail': 'ok' if rc == 0 else 'bloccata'})
    if rc != 0:
        return {'steps': steps, 'ok': False, 'summary': 'Il router c’è ma internet no. Di solito è un problema della linea: controlla le luci del router o chiama il gestore.'}
    # 5. dns
    t0 = time.time()
    rc = run(['getent', 'hosts', 'ubuntu.com'], timeout=8)[0]
    ms = int((time.time() - t0) * 1000)
    steps.append({'name': 'Traduzione dei nomi dei siti (DNS)', 'ok': rc == 0, 'detail': f'ok · {ms} ms' if rc == 0 else 'non funziona'})
    if rc != 0:
        return {'steps': steps, 'ok': False, 'summary': 'Internet funziona ma i nomi dei siti non vengono trovati (DNS). Riavvia il router; se non basta, scrivimi.'}
    return {'steps': steps, 'ok': True, 'summary': 'Tutto a posto: internet funziona.' + (' (' + fixed + ')' if fixed else '')}


# ---------------- Audio e luminosità ----------------
def audio_get(_):
    rc, out, _ = run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'])
    if rc != 0:
        return {'available': False}
    m = re.search(r'([\d.]+)', out)
    vol = round(float(m.group(1)) * 100) if m else 0
    _, info, _ = run(['wpctl', 'inspect', '@DEFAULT_AUDIO_SINK@'])
    d = re.search(r'node\.description = "([^"]+)"', info)
    return {'available': True, 'volume': vol, 'muted': 'MUTED' in out, 'name': d.group(1) if d else ''}


def audio_set(a):
    v = max(0, min(100, int(a.get('volume', 50))))
    run(['wpctl', 'set-volume', '-l', '1.0', '@DEFAULT_AUDIO_SINK@', f'{v / 100:.2f}'])
    run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '1' if v == 0 else '0'])
    return {'ok': True}


def bright_get(_):
    rc, out, _ = run(['brightnessctl', '-m', '-c', 'backlight'])
    if rc != 0 or not out:
        return {'available': False}
    f = out.split(',')
    return {'available': True, 'percent': int(f[3].rstrip('%')) if len(f) > 3 else 100}


def bright_set(a):
    p = max(5, min(100, int(a.get('percent', 100))))
    rc = root(['brightnessctl', '-q', '-c', 'backlight', 'set', f'{p}%'])[0]
    return {'ok': rc == 0}


def power(a):
    act = {'reboot': 'reboot', 'suspend': 'suspend'}.get(a.get('action'), 'poweroff')
    run(['sync'])
    rc, _, err = root(['systemctl', act])
    return {'ok': rc == 0, 'message': err}


# ---------------- Sistema e hardware ----------------
def sys_info(_):
    mem = {}
    for l in open('/proc/meminfo'):
        k, v = l.split(':')
        mem[k] = int(v.split()[0]) * 1024
    cpu = ''
    for l in open('/proc/cpuinfo'):
        if l.startswith('model name'):
            cpu = l.split(':', 1)[1].strip()
            break
    cores = os.cpu_count()
    _, boot, _ = run(['systemd-analyze'])
    m = re.search(r'graphical\.target reached after ([\d.]+)(ms|s|min)', boot) or re.search(r'=\s*([\d.]+)(ms|s|min)', boot)
    boot_s = None
    if m:
        boot_s = float(m.group(1)) / (1000 if m.group(2) == 'ms' else 1) * (60 if m.group(2) == 'min' else 1)
    hw = []
    _, out, _ = run(['lspci', '-vmmk'])
    for rec in out.split('\n\n'):
        r = {}
        for l in rec.splitlines():
            if ':' in l:
                k, v = l.split(':', 1)
                r[k.strip()] = v.strip()
        cls = r.get('Class', '')
        kind = None
        if cls in ('VGA compatible controller', '3D controller', 'Display controller'):
            kind = 'video'
        elif cls == 'Network controller':
            kind = 'wifi'
        elif cls == 'Ethernet controller':
            kind = 'ethernet'
        elif cls in ('Audio device', 'Multimedia audio controller'):
            kind = 'audio'
        if kind:
            hw.append({'kind': kind, 'name': (r.get('Vendor', '') + ' ' + r.get('Device', '')).strip(),
                       'driver': r.get('Driver', '')})
    _, usb, _ = run(['lsusb'])
    for l in usb.splitlines():
        name = l.split(' ', 6)[-1] if l.count(' ') >= 6 else l
        low = name.lower()
        kind = 'webcam' if ('camera' in low or 'webcam' in low) else 'printer' if 'printer' in low or 'deskjet' in low or 'laserjet' in low else 'bluetooth' if 'bluetooth' in low else None
        if kind:
            hw.append({'kind': kind, 'name': name, 'driver': 'usb'})
    bat = None
    for b in ('BAT0', 'BAT1'):
        p = f'/sys/class/power_supply/{b}/capacity'
        if os.path.exists(p):
            st = open(f'/sys/class/power_supply/{b}/status').read().strip()
            bat = {'percent': int(open(p).read()), 'status': st}
            break
    st = os.statvfs('/')
    return {'memTotal': mem.get('MemTotal', 0), 'memUsed': mem.get('MemTotal', 0) - mem.get('MemAvailable', 0),
            'cpu': cpu, 'cores': cores, 'kernel': os.uname().release, 'boot': boot_s, 'hardware': hw,
            'battery': bat, 'live': os.path.exists('/run/casper') or os.path.exists('/cdrom/casper'),
            'diskFree': st.f_bavail * st.f_frsize}


# ---------------- File e dischi ----------------
def fs_roots(_):
    rc, out, _ = run(['lsblk', '-J', '-b', '-o', 'NAME,PATH,LABEL,SIZE,FSTYPE,MOUNTPOINTS,TYPE,RM'])
    drives = []
    try:
        def walk(nodes):
            for n in nodes:
                if n.get('type') == 'part' or (n.get('type') == 'disk' and n.get('fstype')):
                    fs = n.get('fstype') or ''
                    mps = [m for m in (n.get('mountpoints') or []) if m]
                    live = any(m.startswith(('/cdrom', '/rofs', '/run/live', '/isodevice')) for m in mps)
                    if fs in ('ntfs', 'vfat', 'exfat', 'ext4', 'btrfs', 'xfs') and not live and int(n.get('size') or 0) > 200 * 1024 * 1024:
                        drives.append({'dev': n['path'], 'label': n.get('label') or '', 'size': int(n.get('size') or 0),
                                       'fs': fs, 'removable': bool(n.get('rm')), 'mount': mps[0] if mps else ''})
                walk(n.get('children') or [])
        walk(json.loads(out).get('blockdevices', []))
    except Exception:
        pass
    return {'home': HOME, 'drives': drives}


def fs_mount(a):
    dev = a.get('dev', '')
    if not re.match(r'^/dev/[\w-]+$', dev):
        return {'ok': False, 'message': 'disco non valido'}
    rc, out, _ = run(['lsblk', '-no', 'MOUNTPOINTS,FSTYPE,LABEL', dev])
    first = out.splitlines()[0] if out else ''
    for m in first.split():
        if m.startswith('/'):
            return {'ok': True, 'mount': m}
    _, fs, _ = run(['lsblk', '-no', 'FSTYPE', dev])
    _, label, _ = run(['lsblk', '-no', 'LABEL', dev])
    name = re.sub(r'[^\w.-]', '_', label.strip() or os.path.basename(dev))
    mp = f'/media/{USER}/{name}'
    root(['mkdir', '-p', mp])
    uid, gid = os.getuid(), os.getgid()
    fs = fs.strip()
    if fs == 'ntfs':
        rc, _, err = root(['mount', '-t', 'ntfs3', '-o', f'ro,uid={uid},gid={gid}', dev, mp])
    elif fs in ('vfat', 'exfat'):
        rc, _, err = root(['mount', '-o', f'ro,uid={uid},gid={gid}', dev, mp])
    else:
        rc, _, err = root(['mount', '-o', 'ro', dev, mp])
    if rc != 0:
        msg = err
        if 'hibernat' in err.lower() or 'dirty' in err.lower() or 'unclean' in err.lower():
            msg = 'Windows non è stato spento del tutto (avvio rapido attivo). Da Windows tieni premuto Maiusc mentre premi Arresta, poi riprova.'
        return {'ok': False, 'message': msg}
    return {'ok': True, 'mount': mp}


def fs_list(a):
    path = os.path.abspath(os.path.expanduser(a.get('path') or HOME))
    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith('.') or e.name in ('$RECYCLE.BIN', 'System Volume Information', 'lost+found'):
                    continue
                try:
                    st = e.stat()
                    if e.is_dir():
                        dirs.append({'name': e.name, 'mtime': int(st.st_mtime * 1000)})
                    else:
                        files.append({'name': e.name, 'size': st.st_size, 'mtime': int(st.st_mtime * 1000)})
                except OSError:
                    pass
                if len(dirs) + len(files) > 5000:
                    break
    except PermissionError:
        return {'ok': False, 'message': 'Non hai il permesso di aprire questa cartella.'}
    except FileNotFoundError:
        return {'ok': False, 'message': 'La cartella non esiste più.'}
    return {'ok': True, 'path': path, 'dirs': dirs, 'files': files}


def fs_read(a):
    path = os.path.abspath(a.get('path', ''))
    size = os.path.getsize(path)
    if size > 80 * 1024 * 1024:
        return {'ok': False, 'message': 'File troppo grande per l’anteprima (oltre 80 MB).'}
    with open(path, 'rb') as f:
        return {'ok': True, 'data': base64.b64encode(f.read()).decode()}



def sys_boot(_):
    """Secondi dall'avvio del kernel al desktop (systemd-analyze), quando l'avvio è finito."""
    rc, out, _ = run(['systemd-analyze'], timeout=8)
    if rc != 0 or 'graphical.target' not in out and '=' not in out:
        return {'seconds': None}
    tot = 0.0
    m = re.search(r'Startup finished in (.*?)(?:\n|$)', out)
    if not m:
        return {'seconds': None}
    for val, unit in re.findall(r'([\d.]+)(ms|min|s)\b', m.group(1).split('=')[0]):
        if 'firmware' in m.group(1) or 'loader' in m.group(1):
            pass
        tot += float(val) / 1000 if unit == 'ms' else float(val) * (60 if unit == 'min' else 1)
    # escludi firmware e boot loader: contano solo kernel, avvio iniziale e sistema
    for part in re.findall(r'([\d.]+)(ms|min|s) \((firmware|loader)\)', m.group(1)):
        v = float(part[0]) / 1000 if part[1] == 'ms' else float(part[0]) * (60 if part[1] == 'min' else 1)
        tot -= v
    return {'seconds': round(tot, 1) if tot > 0 else None}


# ---------------- Gestione file ----------------
def _safe(p):
    p = os.path.abspath(os.path.expanduser(p or ''))
    if not p or p == '/':
        raise ValueError('percorso non valido')
    return p


def _free_name(dest_dir, name):
    base, ext = os.path.splitext(name)
    cand, n = name, 2
    while os.path.exists(os.path.join(dest_dir, cand)):
        cand = f'{base} ({n}){ext}'
        n += 1
    return cand


def fs_mkdir(a):
    d = _safe(a.get('path'))
    name = (a.get('name') or 'Nuova cartella').strip().replace('/', '-')
    name = _free_name(d, name)
    os.makedirs(os.path.join(d, name))
    return {'ok': True, 'name': name}


def fs_rename(a):
    src = _safe(a.get('path'))
    new = (a.get('name') or '').strip().replace('/', '-')
    if not new:
        return {'ok': False, 'message': 'Scrivi un nome.'}
    dst = os.path.join(os.path.dirname(src), new)
    if os.path.exists(dst):
        return {'ok': False, 'message': 'Esiste già un elemento con questo nome.'}
    os.rename(src, dst)
    return {'ok': True}


def _transfer(a, move):
    import shutil
    dest = _safe(a.get('dest'))
    done = 0
    for src in a.get('paths') or []:
        src = _safe(src)
        if os.path.dirname(src) == dest and move:
            continue
        if dest == src or dest.startswith(src + os.sep):
            return {'ok': False, 'message': 'Non puoi copiare una cartella dentro sé stessa.'}
        name = _free_name(dest, os.path.basename(src))
        target = os.path.join(dest, name)
        if move:
            shutil.move(src, target)
        elif os.path.isdir(src):
            shutil.copytree(src, target, symlinks=True)
        else:
            shutil.copy2(src, target)
        done += 1
    return {'ok': True, 'count': done}


def fs_copy(a):
    return _transfer(a, False)


def fs_move(a):
    return _transfer(a, True)


def fs_trash(a):
    bad = []
    for p in a.get('paths') or []:
        rc, _, err = run(['gio', 'trash', '--', _safe(p)])
        if rc != 0:
            bad.append(os.path.basename(p))
    return {'ok': not bad, 'message': ('Non riesco a spostare nel cestino: ' + ', '.join(bad)) if bad else ''}


TRASH = os.path.join(HOME, '.local/share/Trash')


def fs_trash_list(_):
    files = []
    fdir, idir = os.path.join(TRASH, 'files'), os.path.join(TRASH, 'info')
    if os.path.isdir(fdir):
        for n in os.listdir(fdir):
            p = os.path.join(fdir, n)
            orig, when = '', 0
            try:
                for l in open(os.path.join(idir, n + '.trashinfo')):
                    if l.startswith('Path='):
                        from urllib.parse import unquote
                        orig = unquote(l[5:].strip())
                    if l.startswith('DeletionDate='):
                        when = int(time.mktime(time.strptime(l[13:].strip(), '%Y-%m-%dT%H:%M:%S')) * 1000)
            except Exception:
                pass
            st = os.lstat(p)
            files.append({'name': n, 'dir': os.path.isdir(p), 'size': st.st_size, 'mtime': when or int(st.st_mtime * 1000), 'orig': orig})
    return {'ok': True, 'files': files}


def fs_trash_restore(a):
    import shutil
    out = []
    for n in a.get('names') or []:
        n = os.path.basename(n)
        info = os.path.join(TRASH, 'info', n + '.trashinfo')
        orig = ''
        try:
            from urllib.parse import unquote
            for l in open(info):
                if l.startswith('Path='):
                    orig = unquote(l[5:].strip())
        except Exception:
            pass
        if not orig:
            continue
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        dst = os.path.join(os.path.dirname(orig), _free_name(os.path.dirname(orig), os.path.basename(orig)))
        shutil.move(os.path.join(TRASH, 'files', n), dst)
        try:
            os.remove(info)
        except OSError:
            pass
        out.append(dst)
    return {'ok': True, 'restored': out}


def fs_trash_empty(_):
    rc, _, err = run(['gio', 'trash', '--empty'])
    return {'ok': rc == 0, 'message': err}


def fs_search(a):
    q = (a.get('q') or '').strip()
    if len(q) < 2:
        return {'ok': True, 'results': []}
    roots = [HOME, '/media', '/mnt']
    res = []
    dbs = [d for d in [INDEX_DB] + [INDEX_DB + '.%d' % i for i in (1, 2)] if os.path.exists(d)]
    rc, out = 1, ''
    if dbs:
        rc, out, _ = run(['plocate', '-i', '-l', '400', '-d', ':'.join(dbs), '--', q], timeout=10)
    if rc == 0 or out:
        for p in out.splitlines():
            if not any(p == r or p.startswith(r + '/') for r in roots):
                continue
            rel = p[len(HOME):] if p.startswith(HOME) else p
            if '/.' in rel or q.lower() not in os.path.basename(p).lower():
                continue
            res.append(p)
            if len(res) >= 40:
                break
    if not res:  # indice non ancora pronto: cerca direttamente nella cartella personale
        for dp, dns, fns in os.walk(HOME):
            dns[:] = [d for d in dns if not d.startswith('.')]
            for n in dns + fns:
                if q.lower() in n.lower():
                    res.append(os.path.join(dp, n))
            if len(res) >= 40 or dp.count('/') - HOME.count('/') > 6:
                break
    items = []
    for p in res[:40]:
        try:
            st = os.stat(p)
            items.append({'path': p, 'name': os.path.basename(p), 'dir': os.path.isdir(p), 'size': st.st_size, 'mtime': int(st.st_mtime * 1000)})
        except OSError:
            pass
    return {'ok': True, 'results': items}


INDEX_DB = os.path.join(HOME, '.cache', 'rise', 'file.db')


def index_update(_):
    # Indicizza SOLO le cartelle dell'utente (non tutto il sistema): leggero anche dalla chiavetta.
    os.makedirs(os.path.dirname(INDEX_DB), exist_ok=True)
    rc = 0
    for i, r in enumerate([HOME] + [d for d in ('/media', '/mnt') if os.path.isdir(d) and os.listdir(d)]):
        db = INDEX_DB if i == 0 else INDEX_DB + '.%d' % i
        rc |= run(['nice', '-n', '19', 'ionice', '-c3', 'updatedb', '-l', '0', '-U', r, '-o', db,
                   '--prunenames', '.git .cache node_modules __pycache__ .local'], timeout=300)[0]
    return {'ok': rc == 0}


# ---------------- Rete: PC vicini e cartelle condivise ----------------
def net_hosts(_):
    hosts = {}
    rc, out, _ = run(['avahi-browse', '-t', '-r', '-p', '_smb._tcp'], timeout=8)
    for l in out.splitlines():
        f = l.split(';')
        if len(f) > 7 and f[0] == '=' and f[2] in ('IPv4', 'IPv6'):
            name = f[3].replace('\\032', ' ')
            if f[2] == 'IPv4' or name not in hosts:
                hosts[name] = {'name': name, 'host': f[6].rstrip('.'), 'address': f[7]}
    rc, out, _ = run(['nmblookup', '-S', '*'], timeout=6)
    cur = None
    for l in out.splitlines():
        m = re.match(r'^(\d+\.\d+\.\d+\.\d+) \*<00>', l)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r'^\s+(\S+)\s+<00> -\s+(?!<GROUP>)', l)
        if m and cur and not any(h['address'] == cur for h in hosts.values()):
            hosts[m.group(1)] = {'name': m.group(1), 'host': m.group(1), 'address': cur}
            cur = None
    me = os.uname().nodename.lower()
    return sorted([h for h in hosts.values() if h['name'].lower() != me], key=lambda h: h['name'].lower())


def _smb_auth(a):
    u, p = a.get('user') or '', a.get('password') or ''
    return ['-U', f'{u}%{p}'] if u else ['-N']


def net_shares(a):
    host = (a.get('address') or a.get('host') or '').strip().strip('\\/')
    if not re.match(r'^[\w.\-:]+$', host):
        return {'ok': False, 'message': 'Indirizzo non valido.'}
    rc, out, err = run(['smbclient', '-g', '-L', '//' + host] + _smb_auth(a), timeout=15)
    shares = []
    for l in out.splitlines():
        f = l.split('|')
        if len(f) >= 2 and f[0] == 'Disk' and not f[1].endswith('$'):
            shares.append({'name': f[1], 'comment': f[2] if len(f) > 2 else ''})
    if rc != 0 and not shares:
        msg = err or out
        if 'LOGON_FAILURE' in msg or 'ACCESS_DENIED' in msg:
            return {'ok': False, 'auth': True, 'message': 'Serve nome utente e password di quel computer.'}
        return {'ok': False, 'message': 'Non riesco a raggiungere questo computer. Controlla che sia acceso e sulla stessa rete.'}
    return {'ok': True, 'shares': shares}


def net_mount(a):
    host = (a.get('address') or a.get('host') or '').strip().strip('\\/')
    share = (a.get('share') or '').strip('/')
    if not re.match(r'^[\w.\-:]+$', host) or not share or '/' in share:
        return {'ok': False, 'message': 'Indirizzo non valido.'}
    mp = f'/media/{USER}/rete/{re.sub(r"[^\w.-]", "_", host)}-{re.sub(r"[^\w.-]", "_", share)}'
    if os.path.ismount(mp):
        return {'ok': True, 'mount': mp}
    root(['mkdir', '-p', mp])
    uid, gid = os.getuid(), os.getgid()
    base = f'uid={uid},gid={gid},file_mode=0664,dir_mode=0775,iocharset=utf8'
    cred = f",username={a['user']},password={a.get('password', '')}" if a.get('user') else ',guest'
    err = ''
    for vers in ('3.1.1', '3.0', '2.1', '1.0'):
        rc, _, err = root(['mount', '-t', 'cifs', f'//{host}/{share}', mp, '-o', base + cred + f',vers={vers}'], timeout=20)
        if rc == 0:
            return {'ok': True, 'mount': mp}
    if 'Permission denied' in err or 'error(13)' in err:
        return {'ok': False, 'auth': True, 'message': 'Nome utente o password non corretti.'}
    return {'ok': False, 'message': 'Non riesco ad aprire la cartella condivisa.'}


SHARES_CONF = '/etc/samba/rise-shares.conf'


def _shares_read():
    out, cur = [], None
    try:
        for l in open(SHARES_CONF):
            l = l.strip()
            m = re.match(r'^\[(.+)\]$', l)
            if m:
                cur = {'name': m.group(1), 'path': '', 'writable': False}
                out.append(cur)
            elif cur and '=' in l:
                k, v = [x.strip() for x in l.split('=', 1)]
                if k == 'path':
                    cur['path'] = v
                if k == 'read only':
                    cur['writable'] = v.lower() == 'no'
    except FileNotFoundError:
        pass
    return out


def _shares_write(lst):
    txt = '# Cartelle condivise da Rise (generato automaticamente)\n'
    for s in lst:
        txt += (f"\n[{s['name']}]\n   path = {s['path']}\n   read only = {'no' if s['writable'] else 'yes'}\n"
                f"   guest ok = yes\n   browseable = yes\n   force user = {USER}\n")
    p = subprocess.run(['sudo', '-n', 'tee', SHARES_CONF], input=txt, text=True, capture_output=True)
    root(['smbcontrol', 'smbd', 'reload-config'])
    return p.returncode == 0


def share_list(_):
    return {'ok': True, 'shares': _shares_read(), 'host': os.uname().nodename}


def share_add(a):
    path = _safe(a.get('path'))
    lst = [s for s in _shares_read() if s['path'] != path]
    name = re.sub(r'[^\w\- ]', '', os.path.basename(path))[:40] or 'Cartella'
    names = {s['name'].lower() for s in lst}
    base, n = name, 2
    while name.lower() in names:
        name = f'{base} {n}'
        n += 1
    lst.append({'name': name, 'path': path, 'writable': bool(a.get('writable'))})
    ok = _shares_write(lst)
    return {'ok': ok, 'name': name, 'host': os.uname().nodename}


def share_remove(a):
    path = _safe(a.get('path'))
    ok = _shares_write([s for s in _shares_read() if s['path'] != path])
    return {'ok': ok}


# ---------------- Tastiera ----------------
KBD = {'it': ('it', ''), 'us': ('us', ''), 'gb': ('gb', ''), 'de': ('de', ''), 'fr': ('fr', ''), 'es': ('es', ''),
       'ch': ('ch', ''), 'ch(fr)': ('ch', 'fr'), 'pt': ('pt', '')}


def kbd_set(a):
    lay = a.get('layout', 'it')
    if lay not in KBD:
        return {'ok': False, 'message': 'Tastiera non conosciuta.'}
    layout, variant = KBD[lay]
    d = os.path.expanduser('~/.config/labwc')
    os.makedirs(d, exist_ok=True)
    lines = []
    try:
        for l in open('/etc/xdg/labwc/environment'):
            if not l.startswith('XKB_DEFAULT_'):
                lines.append(l.rstrip('\n'))
    except OSError:
        pass
    lines.append('XKB_DEFAULT_LAYOUT=' + layout)
    if variant:
        lines.append('XKB_DEFAULT_VARIANT=' + variant)
    with open(os.path.join(d, 'environment'), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    # labwc rilegge la configurazione e cambia la tastiera subito, senza riavviare
    rc = run(['labwc', '--reconfigure'])[0]
    return {'ok': rc == 0}


# ---------------- Programmi (.deb) ----------------
def is_live(_=None):
    return {'live': os.path.exists('/run/casper') or os.path.exists('/cdrom/casper')}


def pkg_install(a):
    path = os.path.abspath(a.get('path') or '')
    if not path.endswith('.deb') or not os.path.isfile(path):
        return {'ok': False, 'message': 'File non valido.'}
    env = ['env', 'DEBIAN_FRONTEND=noninteractive']
    # aggiorna l'elenco dei pacchetti (sulla chiavetta è vuoto) per poter scaricare le dipendenze
    rc, out, err = sudo(env + ['apt-get', 'update', '-qq'], 300)
    if rc != 0 and 'Could not resolve' in (err + out):
        return {'ok': False, 'message': 'Serve internet per installare: collegati al Wi‑Fi e riprova.'}
    rc, out, err = sudo(env + ['apt-get', 'install', '-y', '-qq', path], 1200)
    if rc == 0:
        return {'ok': True, 'apps': register_apps(run(['dpkg-deb', '-f', path, 'Package'])[1].strip())}
    msg = (err or out).strip().splitlines()
    tail = msg[-1] if msg else ''
    if 'Unable to locate' in tail or 'Depends' in (err + out):
        return {'ok': False, 'message': 'Mancano dei componenti che non riesco a scaricare. Controlla la connessione e riprova.'}
    if 'Could not get lock' in (err + out):
        return {'ok': False, 'message': 'Un\u2019altra installazione è in corso. Riprova tra un minuto.'}
    return {'ok': False, 'message': 'Installazione non riuscita: ' + tail[:200]}


APPS_FILE = os.path.join(HOME, '.config', 'rise', 'apps.json')


def extra_apps():
    try:
        return json.load(open(APPS_FILE))
    except Exception:
        return []


def register_apps(pkg):
    """Aggiunge al menu Start i programmi (file .desktop) contenuti nel pacchetto appena installato."""
    if not pkg:
        return []
    _, out, _ = run(['dpkg', '-L', pkg])
    found = []
    for f in out.splitlines():
        if not (f.startswith('/usr/share/applications/') and f.endswith('.desktop')):
            continue
        d = {}
        try:
            sec = None
            for l in open(f, errors='replace'):
                l = l.strip()
                if l.startswith('['):
                    sec = l
                elif sec == '[Desktop Entry]' and '=' in l:
                    k, v = l.split('=', 1)
                    d.setdefault(k, v)
        except OSError:
            continue
        if d.get('NoDisplay') == 'true' or not d.get('Exec'):
            continue
        cmd = [x for x in d['Exec'].split() if not x.startswith('%')]
        found.append({'id': 'pkg:' + os.path.basename(f)[:-8], 'name': d.get('Name', pkg),
                      'desc': d.get('Comment', 'Programma installato'), 'cmd': cmd})
    apps = [x for x in extra_apps() if x['id'] not in {y['id'] for y in found}] + found
    os.makedirs(os.path.dirname(APPS_FILE), exist_ok=True)
    json.dump(apps, open(APPS_FILE, 'w'))
    return found


# ---------------- Documenti Word (.docx) creati dall'Assistente ----------------
_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _docx_runs(text, bold=False):
    from xml.sax.saxutils import escape
    runs = []
    for part in re.split(r'(\*\*[^*]+\*\*|__[^_]+__)', text):
        if not part:
            continue
        strong = part[:2] in ('**', '__') and part[-2:] == part[:2] and len(part) > 4
        t = part[2:-2] if strong else part.replace('**', '')
        rpr = '<w:rPr><w:b/></w:rPr>' if (bold or strong) else ''
        runs.append('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, escape(t)))
    return ''.join(runs) or '<w:r><w:t></w:t></w:r>'


def _docx_para(text, style=None, bold=False, align=None, num=False):
    ppr = ''
    if style:
        ppr += '<w:pStyle w:val="%s"/>' % style
    if num:
        ppr += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    if align:
        ppr += '<w:jc w:val="%s"/>' % align
    return '<w:p>%s%s</w:p>' % ('<w:pPr>%s</w:pPr>' % ppr if ppr else '', _docx_runs(text, bold))


def _docx_table(rows):
    cols = max(len(r) for r in rows)
    # la prima colonna (descrizione) più larga, le altre (numeri) più strette
    widths = [9638 // cols] * cols if cols < 3 else [9638 - 1900 * (cols - 1)] + [1900] * (cols - 1)
    if cols == 4:
        widths = [4738, 900, 2000, 2000]
    grid = ''.join('<w:gridCol w:w="%d"/>' % w for w in widths)
    out = []
    for i, r in enumerate(rows):
        cells = []
        for j in range(cols):
            width = widths[j]
            c = r[j] if j < len(r) else ''
            num = bool(re.fullmatch(r'[-+€$\s\d.,%]+', c.strip())) and c.strip() != ''
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="DCE8FA"/>' if i == 0 else ''
            cells.append('<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/>%s</w:tcPr>%s</w:tc>'
                         % (width, shade, _docx_para(c.strip(), 'Tabella', bold=(i == 0), align='right' if num and i else None)))
        out.append('<w:tr>%s</w:tr>' % ''.join(cells))
    return ('<w:tbl><w:tblPr><w:tblStyle w:val="Griglia"/><w:tblW w:w="5000" w:type="pct"/></w:tblPr>'
            '<w:tblGrid>%s</w:tblGrid>%s</w:tbl>' % (grid, ''.join(out)))


def _docx_xml(text):
    """Testo semplice (con # titoli, - elenchi, **grassetto** e tabelle | a | b |) → corpo di un .docx."""
    out, table = [], []

    def flush():
        if table:
            out.append(_docx_table(table))
            out.append(_docx_para(''))
            table.clear()

    for line in text.replace('\r', '').split('\n'):
        l = line.strip()
        if l.startswith('|') and l.endswith('|') and len(l) > 1:
            cells = [c for c in l[1:-1].split('|')]
            if all(re.fullmatch(r'\s*:?-{2,}:?\s*', c) for c in cells):
                continue                      # riga |---|---| delle tabelle markdown
            table.append(cells)
            continue
        flush()
        m = re.match(r'(#{1,3})\s+(.*)', l)
        if m:
            out.append(_docx_para(m.group(2).replace('**', ''), 'Titolo%d' % len(m.group(1))))
        elif re.match(r'[-*•]\s+', l):
            out.append(_docx_para(re.sub(r'^[-*•]\s+', '', l), 'Elenco', num=True))
        elif re.fullmatch(r'-{3,}|\*{3,}|_{3,}', l):
            out.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="9AA7B8"/></w:pBdr></w:pPr></w:p>')
        else:
            out.append(_docx_para(l))
    flush()
    return (_XML + '<w:document xmlns:w="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>' % _W
            + ''.join(out) +
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
            'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')


def _docx_styles():
    def heading(n, size, color, before):
        return ('<w:style w:type="paragraph" w:styleId="Titolo%d"><w:name w:val="heading %d"/><w:basedOn w:val="Normal"/>'
                '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="%d" w:after="120"/>'
                '<w:outlineLvl w:val="%d"/></w:pPr><w:rPr><w:b/><w:color w:val="%s"/><w:sz w:val="%d"/></w:rPr></w:style>'
                % (n, n, before, n - 1, color, size))
    return (_XML + '<w:styles xmlns:w="%s">' % _W +
            '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Liberation Sans" w:hAnsi="Liberation Sans" w:cs="Liberation Sans" w:eastAsia="Liberation Sans"/>'
            '<w:sz w:val="22"/><w:lang w:val="it-IT"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
            + heading(1, 36, '1F4E9E', 0) + heading(2, 28, '2456E0', 240) + heading(3, 24, '333333', 200) +
            '<w:style w:type="paragraph" w:styleId="Elenco"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>'
            '<w:pPr><w:spacing w:after="60"/><w:ind w:left="720"/></w:pPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Tabella"><w:name w:val="Table Text"/><w:basedOn w:val="Normal"/>'
            '<w:pPr><w:spacing w:before="40" w:after="40" w:line="240" w:lineRule="auto"/></w:pPr></w:style>'
            '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
            '<w:tblPr><w:tblCellMar><w:left w:w="108" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
            '<w:style w:type="table" w:styleId="Griglia"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/><w:tblPr><w:tblBorders>'
            + ''.join('<w:%s w:val="single" w:sz="4" w:space="0" w:color="9AA7B8"/>' % e for e in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV')) +
            '</w:tblBorders></w:tblPr></w:style></w:styles>')


def _docx_numbering():
    return (_XML + '<w:numbering xmlns:w="%s"><w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/>'
            '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:lvlJc w:val="left"/>'
            '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>'
            '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>' % _W)


def doc_create(a):
    """Crea un vero documento Word (.docx) in Documenti a partire dal testo scritto dall'Assistente."""
    import zipfile
    from xml.sax.saxutils import escape
    text = a.get('text') or a.get('testo') or ''
    title = (a.get('title') or a.get('titolo') or '').strip()
    if not title:                               # niente titolo: si usa il primo titolo del testo
        m = re.search(r'^#{1,3}\s+(.+)$', text, re.M)
        title = m.group(1) if m else 'Documento'
    title = re.sub(r'[\\/:*?"<>|\n#]+', ' ', title.replace('**', '')).strip()[:70].strip() or 'Documento'
    folder = os.path.join(HOME, 'Documenti')
    if not os.path.isdir(folder) and os.path.isdir(os.path.join(HOME, 'Documents')):
        folder = os.path.join(HOME, 'Documents')
    missing = ''
    if a.get('cartella'):                        # "salvalo nella cartella prova sul desktop"
        try:
            import riseauto
            folder, missing = riseauto.resolve_folder(a['cartella'])
        except Exception:
            missing = 'non trovata'
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, title + '.docx')
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, '%s (%d).docx' % (title, n))
        n += 1
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    ov = '<Override PartName="/%s" ContentType="application/vnd.openxmlformats-%s+xml"/>'
    ct = (_XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          + ov % ('word/document.xml', 'officedocument.wordprocessingml.document.main')
          + ov % ('word/styles.xml', 'officedocument.wordprocessingml.styles')
          + ov % ('word/numbering.xml', 'officedocument.wordprocessingml.numbering')
          + ov % ('docProps/core.xml', 'package.core-properties')
          + ov % ('docProps/app.xml', 'officedocument.extended-properties') + '</Types>')
    rel = '<Relationship Id="%s" Type="%s" Target="%s"/>'
    od = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    rels = (_XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + rel % ('rId1', od + 'officeDocument', 'word/document.xml')
            + rel % ('rId2', 'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties', 'docProps/core.xml')
            + rel % ('rId3', od + 'extended-properties', 'docProps/app.xml') + '</Relationships>')
    docrels = (_XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + rel % ('rId1', od + 'styles', 'styles.xml') + rel % ('rId2', od + 'numbering', 'numbering.xml') + '</Relationships>')
    core = (_XML + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>%s</dc:title><dc:creator>Rise</dc:creator>'
            '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
            '</cp:coreProperties>' % (escape(title), now, now))
    app = (_XML + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           '<Application>Rise Assistente</Application></Properties>')
    tmp = path + '.tmp'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ct)
        z.writestr('_rels/.rels', rels)
        z.writestr('docProps/core.xml', core)
        z.writestr('docProps/app.xml', app)
        z.writestr('word/_rels/document.xml.rels', docrels)
        z.writestr('word/styles.xml', _docx_styles())
        z.writestr('word/numbering.xml', _docx_numbering())
        z.writestr('word/document.xml', _docx_xml(text))
    os.replace(tmp, path)
    where = 'Scrivania (desktop)' if os.path.basename(folder) == 'Scrivania' else os.path.basename(folder)
    return {'ok': True, 'path': path, 'name': os.path.basename(path), 'size': os.path.getsize(path),
            'folder': where, 'missing': missing}


def _num(v):
    """'1.234,50 €' / '1234.5' / 12 → 1234.5"""
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r'[^\d,.\-]', '', str(v or ''))
    if not t:
        return 0.0
    if ',' in t and '.' in t:
        t = t.replace('.', '').replace(',', '.') if t.rfind(',') > t.rfind('.') else t.replace(',', '')
    elif ',' in t:
        t = t.replace(',', '.')
    elif t.count('.') > 1 or re.fullmatch(r'\d{1,3}\.\d{3}', t):
        t = t.replace('.', '')
    try:
        return float(t)
    except ValueError:
        return 0.0


def _eur(x):
    s = '{:,.2f}'.format(x).replace(',', 'X').replace('.', ',').replace('X', '.')
    return s + ' €'


def doc_preventivo_conf(_=None):
    """Dati già noti per il modulo del preventivo: la tua ditta e il prossimo numero."""
    try:
        conf = json.load(open(os.path.join(HOME, '.config', 'rise', 'preventivi.json')))
    except (OSError, ValueError):
        conf = {}
    year = time.strftime('%Y')
    return {'ok': True, 'azienda': conf.get('azienda', ''), 'numero': '%d/%s' % (conf.get('ultimo', {}).get(year, 0) + 1, year)}


def doc_preventivo(a):
    """Preventivo impaginato: i conti (righe, imponibile, IVA, totale) li fa Rise, non l'Assistente."""
    conf_p = os.path.join(HOME, '.config', 'rise', 'preventivi.json')
    try:
        conf = json.load(open(conf_p))
    except (OSError, ValueError):
        conf = {}
    year = time.strftime('%Y')
    azienda = (a.get('azienda') or conf.get('azienda') or '').strip()
    numero = str(a.get('numero') or '').strip()
    if not numero:
        n = conf.get('ultimo', {}).get(year, 0) + 1
        numero = '%d/%s' % (n, year)
        conf.setdefault('ultimo', {})[year] = n
    if a.get('azienda'):
        conf['azienda'] = azienda
    data = (a.get('data') or time.strftime('%d/%m/%Y')).strip()
    cliente = (a.get('cliente') or '').strip()
    oggetto = (a.get('oggetto') or '').strip()
    iva = _num(a.get('iva')) if a.get('iva') not in (None, '') else 22.0
    rows, imponibile = [], 0.0
    for v in a.get('voci') or []:
        if isinstance(v, str):
            v = {'descrizione': v}
        q = _num(v.get('quantita', v.get('quantità', 1))) or 1
        pr = _num(v.get('prezzo', v.get('prezzo_unitario', 0)))
        tot = q * pr
        imponibile += tot
        qs = ('%g' % q).replace('.', ',')
        rows.append('| %s | %s | %s | %s |' % ((v.get('descrizione') or '').replace('|', '/'), qs, _eur(pr), _eur(tot)))
    imposta = imponibile * iva / 100
    L = []
    if azienda:
        lines = [x.strip() for x in re.split(r'\n|,\s*(?=(?:via|viale|piazza|corso|p\.?\s?iva|tel|email|e-mail)\b)', azienda, flags=re.I) if x.strip()]
        L.append('**%s**' % lines[0])
        L += lines[1:]
        L.append('')
    L.append('# Preventivo n. %s' % numero)
    L.append('Data: **%s**' % data)
    L.append('')
    if cliente:
        L.append('Spett.le **%s**' % cliente.replace('\n', '**\n**'))
    if oggetto:
        L.append('Oggetto: **%s**' % oggetto)
    L.append('')
    if rows:
        L.append('| Descrizione | Q.tà | Prezzo unitario | Importo |')
        L.append('|---|---|---|---|')
        L += rows
        L.append('| | | Imponibile | %s |' % _eur(imponibile))
        if iva:
            L.append('| | | IVA %s%% | %s |' % (('%g' % iva).replace('.', ','), _eur(imposta)))
        L.append('| | | **Totale** | **%s** |' % _eur(imponibile + imposta))
        L.append('')
    note = (a.get('note') or '').strip()
    if note:
        L.append('## Note')
        L += [('- ' + x.lstrip('-• ').strip()) for x in note.split('\n') if x.strip()]
        L.append('')
    L.append('## Condizioni')
    L.append('- Pagamento: %s' % ((a.get('pagamento') or '').strip() or 'da concordare'))
    L.append('- Validità dell’offerta: %s' % ((a.get('validita') or '').strip() or '30 giorni dalla data del preventivo'))
    L.append('')
    L.append('')
    L.append('Per accettazione ______________________________')
    title = 'Preventivo %s' % numero.replace('/', '-')
    if cliente:
        title += ' - ' + cliente.split('\n')[0]
    r = doc_create({'title': title, 'text': '\n'.join(L), 'cartella': a.get('cartella') or ''})
    if r.get('ok'):
        try:
            os.makedirs(os.path.dirname(conf_p), exist_ok=True)
            json.dump(conf, open(conf_p, 'w'), ensure_ascii=False)
        except OSError:
            pass
        r['totale'] = _eur(imponibile + imposta)
    return r


def sys_bootwhy(_):
    """Le fasi più lente dell'avvio (per capire cosa rallenta), in parole e secondi."""
    _, total, _ = run(['systemd-analyze', '--no-pager'], timeout=20)
    _, blame, _ = run(['systemd-analyze', 'blame', '--no-pager'], timeout=20)
    _, chain, _ = run(['systemd-analyze', 'critical-chain', '--no-pager'], timeout=20)
    items = []
    for l in blame.splitlines()[:12]:
        parts = l.split()
        if len(parts) >= 2:
            items.append({'time': ' '.join(parts[:-1]), 'unit': parts[-1]})
    return {'total': total.splitlines()[0] if total else '', 'slow': items,
            'chain': [l.strip() for l in chain.splitlines()[2:14]]}

CMDS = {'sys.bootwhy': sys_bootwhy, 'doc.create': doc_create, 'doc.preventivo': doc_preventivo, 'doc.preventivoConf': doc_preventivo_conf, 'kbd.set': kbd_set, 'pkg.install': pkg_install, 'sys.live': is_live, 'net.status': net_status, 'net.scan': net_scan, 'net.connect': net_connect, 'net.radio': net_radio,
        'net.diag': net_diag, 'audio.get': audio_get, 'audio.set': audio_set, 'bright.get': bright_get,
        'bright.set': bright_set, 'power': power, 'sys.info': sys_info, 'fs.roots': fs_roots,
        'fs.mount': fs_mount, 'fs.list': fs_list, 'fs.read': fs_read,
        'fs.mkdir': fs_mkdir, 'fs.rename': fs_rename, 'fs.copy': fs_copy, 'fs.move': fs_move, 'fs.trash': fs_trash,
        'fs.trashList': fs_trash_list, 'fs.trashRestore': fs_trash_restore, 'fs.trashEmpty': fs_trash_empty,
        'fs.search': fs_search, 'sys.boot': sys_boot, 'index.update': index_update,
        'net.hosts': net_hosts, 'net.shares': net_shares, 'net.mount': net_mount,
        'share.list': share_list, 'share.add': share_add, 'share.remove': share_remove}



# Assistente (intelligenza artificiale sul computer)
try:
    import riseai
    CMDS.update(riseai.CMDS)
except Exception:  # senza il modulo l'Assistente risulta semplicemente non disponibile
    pass
for _m in ('risevoice', 'riseweb', 'riseapps', 'riseauto', 'riseremind', 'riseupdate', 'riseinstall', 'risenet', 'riseprint', 'risebt'):
    try:
        CMDS.update(__import__(_m).CMDS)
    except Exception:
        pass

# ---------------- sfondo del desktop ----------------
def _wall_dir():
    d = os.path.join(HOME, '.config', 'rise')
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):                      # si tiene solo l'ultimo sfondo scelto
        if f.startswith('sfondo-'):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
    return d


def wall_save(a):
    """Salva come sfondo una foto scelta dall'utente (arriva dal desktop in base64)."""
    ext = re.sub(r'[^a-z0-9]', '', (a.get('ext') or 'jpg').lower())[:5] or 'jpg'
    try:
        data = base64.b64decode(a.get('data') or '')
    except Exception:
        return {'ok': False, 'message': 'Immagine non valida.'}
    if len(data) < 100:
        return {'ok': False, 'message': 'Immagine non valida.'}
    path = os.path.join(_wall_dir(), 'sfondo-%d.%s' % (int(time.time()), ext))
    open(path, 'wb').write(data)
    return {'ok': True, 'path': path}


def wall_copy(a):
    """Usa come sfondo una foto che è già nel computer (da Esplora file o dal Visualizzatore)."""
    import shutil as _sh
    src = os.path.abspath(os.path.expanduser(a.get('path') or ''))
    if not os.path.isfile(src):
        return {'ok': False, 'message': 'Foto non trovata.'}
    ext = os.path.splitext(src)[1].lower().lstrip('.') or 'jpg'
    path = os.path.join(_wall_dir(), 'sfondo-%d.%s' % (int(time.time()), ext))
    _sh.copyfile(src, path)
    return {'ok': True, 'path': path}


def desk_ensure(_=None):
    d = os.path.join(HOME, 'Scrivania')
    os.makedirs(d, exist_ok=True)
    return {'ok': True, 'path': d}


CMDS.update({'wall.save': wall_save, 'wall.copy': wall_copy, 'desk.ensure': desk_ensure})


def shot_take(_=None):
    """Foto dello schermo (grim) nella cartella Immagini."""
    d = os.path.join(HOME, 'Immagini')
    os.makedirs(d, exist_ok=True)
    name = time.strftime('Schermata %Y-%m-%d alle %H.%M.%S.png')
    path = os.path.join(d, name)
    time.sleep(0.4)                       # il tempo di far sparire il messaggio appena scritto
    rc, _, err = run(['grim', path], timeout=20)
    if rc != 0 or not os.path.exists(path):
        return {'ok': False, 'message': 'Non riesco a fotografare lo schermo. ' + err[-120:]}
    return {'ok': True, 'path': path, 'name': name}


CMDS.update({'shot.take': shot_take})
