"""Rise – piccoli aiuti per le finestre sul desktop Cinnamon (X11).

GTK 4 non sa più dire al gestore delle finestre "tienila sotto le altre", "non metterla nella barra",
"mettila in quell'angolo": si fa qui, direttamente con le proprietà standard X11 (EWMH).
"""
try:
    from Xlib import X, Xatom, display as xdisplay, protocol
except ImportError:          # python3-xlib non installato: le finestre funzionano lo stesso, senza rifiniture
    X = None

_d = None


def _dpy():
    global _d
    if _d is None and X is not None:
        try:
            _d = xdisplay.Display()
        except Exception:
            _d = False
    return _d or None


def available():
    return _dpy() is not None


def xid_of(gtk_window):
    try:
        import gi
        gi.require_version('GdkX11', '4.0')
        from gi.repository import GdkX11
        surf = gtk_window.get_surface()
        if surf is not None and isinstance(surf, GdkX11.X11Surface):
            return surf.get_xid()
    except Exception:
        pass
    return None


def _atom(name):
    return _dpy().intern_atom(name)


def screen_size():
    d = _dpy()
    if not d:
        return 1920, 1080
    s = d.screen()
    return s.width_in_pixels, s.height_in_pixels


def workarea():
    """Spazio libero dello schermo (senza la barra di Cinnamon): x, y, larghezza, altezza."""
    d = _dpy()
    if not d:
        return 0, 0, 1920, 1080
    try:
        p = d.screen().root.get_full_property(_atom('_NET_WORKAREA'), Xatom.CARDINAL)
        if p and len(p.value) >= 4:
            return tuple(int(v) for v in p.value[:4])
    except Exception:
        pass
    w, h = screen_size()
    return 0, 0, w, h


def prepare(xid, wm_class=None, kind=None, states=(), sticky=False):
    """Da chiamare prima che la finestra compaia (segnale 'realize').
    kind: 'utility', 'notification', 'dialog'…  states: 'below', 'above', 'skip_taskbar', 'skip_pager'."""
    d = _dpy()
    if not d or not xid:
        return
    w = d.create_resource_object('window', xid)
    try:
        if wm_class:
            w.set_wm_class(wm_class[0], wm_class[1])
        if kind:
            w.change_property(_atom('_NET_WM_WINDOW_TYPE'), Xatom.ATOM, 32,
                              [_atom('_NET_WM_WINDOW_TYPE_' + kind.upper())])
        names = ['_NET_WM_STATE_' + s.upper() for s in states]
        if sticky:
            names.append('_NET_WM_STATE_STICKY')
        if names:
            w.change_property(_atom('_NET_WM_STATE'), Xatom.ATOM, 32, [_atom(n) for n in names])
        if sticky:
            w.change_property(_atom('_NET_WM_DESKTOP'), Xatom.CARDINAL, 32, [0xFFFFFFFF])
        d.flush()
    except Exception:
        pass


def set_states(xid, states, on=True):
    """Dopo che la finestra è comparsa: cambia gli stati con i messaggi al gestore delle finestre."""
    d = _dpy()
    if not d or not xid:
        return
    root = d.screen().root
    for s in states:
        ev = protocol.event.ClientMessage(
            window=d.create_resource_object('window', xid), client_type=_atom('_NET_WM_STATE'),
            data=(32, [1 if on else 0, _atom('_NET_WM_STATE_' + s.upper()), 0, 1, 0]))
        root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
    d.flush()


def move(xid, x, y):
    d = _dpy()
    if not d or not xid:
        return
    try:
        w = d.create_resource_object('window', xid)
        # chiede al gestore delle finestre di spostarla (rispettando la sua cornice)
        ev = protocol.event.ClientMessage(window=w, client_type=_atom('_NET_MOVERESIZE_WINDOW'),
                                          data=(32, [(1 << 8) | (1 << 9) | 1 | (2 << 12), int(x), int(y), 0, 0]))
        d.screen().root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        w.configure(x=int(x), y=int(y))
        d.flush()
    except Exception:
        pass


def set_icon(xid, png_path):
    """Icona della finestra (barra delle applicazioni, Alt+Tab) da un file PNG."""
    d = _dpy()
    if not d or not xid:
        return
    try:
        from PIL import Image
        im = Image.open(png_path).convert('RGBA').resize((48, 48))
        data = [48, 48]
        for r, g, b, a in im.getdata():
            data.append((a << 24) | (r << 16) | (g << 8) | b)
        w = d.create_resource_object('window', xid)
        w.change_property(_atom('_NET_WM_ICON'), Xatom.CARDINAL, 32, data)
        d.flush()
    except Exception:
        pass
