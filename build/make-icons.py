#!/usr/bin/env python3
"""Rise – icone delle app di Rise per il menu di Cinnamon (dalle icone dentro desktop/index.html).
Uso: build/make-icons.py <cartella di destinazione share>  (crea icons/hicolor/... e icons/Rise/...)"""
import base64, json, os, re, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1]
s = open(os.path.join(ROOT, 'desktop/index.html'), encoding='utf-8').read()
i = s.index('const ICO={')
j = s.index('};', i)
ico = json.loads(s[i + 10:j + 1])
APPS = {'assistant': 'assistant', 'store': 'store', 'installer': 'install', 'nettools': 'radar', 'settings': 'settings',
        'viewer': 'viewer', 'notes': 'notes', 'calc': 'calc', 'welcome': 'orb', 'figure': 'assistant'}
def svg(name):
    own = os.path.join(ROOT, 'build/icons', name + '.svg')
    if os.path.exists(own):
        return open(own, 'rb').read()
    if name not in ico:
        m = re.search(r'<symbol id="i-%s"([^>]*)>(.*?)</symbol>' % re.escape(name), s, re.S)
        if not m:
            raise SystemExit('icona mancante: ' + name)
        vb = re.search(r'viewBox="([^"]+)"', m.group(1))
        defs = ''.join(re.findall(r'<linearGradient.*?</linearGradient>|<radialGradient.*?</radialGradient>', s, re.S)) if 'url(#' in m.group(2) else ''
        return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="48" height="48" viewBox="%s"><defs>%s</defs>%s</svg>'
                % (vb.group(1) if vb else '0 0 48 48', defs, m.group(2))).encode()
    v = ico[name]
    m = re.match(r'data:image/svg\+xml;base64,(.*)', v)
    if m:
        return base64.b64decode(m.group(1))
    m = re.match(r'data:image/svg\+xml(?:;utf8)?,(.*)', v)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1)).encode()
    raise SystemExit('icona non SVG: ' + name)
sc = os.path.join(OUT, 'icons/hicolor/scalable/apps'); os.makedirs(sc, exist_ok=True)
for app, name in APPS.items():
    open(os.path.join(sc, 'rise-%s.svg' % app), 'wb').write(svg(name))
# il pulsante del menu Start: il cerchio di Rise
open(os.path.join(sc, 'rise-orb.svg'), 'wb').write(svg('orb'))
print('icone:', len(APPS) + 1)
