"""Rise – azioni dell'Assistente sul computer: cartelle, file, PDF, email e rubrica.

Tutto quello che esce dal computer (le email) parte solo dopo la conferma dell'utente: l'Assistente
prepara la bozza, Rise la mostra, l'utente dice "invia" o preme il pulsante.
"""
import glob
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
import unicodedata
import zipfile
from email.message import EmailMessage
from xml.sax.saxutils import unescape

HOME = os.path.expanduser('~')
CONF = os.path.join(HOME, '.config', 'rise')
MAIL_FILE = os.path.join(CONF, 'posta.json')
CONTACTS_FILE = os.path.join(CONF, 'rubrica.json')
for _p in glob.glob('/opt/rise-py/lib/python3*/site-packages'):     # fpdf2 (licenza LGPL) per i PDF
    if _p not in sys.path:
        sys.path.append(_p)

def _xdg(key, default):
    """Le cartelle dell'utente come le conosce il sistema (~/.config/user-dirs.dirs), relative alla home."""
    try:
        for line in open(os.path.join(os.path.expanduser('~'), '.config', 'user-dirs.dirs'), errors='replace'):
            if line.startswith('XDG_%s_DIR=' % key):
                v = line.split('=', 1)[1].strip().strip('"').replace('$HOME', os.path.expanduser('~'))
                rel = os.path.relpath(v, os.path.expanduser('~'))
                if rel and not rel.startswith('..') and rel != '.':
                    return rel
    except OSError:
        pass
    return default


_DESK = _xdg('DESKTOP', 'Scrivania')
PLACES = {'documenti': _xdg('DOCUMENTS', 'Documenti'), 'desktop': _DESK, 'scrivania': _DESK, 'download': _xdg('DOWNLOAD', 'Download'),
          'scaricati': _xdg('DOWNLOAD', 'Download'), 'immagini': _xdg('PICTURES', 'Immagini'), 'foto': _xdg('PICTURES', 'Immagini'),
          'musica': _xdg('MUSIC', 'Musica'), 'video': _xdg('VIDEOS', 'Video'), 'home': '', 'casa': ''}
KINDS = {'word': ('.docx', '.doc', '.odt', '.rtf'), 'documento': ('.docx', '.doc', '.odt', '.rtf', '.pdf', '.txt'),
         'excel': ('.xlsx', '.xls', '.ods', '.csv'), 'foglio': ('.xlsx', '.xls', '.ods', '.csv'),
         'pdf': ('.pdf',), 'presentazione': ('.pptx', '.ppt', '.odp'),
         'foto': ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.gif'), 'immagine': ('.jpg', '.jpeg', '.png', '.webp', '.gif'),
         'video': ('.mp4', '.mkv', '.avi', '.mov', '.webm'), 'musica': ('.mp3', '.flac', '.ogg', '.m4a', '.wav')}


def _norm(s):
    s = unicodedata.normalize('NFD', s.lower())
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')


def _place(where):
    w = _norm(where or 'documenti').strip()
    for k, v in PLACES.items():
        if k in w:
            return os.path.join(HOME, v) if v else HOME
    if w.startswith('/') and os.path.isdir(w):
        return w
    return os.path.join(HOME, 'Documenti')


# ---------------- cartelle ----------------
_PLACE_RE = r'(?:sul|sulla|sui|nel|nella|nei|nelle|in|dentro(?:\s+(?:a|al|alla))?)\s+(?:mio\s+|mia\s+|miei\s+|il\s+|la\s+)?'


def _split_place(text):
    """"prova sul mio desktop" -> ("prova", Scrivania); "fatture dentro la cartella clienti" -> ("fatture", …/clienti)."""
    t = (text or '').strip().rstrip('.!?')
    m = re.search(r'\s*\b(?:dentro|nella|nel|in)\s+(?:la\s+)?cartella\s+(.+)$', t, re.I)
    if m:
        base, miss = resolve_folder(m.group(1))
        if not miss:
            return t[:m.start()].strip(), base
    for k, v in PLACES.items():
        m = re.search(r'\s*\b' + _PLACE_RE + k + r'\b', t, re.I) or re.search(r'^\s*' + _PLACE_RE + k + r'\b\s*', t, re.I)
        if m:
            return (t[:m.start()] + ' ' + t[m.end():]).strip(), (os.path.join(HOME, v) if v else HOME)
    return t, None


_NAMED = re.compile(r'(?:^|\s|,)(?:e\s+)?(?:la\s+)?(?:chiamala|chiamata|chiamarla|che si chiami|che si chiama|di nome|con (?:il )?nome|nominala|dalle il nome|col nome|con scritto)\s+["“]?(.+?)["”]?\s*$', re.I)


def make_folder(a):
    raw = (a.get('nome') or a.get('name') or a.get('testo') or '').strip().rstrip('.!?')
    # "cartella sul desktop e chiamala preventivi": il nome è quello dopo "chiamala"
    named = _NAMED.search(raw)
    if named:
        rest, nm = raw[:named.start()].strip(' ,'), named.group(1)
        rest2, base0 = _split_place(rest)
        nm2, base1 = _split_place(nm)
        raw, base = nm2.strip(), (base1 or base0)
    else:
        raw, base = _split_place(raw)
    raw = re.sub(r'^(?:una\s+)?(?:nuova\s+)?cartella(?:\s+|$)', '', raw.strip(' ,;:'), flags=re.I)
    raw = re.sub(r'^(?:chiamata|di nome|con nome|con il nome|nominata)\s+', '', raw, flags=re.I).strip(' "“”\'')
    name = re.sub(r'[\\/:*?"<>|]+', ' ', raw)[:80].strip(' ,;:.')
    if not name:
        return {'ok': False, 'message': 'Come la chiamo?'}
    if not base:
        d = a.get('dove') or a.get('where') or ''
        base = resolve_folder(d)[0] if d and not any(k in _norm(d) for k in PLACES) else _place(d)
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, name)
    existed = os.path.isdir(path)
    os.makedirs(path, exist_ok=True)
    return {'ok': True, 'path': path, 'existed': existed,
            'where': os.path.basename(base) or 'cartella personale'}


# ---------------- dove salvare: "nella cartella prova", "sul desktop" ----------------
def resolve_folder(name):
    """Cartella in cui salvare un documento. Vuoto → Documenti. Accetta luoghi (desktop, download…),
    nomi di cartelle esistenti ("prova", "cartella clienti sul desktop") o un percorso."""
    docs = os.path.join(HOME, 'Documenti')
    n = (name or '').strip()
    if not n:
        return docs, ''
    if n.startswith(('/', '~')):
        p = os.path.expanduser(n)
        return (p, '') if os.path.isdir(p) else (docs, 'non trovata')
    low = _norm(n)
    words = [w for w in re.findall(r'\w+', re.sub(r'\b(cartell\w*|nella|nel|sul|sulla|in|la|il)\b', ' ', low))]
    only_place = [k for k in PLACES if low.strip() in (k, 'il ' + k, 'sul ' + k, 'cartella ' + k)]
    if only_place:
        p = os.path.join(HOME, PLACES[only_place[0]])
        os.makedirs(p, exist_ok=True)
        return p, ''
    r = find_files({'testo': 'cartella ' + n, 'tipo': 'cartella'})
    for x in r.get('results') or []:
        if x.get('dir'):
            return x['path'], ''
    return docs, 'non trovata'


# ---------------- trovare i file ----------------
SKIP = {'.cache', '.local', '.config', '.mozilla', 'node_modules', '__pycache__', '.git', 'snap'}


def find_files(a):
    q = _norm(a.get('testo') or a.get('q') or '')
    kind = _norm(a.get('tipo') or '')
    want_dir = bool(re.search(r'\b(cartell\w*|folder)\b', kind + ' ' + q))
    # "sul desktop", "in documenti": si cerca prima lì, e la parola non fa parte del nome
    prefer = None
    for k, v in PLACES.items():
        if re.search(r'\b%s\b' % k, q):
            prefer = os.path.join(HOME, v)
            q = re.sub(r'\b(sul|sulla|nel|nella|in|dal|dalla|della|del)?\s*%s\b' % k, ' ', q)
    q = re.sub(r'\b(cartell\w*|folder)\b', ' ', q)
    exts = None
    for k, v in KINDS.items():
        if k in kind or (not kind and re.search(r'\b%s\b' % k, q)):
            exts = v
            q = re.sub(r'\b(il |la |lo |l\')?(file |documento )?%s\b' % k, ' ', q)
            break
    stop = {'il', 'la', 'lo', 'le', 'i', 'gli', 'un', 'una', 'di', 'del', 'della', 'file', 'documento', 'apri',
            'aprimi', 'mi', 'che', 'ho', 'fatto', 'ultimo', 'ultima', 'mio', 'mia', 'per', 'a', 'al', 'con',
            'sul', 'sulla', 'nel', 'nella', 'in', 'chiamata', 'chiamato', 'nome'}
    words = [w for w in re.findall(r'[\w]+', q) if w not in stop and len(w) > 1]
    latest = bool(re.search(r'\bultim', _norm(a.get('testo') or '')))
    roots = [HOME] + [d for d in glob.glob('/media/*/*') if os.path.isdir(d)]
    if not words and not exts and prefer and os.path.isdir(prefer):     # "apri il desktop", "apri documenti"
        return {'ok': True, 'results': [{'path': prefer, 'name': os.path.basename(prefer), 'dir': True, 'size': 0,
                                         'mtime': int(os.stat(prefer).st_mtime * 1000), 'folder': '~'}]}
    found, t0 = [], time.time()
    for root in roots:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith('.') and d not in SKIP]
            if dp.count(os.sep) - root.count(os.sep) > 6 or time.time() - t0 > 4:
                dns[:] = []
            if words and not exts:                     # anche le cartelle col nome giusto
                for d in dns:
                    low = _norm(d)
                    if all(w in low for w in words):
                        p = os.path.join(dp, d)
                        try:
                            mt = int(os.stat(p).st_mtime * 1000)
                        except OSError:
                            continue
                        found.append({'path': p, 'name': d, 'dir': True, 'size': 0, 'mtime': mt,
                                      'folder': dp.replace(HOME, '~', 1)})
            if want_dir:
                continue
            for f in fns:
                if f.startswith('.'):
                    continue
                low = _norm(f)
                if exts and not low.endswith(exts):
                    continue
                if words and not all(w in low for w in words):
                    continue
                if not words and not exts:
                    continue
                p = os.path.join(dp, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                found.append({'path': p, 'name': f, 'size': st.st_size, 'mtime': int(st.st_mtime * 1000),
                              'folder': dp.replace(HOME, '~', 1)})
    # prima le cartelle se ha detto "cartella", poi quelle nel posto indicato, poi le più recenti
    found.sort(key=lambda x: (not (want_dir and x.get('dir')), not (prefer and x['path'].startswith(prefer + os.sep)),
                              -x['mtime']))
    if latest:
        found = found[:1]
    return {'ok': True, 'results': found[:8]}


# ---------------- rinominare ed eliminare ----------------
PROTECTED = {'Documenti', 'Scrivania', 'Download', 'Immagini', 'Musica', 'Video', 'Desktop', 'Documents', 'Pictures'}


def _candidates(text, kind=''):
    """File o cartelle col nome indicato: prima quelli col nome identico."""
    want_dir = 'cartell' in _norm(kind)
    want_file = 'file' in _norm(kind) or 'document' in _norm(kind)
    q = (('cartella ' if want_dir else '') + (text or '')).strip()
    res = find_files({'testo': q, 'tipo': 'cartella' if want_dir else ''}).get('results') or []
    if want_file:
        res = [r for r in res if not r.get('dir')]
    res = [r for r in res if not (r.get('dir') and os.path.basename(r['path']) in PROTECTED
                                  and os.path.dirname(r['path']) == HOME) and r['path'] != HOME]
    words = _norm(re.sub(r'\b(cartell\w*|file|documento|il|la|lo)\b', ' ', text or '')).split()
    exact = [r for r in res if _norm(os.path.splitext(r['name'])[0] if not r.get('dir') else r['name']) == ' '.join(words)]
    return exact or res


def rename(a):
    """Rinomina un file o una cartella trovata per nome (stessa cartella; l'estensione dei file resta)."""
    new = re.sub(r'[\\/:*?"<>|]+', ' ', (a.get('nuovo') or a.get('new') or '').strip().strip('"“”\'')).strip()
    if not new:
        return {'ok': False, 'message': 'Come lo devo chiamare?'}
    if a.get('path'):
        c = [{'path': a['path'], 'name': os.path.basename(a['path']), 'dir': os.path.isdir(a['path'])}]
    else:
        c = _candidates(a.get('testo') or a.get('nome') or '', a.get('tipo') or '')
    if not c:
        return {'ok': False, 'message': 'Non trovo niente con quel nome.'}
    if len(c) > 1 and not a.get('path'):
        return {'ok': False, 'choose': c[:6], 'message': 'Ho trovato più cose con quel nome: quale?'}
    src = c[0]['path']
    if os.path.basename(src) in PROTECTED and os.path.dirname(src) == HOME:
        return {'ok': False, 'message': 'Questa cartella del sistema non si rinomina.'}
    if not os.path.isdir(src) and not os.path.splitext(new)[1]:
        new += os.path.splitext(src)[1]
    dst = os.path.join(os.path.dirname(src), new)
    if os.path.exists(dst):
        return {'ok': False, 'message': 'Esiste già “%s” in quella cartella.' % new}
    os.rename(src, dst)
    return {'ok': True, 'path': dst, 'old': os.path.basename(src), 'name': new, 'dir': os.path.isdir(dst),
            'folder': os.path.basename(os.path.dirname(dst))}


def find_for_delete(a):
    c = _candidates(a.get('testo') or a.get('nome') or '', a.get('tipo') or '')
    return {'ok': bool(c), 'results': c[:6], 'message': '' if c else 'Non trovo niente con quel nome.'}


def trash(a):
    """Sposta nel Cestino (si può recuperare): mai le cartelle principali."""
    done, bad = [], []
    for p in a.get('paths') or []:
        p = os.path.abspath(os.path.expanduser(p))
        if not p.startswith(HOME + os.sep) or (os.path.dirname(p) == HOME and os.path.basename(p) in PROTECTED):
            bad.append(os.path.basename(p))
            continue
        r = subprocess.run(['gio', 'trash', '--', p], capture_output=True, text=True)
        (done if r.returncode == 0 else bad).append(os.path.basename(p))
    return {'ok': not bad, 'done': done, 'failed': bad}


# ---------------- PDF ----------------
def _font_paths():
    for base in ('/usr/share/fonts/truetype/liberation', '/usr/share/fonts/truetype/liberation2'):
        r, b = os.path.join(base, 'LiberationSans-Regular.ttf'), os.path.join(base, 'LiberationSans-Bold.ttf')
        if os.path.exists(r) and os.path.exists(b):
            return r, b
    return None, None


def _pdf_from_markup(text, path, title=''):
    from fpdf import FPDF
    pdf = FPDF(format='A4')
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(True, 18)
    reg, bold = _font_paths()
    if reg:
        pdf.add_font('Rise', '', reg)
        pdf.add_font('Rise', 'B', bold)
        fam = 'Rise'
    else:
        fam = 'Helvetica'
    pdf.set_title(title or 'Documento')
    pdf.set_creator('Rise')
    pdf.add_page()
    W = pdf.w - pdf.l_margin - pdf.r_margin

    def para(t, size=11, style='', color=(26, 26, 26), after=2.5):
        pdf.set_font(fam, style, size)
        pdf.set_text_color(*color)
        pdf.multi_cell(W, size * 0.5, t.replace('**', ''), markdown=False, new_x='LMARGIN', new_y='NEXT')
        pdf.ln(after)

    def rich(t):
        # grassetto **…** dentro la riga
        pdf.set_text_color(26, 26, 26)
        parts = re.split(r'(\*\*[^*]+\*\*)', t)
        for part in parts:
            if not part:
                continue
            b = part.startswith('**') and part.endswith('**')
            pdf.set_font(fam, 'B' if b else '', 11)
            pdf.write(5.5, part[2:-2] if b else part)
        pdf.ln(7.5)

    table = []

    def flush():
        if not table:
            return
        cols = max(len(r) for r in table)
        widths = [W * 0.49, W * 0.09, W * 0.21, W * 0.21] if cols == 4 else [W / cols] * cols
        pdf.set_draw_color(154, 167, 184)
        for i, r in enumerate(table):
            pdf.set_font(fam, 'B' if i == 0 else '', 10)
            if i == 0:
                pdf.set_fill_color(220, 232, 250)
            h = 7
            for j in range(cols):
                c = (r[j] if j < len(r) else '').strip()
                bold = c.startswith('**')
                c = c.replace('**', '')
                if bold:
                    pdf.set_font(fam, 'B', 10)
                empty = not c and i > 0
                pdf.cell(widths[j], h, c, border=0 if empty else 1, fill=(i == 0),
                         align='R' if j and re.fullmatch(r'[-+€$\s\d.,%]+', c or 'x') else 'L')
                if bold and i:
                    pdf.set_font(fam, '', 10)
            pdf.ln(h)
        pdf.ln(4)
        table.clear()

    for raw in (text or '').replace('\r', '').split('\n'):
        l = raw.strip()
        if l.startswith('|') and l.endswith('|') and len(l) > 1:
            cells = l[1:-1].split('|')
            if all(re.fullmatch(r'\s*:?-{2,}:?\s*', c) for c in cells):
                continue
            table.append(cells)
            continue
        flush()
        m = re.match(r'(#{1,3})\s+(.*)', l)
        if m:
            n = len(m.group(1))
            pdf.ln(2 if n > 1 else 0)
            para(m.group(2), {1: 18, 2: 14, 3: 12}[n], 'B', (31, 78, 158) if n == 1 else (36, 86, 224) if n == 2 else (40, 40, 40), 2)
        elif re.match(r'[-*•]\s+', l):
            pdf.set_x(pdf.l_margin + 4)
            rich('•  ' + re.sub(r'^[-*•]\s+', '', l))
        elif re.fullmatch(r'-{3,}|\*{3,}|_{3,}', l):
            y = pdf.get_y() + 2
            pdf.set_draw_color(154, 167, 184)
            pdf.line(pdf.l_margin, y, pdf.l_margin + W, y)
            pdf.ln(5)
        elif not l:
            pdf.ln(3)
        else:
            rich(l)
    flush()
    pdf.output(path)


def _docx_text(path):
    """Testo (con titoli, elenchi e tabelle) di un .docx, per trasformarlo in PDF."""
    with zipfile.ZipFile(path) as z:
        xml = z.read('word/document.xml').decode('utf8', 'replace')
    out = []
    body = re.search(r'<w:body>(.*)</w:body>', xml, re.S).group(1)
    for m in re.finditer(r'<w:tbl>.*?</w:tbl>|<w:p[ >].*?</w:p>|<w:p/>', body, re.S):
        blk = m.group(0)
        if blk.startswith('<w:tbl>'):
            for tr in re.findall(r'<w:tr[ >].*?</w:tr>', blk, re.S):
                cells = []
                for tc in re.findall(r'<w:tc>.*?</w:tc>', tr, re.S):
                    cells.append(unescape(''.join(re.findall(r'<w:t[^>]*>([^<]*)</w:t>', tc))))
                out.append('| ' + ' | '.join(cells) + ' |')
            out.append('')
            continue
        st = re.search(r'<w:pStyle w:val="([^"]+)"', blk)
        txt = ''
        for r in re.findall(r'<w:r[ >].*?</w:r>', blk, re.S):
            t = unescape(''.join(re.findall(r'<w:t[^>]*>([^<]*)</w:t>', r)))
            if t and '<w:b/>' in r and t.strip():
                t = '**%s**' % t
            txt += t
        s = st.group(1).lower() if st else ''
        if re.search(r'(titolo|heading)\s*1|^title', s):
            txt = '# ' + txt.replace('**', '')
        elif re.search(r'(titolo|heading)\s*2', s):
            txt = '## ' + txt.replace('**', '')
        elif re.search(r'(titolo|heading)\s*3', s):
            txt = '### ' + txt.replace('**', '')
        elif '<w:numPr>' in blk or 'elenco' in s or 'list' in s:
            txt = '- ' + txt
        out.append(txt)
    return '\n'.join(out)


def make_pdf(a):
    """Crea un PDF dal testo dell'Assistente, oppure trasforma in PDF un documento Word."""
    try:
        import fpdf  # noqa: F401
    except ImportError:
        return {'ok': False, 'message': 'Su questa versione di Rise manca il componente per i PDF.'}
    src = a.get('da_file') or a.get('path')
    title = (a.get('titolo') or a.get('title') or '').strip()
    if src:
        src = os.path.expanduser(src)
        if not os.path.isfile(src):
            return {'ok': False, 'message': 'Non trovo il file da trasformare.'}
        if src.lower().endswith('.pdf'):
            return {'ok': True, 'path': src, 'name': os.path.basename(src)}
        if not src.lower().endswith('.docx'):
            return {'ok': False, 'message': 'Per ora so trasformare in PDF solo i documenti Word (.docx).'}
        text = _docx_text(src)
        out = os.path.splitext(src)[0] + '.pdf'
        title = title or os.path.splitext(os.path.basename(src))[0]
    else:
        text = a.get('testo') or a.get('text') or ''
        if not title:
            m = re.search(r'^#{1,3}\s+(.+)$', text, re.M)
            title = m.group(1) if m else 'Documento'
        title = re.sub(r'[\\/:*?"<>|\n#]+', ' ', title.replace('**', '')).strip()[:70] or 'Documento'
        folder, _miss = resolve_folder(a.get('cartella') or '')
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(folder, title + '.pdf')
    n, base = 2, out[:-4]
    while os.path.exists(out) and not src:
        out = '%s (%d).pdf' % (base, n)
        n += 1
    _pdf_from_markup(text, out, title)
    return {'ok': True, 'path': out, 'name': os.path.basename(out), 'size': os.path.getsize(out)}


# ---------------- rubrica ----------------
def _contacts():
    try:
        return json.load(open(CONTACTS_FILE))
    except Exception:
        return []


def contacts(_=None):
    return {'ok': True, 'contacts': _contacts()}


def contact_save(a):
    name, email = (a.get('nome') or '').strip(), (a.get('email') or '').strip()
    if not name or not re.fullmatch(r'[^@\s]+@[^@\s]+\.\w+', email):
        return {'ok': False, 'message': 'Servono un nome e un indirizzo email valido.'}
    c = [x for x in _contacts() if _norm(x['nome']) != _norm(name)]
    c.append({'nome': name, 'email': email})
    os.makedirs(CONF, exist_ok=True)
    json.dump(sorted(c, key=lambda x: _norm(x['nome'])), open(CONTACTS_FILE, 'w'), ensure_ascii=False)
    return {'ok': True}


def contact_delete(a):
    c = [x for x in _contacts() if _norm(x['nome']) != _norm(a.get('nome') or '')]
    json.dump(c, open(CONTACTS_FILE, 'w'), ensure_ascii=False)
    return {'ok': True}


def resolve_address(who):
    who = (who or '').strip()
    if re.fullmatch(r'[^@\s]+@[^@\s]+\.\w+', who):
        return who, who
    n = _norm(who)
    best = None
    for c in _contacts():
        cn = _norm(c['nome'])
        if cn == n:
            return c['email'], c['nome']
        if n and (n in cn or all(w in cn for w in n.split())):
            best = best or c
    return (best['email'], best['nome']) if best else (None, who)


# ---------------- posta ----------------
PROVIDERS = {
    'gmail': ('smtp.gmail.com', 587, 'Gmail'),
    'outlook': ('smtp-mail.outlook.com', 587, 'Outlook / Hotmail'),
    'libero': ('smtp.libero.it', 465, 'Libero'),
    'aruba': ('smtps.aruba.it', 465, 'Aruba'),
    'arubapec': ('smtps.pec.aruba.it', 465, 'Aruba PEC'),
    'tim': ('smtp.tim.it', 587, 'TIM'),
    'yahoo': ('smtp.mail.yahoo.com', 587, 'Yahoo'),
}


def _mail_conf():
    try:
        return json.load(open(MAIL_FILE))
    except Exception:
        return {}


def mail_get(_=None):
    c = _mail_conf()
    tb = any(os.path.exists(p) for p in ('/var/lib/flatpak/app/org.mozilla.Thunderbird',
                                          os.path.join(HOME, '.local/share/flatpak/app/org.mozilla.Thunderbird')))
    return {'ok': True, 'configured': bool(c.get('email') and c.get('password')), 'email': c.get('email', ''),
            'name': c.get('name', ''), 'provider': c.get('provider', 'gmail'), 'thunderbird': tb,
            'providers': {k: v[2] for k, v in PROVIDERS.items()}}


def mail_set(a):
    c = _mail_conf()
    for k in ('email', 'name', 'provider', 'host', 'port'):
        if a.get(k) is not None:
            c[k] = a[k]
    if a.get('password'):
        c['password'] = a['password'].replace(' ', '')     # le password per le app di Google hanno spazi
    os.makedirs(CONF, exist_ok=True)
    fd = os.open(MAIL_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)      # leggibile solo dall'utente
    with os.fdopen(fd, 'w') as f:
        json.dump(c, f)
    return {'ok': True}


def _smtp_send(c, msg):
    host, port = c.get('host'), int(c.get('port') or 0)
    if not host:
        host, port, _ = PROVIDERS.get(c.get('provider', 'gmail'), PROVIDERS['gmail'])
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
        s.starttls()
    try:
        s.login(c['email'], c['password'])
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:
            pass


def mail_send(a):
    """Invia davvero l'email (lo chiama il desktop solo dopo la conferma dell'utente)."""
    c = _mail_conf()
    to, label = resolve_address(a.get('a') or a.get('to'))
    if not to:
        return {'ok': False, 'message': 'Non conosco l’indirizzo di “%s”.' % label, 'need': 'address'}
    if not (c.get('email') and c.get('password')):
        return {'ok': False, 'message': 'Prima va collegato il tuo account email (Impostazioni → Assistente).', 'need': 'account'}
    msg = EmailMessage()
    msg['From'] = ('%s <%s>' % (c['name'], c['email'])) if c.get('name') else c['email']
    msg['To'] = to
    msg['Subject'] = a.get('oggetto') or a.get('subject') or ''
    if a.get('in_reply_to'):                       # risposta: resta nella stessa conversazione
        msg['In-Reply-To'] = a['in_reply_to']
        msg['References'] = ((a.get('refs') or '') + ' ' + a['in_reply_to']).strip()
    msg.set_content(a.get('testo') or a.get('body') or '')
    for p in a.get('allegati') or []:
        p = os.path.expanduser(p)
        if not os.path.isfile(p):
            return {'ok': False, 'message': 'Non trovo l’allegato %s.' % os.path.basename(p)}
        ext = os.path.splitext(p)[1].lower()
        mt = {'.pdf': ('application', 'pdf'),
              '.docx': ('application', 'vnd.openxmlformats-officedocument.wordprocessingml.document'),
              '.xlsx': ('application', 'vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
              '.jpg': ('image', 'jpeg'), '.jpeg': ('image', 'jpeg'), '.png': ('image', 'png')}.get(ext, ('application', 'octet-stream'))
        msg.add_attachment(open(p, 'rb').read(), maintype=mt[0], subtype=mt[1], filename=os.path.basename(p))
    try:
        _smtp_send(c, msg)
    except smtplib.SMTPAuthenticationError:
        return {'ok': False, 'message': 'Il tuo account email ha rifiutato la password. Con Gmail serve una “password per le app” '
                                        '(Account Google → Sicurezza → Password per le app).', 'need': 'account'}
    except Exception as e:
        return {'ok': False, 'message': 'Email non inviata: %s' % e}
    return {'ok': True, 'to': to}


def mail_test(_=None):
    c = _mail_conf()
    return mail_send({'a': c.get('email'), 'oggetto': 'Prova da Rise',
                      'testo': 'Se leggi questo messaggio, l’Assistente di Rise può inviare email per te.'})


def mail_prepare(a):
    """Bozza dell'email preparata dall'Assistente: indirizzo trovato in rubrica, allegati controllati."""
    to, label = resolve_address(a.get('a') or a.get('destinatario'))
    atts = []
    for p in a.get('allegati') or []:
        p = os.path.expanduser(p)
        if os.path.isfile(p):
            atts.append({'path': p, 'name': os.path.basename(p), 'size': os.path.getsize(p)})
    return {'ok': True, 'to': to, 'label': label, 'oggetto': a.get('oggetto') or '', 'testo': a.get('testo') or '',
            'allegati': atts, 'account': mail_get()}


def mail_thunderbird(a):
    """Senza account configurato: apre Thunderbird con l'email già scritta e l'allegato, l'utente preme Invia."""
    to, _ = resolve_address(a.get('a') or '')
    def q(s):
        return (s or '').replace("'", '’').replace(',', '，')
    parts = ["to='%s'" % (to or ''), "subject='%s'" % q(a.get('oggetto')), "body='%s'" % q(a.get('testo'))]
    atts = [p for p in (a.get('allegati') or []) if os.path.isfile(os.path.expanduser(p))]
    if atts:
        parts.append("attachment='%s'" % ','.join('file://' + os.path.expanduser(p) for p in atts))
    env = dict(os.environ)
    env.pop('LD_PRELOAD', None)
    cmd = ['flatpak', 'run', '--filesystem=home', 'org.mozilla.Thunderbird', '-compose', ','.join(parts)]
    try:
        subprocess.Popen(cmd, start_new_session=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return {'ok': False, 'message': str(e)}
    return {'ok': True}


CMDS = {'auto.rename': rename, 'auto.findDelete': find_for_delete, 'auto.trash': trash, 'auto.mkdir': make_folder, 'auto.find': find_files, 'auto.pdf': make_pdf,
        'contacts.list': contacts, 'contacts.save': contact_save, 'contacts.delete': contact_delete,
        'mail.get': mail_get, 'mail.set': mail_set, 'mail.send': mail_send, 'mail.test': mail_test,
        'mail.prepare': mail_prepare, 'mail.thunderbird': mail_thunderbird}


# ---------------- leggere la posta (IMAP, stesso account usato per inviare) ----------------
IMAP = {'gmail': 'imap.gmail.com', 'outlook': 'outlook.office365.com', 'libero': 'imapmail.libero.it',
        'aruba': 'imaps.aruba.it', 'arubapec': 'imaps.pec.aruba.it', 'tim': 'imap.tim.it', 'yahoo': 'imap.mail.yahoo.com'}


def _imap():
    import imaplib
    c = _mail_conf()
    if not (c.get('email') and c.get('password')):
        raise PermissionError('Prima va collegato il tuo account email (Impostazioni → Assistente).')
    host = c.get('imap') or IMAP.get(c.get('provider', 'gmail'), 'imap.' + c['email'].split('@')[-1])
    m = imaplib.IMAP4_SSL(host, 993, timeout=30)
    m.login(c['email'], c['password'])
    m.select('INBOX', readonly=True)
    return m


def _dec(h):
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(h or '')))
    except Exception:
        return h or ''


def mail_inbox(a):
    import email
    import email.utils
    n = max(1, min(30, int(a.get('quante') or 8)))
    who = _norm(a.get('da') or '')
    try:
        m = _imap()
    except PermissionError as e:
        return {'ok': False, 'message': str(e), 'need': 'account'}
    except Exception as e:
        return {'ok': False, 'message': 'Non riesco a leggere la posta: %s' % e}
    try:
        crit = 'UNSEEN' if a.get('solo_nuove') else 'ALL'
        _, data = m.search(None, crit)
        ids = data[0].split()[-200:]
        out = []
        for i in reversed(ids):
            _, d = m.fetch(i, '(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
            flags = d[0][0].decode(errors='replace')
            h = email.message_from_bytes(d[0][1])
            frm = _dec(h.get('From'))
            if who and who not in _norm(frm):
                continue
            name, addr = email.utils.parseaddr(frm)
            out.append({'id': i.decode(), 'from': name or addr, 'addr': addr, 'subject': _dec(h.get('Subject')) or '(senza oggetto)',
                        'date': h.get('Date', ''), 'unread': '\\Seen' not in flags})
            if len(out) >= n:
                break
        _, u = m.search(None, 'UNSEEN')
        return {'ok': True, 'mails': out, 'unread': len(u[0].split())}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _plain(msg):
    import html as _h
    text, atts = '', []
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        fn = part.get_filename()
        if fn:
            atts.append(_dec(fn))
            continue
        ct = part.get_content_type()
        try:
            body = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', 'replace')
        except Exception:
            continue
        if ct == 'text/plain' and not text:
            text = body
        elif ct == 'text/html' and not text:
            body = re.sub(r'(?is)<(script|style).*?</\1>', ' ', body)
            body = re.sub(r'(?i)<br\s*/?>|</p>|</div>', '\n', body)
            text = _h.unescape(re.sub(r'<[^>]+>', ' ', body))
    text = re.sub(r'[ \t\xa0]+', ' ', text)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text).strip()
    return text, atts


def mail_read(a):
    import email
    import email.utils
    try:
        m = _imap()
    except PermissionError as e:
        return {'ok': False, 'message': str(e), 'need': 'account'}
    except Exception as e:
        return {'ok': False, 'message': 'Non riesco a leggere la posta: %s' % e}
    try:
        i = (a.get('id') or '').encode()
        if not i:
            who = _norm(a.get('da') or '')
            _, data = m.search(None, 'ALL')
            for j in reversed(data[0].split()[-200:]):
                if not who:
                    i = j
                    break
                _, d = m.fetch(j, '(BODY.PEEK[HEADER.FIELDS (FROM)])')
                if who in _norm(_dec(email.message_from_bytes(d[0][1]).get('From'))):
                    i = j
                    break
        if not i:
            return {'ok': False, 'message': 'Non trovo email di “%s”.' % (a.get('da') or '')}
        _, d = m.fetch(i, '(BODY.PEEK[])')
        msg = email.message_from_bytes(d[0][1])
        text, atts = _plain(msg)
        name, addr = email.utils.parseaddr(_dec(msg.get('From')))
        return {'ok': True, 'from': name or addr, 'addr': addr, 'subject': _dec(msg.get('Subject')), 'date': msg.get('Date', ''),
                'text': text[:6000], 'attachments': atts, 'msgid': msg.get('Message-ID', ''),
                'refs': msg.get('References', '')}
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ---------------- leggere i documenti (per riassumerli o tradurli) ----------------
def read_doc(a):
    p = a.get('path')
    if not p:
        r = find_files({'testo': a.get('testo') or '', 'tipo': a.get('tipo') or ''}).get('results') or []
        if not r:
            return {'ok': False, 'message': 'Non trovo il documento.'}
        p = r[0]['path']
    p = os.path.expanduser(p)
    low = p.lower()
    try:
        if low.endswith('.pdf'):
            out = subprocess.run(['pdftotext', '-layout', '-l', '30', p, '-'], capture_output=True, text=True, timeout=60).stdout
        elif low.endswith('.docx'):
            out = _docx_text(p)
        elif low.endswith(('.txt', '.md', '.csv', '.log', '.html', '.htm')):
            out = open(p, errors='replace').read()
        elif low.endswith('.xlsx'):
            with zipfile.ZipFile(p) as z:
                ss = re.findall(r'<t[^>]*>([^<]*)</t>', z.read('xl/sharedStrings.xml').decode('utf8', 'replace')) if 'xl/sharedStrings.xml' in z.namelist() else []
            out = '\n'.join(unescape(x) for x in ss)
        else:
            return {'ok': False, 'message': 'Questo tipo di file non lo so ancora leggere.'}
    except Exception as e:
        return {'ok': False, 'message': 'Non riesco a leggerlo: %s' % e}
    out = re.sub(r'[ \t]+', ' ', out)
    out = re.sub(r'\n\s*\n+', '\n\n', out).strip()
    return {'ok': True, 'path': p, 'name': os.path.basename(p), 'text': out[:7000], 'truncated': len(out) > 7000}


# ---------------- notizie (titoli dai giornali, via RSS) ----------------
FEEDS = {'ansa': ('ANSA', 'https://www.ansa.it/sito/ansait_rss.xml'),
         'repubblica': ('la Repubblica', 'https://www.repubblica.it/rss/homepage/rss2.0.xml'),
         'corriere': ('Corriere della Sera', 'https://xml2.corriereobjects.it/rss/homepage.xml'),
         'sole': ('Il Sole 24 Ore', 'https://www.ilsole24ore.com/rss/italia.xml'),
         'sport': ('ANSA Sport', 'https://www.ansa.it/sito/notizie/sport/sport_rss.xml'),
         'economia': ('ANSA Economia', 'https://www.ansa.it/sito/notizie/economia/economia_rss.xml'),
         'mondo': ('ANSA Mondo', 'https://www.ansa.it/sito/notizie/mondo/mondo_rss.xml'),
         'tecnologia': ('ANSA Tecnologia', 'https://www.ansa.it/sito/notizie/tecnologia/tecnologia_rss.xml')}


def news(a):
    import html as _h
    import urllib.request
    key = _norm(a.get('fonte') or a.get('argomento') or 'ansa')
    k = next((x for x in FEEDS if x in key), 'ansa')
    name, url = FEEDS[k]
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 Rise/0.5'})
        with urllib.request.urlopen(req, timeout=15) as r:
            xml = r.read(2_000_000).decode('utf-8', 'replace')
    except Exception as e:
        return {'ok': False, 'message': 'Non riesco a leggere le notizie: %s' % e}
    items = []
    for it in re.findall(r'<item[ >].*?</item>', xml, re.S)[:12]:
        def g(tag):
            m = re.search(r'<%s[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>' % (tag, tag), it, re.S)
            return _h.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip() if m else ''
        t = g('title')
        if t:
            items.append({'title': t, 'link': g('link'), 'summary': g('description')[:300]})
    return {'ok': True, 'source': name, 'items': items}


# ---------------- Wi-Fi e Bluetooth ----------------
def wifi_join(a):
    """Si collega a una rete Wi-Fi già conosciuta (o con la password data)."""
    ssid = (a.get('rete') or '').strip()
    rc = subprocess.run(['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'], capture_output=True, text=True).stdout
    known = [l.split(':')[0] for l in rc.splitlines() if l.endswith('802-11-wireless')]
    target = next((k for k in known if _norm(k) == _norm(ssid)), None) or next((k for k in known if _norm(ssid) in _norm(k)), None)
    if target and not a.get('password'):
        r = subprocess.run(['nmcli', 'connection', 'up', 'id', target], capture_output=True, text=True, timeout=40)
        return {'ok': r.returncode == 0, 'ssid': target, 'message': (r.stderr or '').strip()[-200:]}
    if not a.get('password'):
        return {'ok': False, 'need': 'password', 'ssid': ssid}
    try:
        import risesys
        return risesys.net_connect({'ssid': ssid, 'password': a['password']})
    except Exception as e:
        return {'ok': False, 'message': str(e)}


def bt_power(a):
    on = bool(a.get('on'))
    subprocess.run(['sudo', '-n', 'rfkill', 'unblock' if on else 'block', 'bluetooth'], capture_output=True, timeout=10)
    r = subprocess.run(['bluetoothctl', 'power', 'on' if on else 'off'], capture_output=True, text=True, timeout=15)
    ok = r.returncode == 0 or (not on)
    return {'ok': ok, 'message': '' if ok else 'Questo computer non ha il Bluetooth, o non è attivo.'}


CMDS.update({'mail.inbox': mail_inbox, 'mail.read': mail_read, 'doc.read': read_doc, 'news.get': news,
             'wifi.join': wifi_join, 'bt.power': bt_power})


# ---------------- archivi: ZIP, RAR, 7z, tar ----------------
def _unique(path):
    base, ext = os.path.splitext(path)
    n, p = 2, path
    while os.path.exists(p):
        p = '%s (%d)%s' % (base, n, ext)
        n += 1
    return p


def compress(a):
    """Comprime file e cartelle in un .zip accanto al primo (Esplora file: "Comprimi")."""
    paths = [os.path.abspath(os.path.expanduser(p)) for p in (a.get('paths') or []) if os.path.exists(os.path.expanduser(p))]
    if not paths and a.get('testo'):
        c = _candidates(a['testo'], a.get('tipo') or '')
        paths = [c[0]['path']] if c else []
    if not paths:
        return {'ok': False, 'message': 'Non trovo cosa comprimere.'}
    name = (a.get('nome') or (os.path.basename(paths[0]) if len(paths) == 1 else 'Archivio')).strip()
    name = re.sub(r'[\\/:*?"<>|]+', ' ', os.path.splitext(name)[0] if not os.path.isdir(paths[0]) and len(paths) == 1 else name)
    out = _unique(os.path.join(os.path.dirname(paths[0]), name + '.zip'))
    count = 0
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in paths:
            root = os.path.dirname(p)
            if os.path.isdir(p):
                for dp, dns, fns in os.walk(p):
                    for f in fns:
                        full = os.path.join(dp, f)
                        z.write(full, os.path.relpath(full, root))
                        count += 1
            else:
                z.write(p, os.path.basename(p))
                count += 1
    return {'ok': True, 'path': out, 'name': os.path.basename(out), 'files': count, 'size': os.path.getsize(out),
            'folder': os.path.basename(os.path.dirname(out))}


def extract(a):
    """Estrae un archivio in una cartella col suo nome, accanto all'archivio. Mai fuori da quella cartella."""
    src = os.path.abspath(os.path.expanduser(a.get('path') or ''))
    if not os.path.isfile(src) and a.get('testo'):
        c = [x for x in _candidates(a['testo'], 'file') if re.search(r'\.(zip|rar|7z|tar|gz|tgz|bz2|xz)$', x['name'], re.I)]
        src = c[0]['path'] if c else ''
    if not os.path.isfile(src):
        return {'ok': False, 'message': 'Non trovo l’archivio.'}
    low = src.lower()
    stem = re.sub(r'\.(tar\.gz|tar\.bz2|tar\.xz|zip|rar|7z|tgz|tar)$', '', os.path.basename(src), flags=re.I)
    dest = _unique(os.path.join(os.path.dirname(src), stem))
    os.makedirs(dest)
    try:
        if low.endswith('.zip'):
            with zipfile.ZipFile(src) as z:
                if a.get('password'):
                    z.setpassword(a['password'].encode())
                for m in z.infolist():
                    target = os.path.realpath(os.path.join(dest, m.filename))
                    if not target.startswith(os.path.realpath(dest) + os.sep) and target != os.path.realpath(dest):
                        continue                               # nomi pericolosi ("../"): si saltano
                    z.extract(m, dest)
        elif re.search(r'\.(tar|tgz|tar\.gz|tar\.bz2|tar\.xz)$', low):
            import tarfile
            with tarfile.open(src) as t:
                try:
                    t.extractall(dest, filter='data')
                except TypeError:
                    t.extractall(dest, members=[m for m in t.getmembers() if not (m.name.startswith('/') or '..' in m.name.split('/'))])
        else:                                                  # .rar, .7z e altri: 7-Zip
            exe = next((x for x in ('7z', '7zz', '7za') if subprocess.run(['sh', '-c', 'command -v ' + x], capture_output=True).returncode == 0), None)
            if not exe:
                os.rmdir(dest)
                return {'ok': False, 'message': 'Per aprire questo tipo di archivio manca 7-Zip.'}
            r = subprocess.run([exe, 'x', '-y', '-o' + dest, '-p' + (a.get('password') or ''), src], capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                if 'Wrong password' in r.stderr + r.stdout or 'password' in (r.stderr + r.stdout).lower():
                    shutil.rmtree(dest, ignore_errors=True)
                    return {'ok': False, 'need': 'password', 'message': 'Questo archivio è protetto da password.'}
                raise RuntimeError((r.stderr or r.stdout).strip()[-200:])
    except RuntimeError as e:
        if 'password' in str(e).lower() or 'encrypted' in str(e).lower():
            shutil.rmtree(dest, ignore_errors=True)
            return {'ok': False, 'need': 'password', 'message': 'Questo archivio è protetto da password.'}
        return {'ok': False, 'message': 'Non riesco ad aprire l’archivio: %s' % e}
    except Exception as e:
        return {'ok': False, 'message': 'Non riesco ad aprire l’archivio: %s' % e}
    # un'unica cartella dentro: si "toglie un livello" per non avere prova/prova/…
    inner = os.listdir(dest)
    if len(inner) == 1 and os.path.isdir(os.path.join(dest, inner[0])):
        tmp = dest + '.rise-tmp'
        os.rename(os.path.join(dest, inner[0]), tmp)
        os.rmdir(dest)
        if not os.path.exists(os.path.join(os.path.dirname(dest), inner[0])):
            dest = os.path.join(os.path.dirname(dest), inner[0])      # si tiene il nome della cartella originale
        os.rename(tmp, dest)
    n = sum(len(f) for _, _, f in os.walk(dest))
    return {'ok': True, 'path': dest, 'name': os.path.basename(dest), 'files': n, 'folder': os.path.basename(os.path.dirname(dest))}


CMDS.update({'arch.compress': compress, 'arch.extract': extract})
