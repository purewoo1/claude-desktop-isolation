"""Host-side size-only bridge for one Xephyr window and its Claude client.

Reads window IDs/classes, the fixed Xephyr title, state and geometry only:
no application titles, clipboard, pixels,
keyboard or application IPC. Host X credentials never enter the sandbox.
"""
import argparse
import ctypes as C
import os
import subprocess
import time


X = C.CDLL('libX11.so.6')
P = C.c_void_p
W = C.c_ulong


class ClientMessage(C.Structure):
    _fields_ = [('type', C.c_int), ('serial', W), ('send_event', C.c_int),
                ('display', P), ('window', W), ('message_type', W),
                ('format', C.c_int), ('data', C.c_long * 5)]


class Event(C.Union):
    _fields_ = [('client', ClientMessage), ('pad', C.c_long * 24)]


for name, args, result in [
    ('XOpenDisplay', [C.c_char_p], P),
    ('XDefaultRootWindow', [P], W),
    ('XInternAtom', [P, C.c_char_p, C.c_int], W),
    ('XGetWindowProperty', [P, W, W, C.c_long, C.c_long, C.c_int, W,
                           C.POINTER(W), C.POINTER(C.c_int), C.POINTER(W),
                           C.POINTER(W), C.POINTER(P)], C.c_int),
    ('XGetGeometry', [P, W, C.POINTER(W), C.POINTER(C.c_int), C.POINTER(C.c_int),
                     C.POINTER(C.c_uint), C.POINTER(C.c_uint),
                     C.POINTER(C.c_uint), C.POINTER(C.c_uint)], C.c_int),
    ('XSendEvent', [P, W, C.c_int, C.c_long, C.POINTER(Event)], C.c_int),
    ('XChangeProperty', [P, W, W, W, C.c_int, C.c_int, P, C.c_int], C.c_int),
    ('XFlush', [P], C.c_int), ('XFree', [P], C.c_int),
    ('XCloseDisplay', [P], C.c_int),
]:
    function = getattr(X, name)
    function.argtypes, function.restype = args, result

# A client may close between enumeration and a property request.
ERROR = C.CFUNCTYPE(C.c_int, P, P)
ignore_stale_window = ERROR(lambda display, error: 0)
X.XSetErrorHandler.argtypes = [ERROR]
X.XSetErrorHandler(ignore_stale_window)


class Display:
    def __init__(self, name, authority):
        self.name, self.authority = name, authority
        old = os.environ.get('XAUTHORITY')
        os.environ['XAUTHORITY'] = authority
        try:
            self.handle = X.XOpenDisplay(name.encode())
        finally:
            if old is None:
                os.environ.pop('XAUTHORITY', None)
            else:
                os.environ['XAUTHORITY'] = old
        if not self.handle:
            raise RuntimeError('Window geometry display unavailable.')
        self.root = X.XDefaultRootWindow(self.handle)
        self.atoms = {}

    def atom(self, name):
        if name not in self.atoms:
            self.atoms[name] = X.XInternAtom(self.handle, name.encode(), 0)
        return self.atoms[name]

    def prop(self, window, name):
        actual, fmt, count, remaining, data = W(), C.c_int(), W(), W(), P()
        status = X.XGetWindowProperty(self.handle, window, self.atom(name),
                                     0, 1024, 0, 0, C.byref(actual), C.byref(fmt),
                                     C.byref(count), C.byref(remaining), C.byref(data))
        try:
            if status or not data.value:
                return ()
            if fmt.value == 32:
                return tuple(C.cast(data, C.POINTER(W))[i] for i in range(count.value))
            if fmt.value == 8:
                return C.string_at(data, count.value)
            return ()
        finally:
            if data.value:
                X.XFree(data)

    def size(self, window):
        root, x, y, width, height, border, depth = W(), C.c_int(), C.c_int(), C.c_uint(), C.c_uint(), C.c_uint(), C.c_uint()
        if X.XGetGeometry(self.handle, window, C.byref(root), C.byref(x), C.byref(y),
                          C.byref(width), C.byref(height), C.byref(border), C.byref(depth)):
            return width.value, height.value
        return None

    def message(self, window, name, values):
        event = Event()
        event.client.type, event.client.send_event = 33, 1
        event.client.display, event.client.window = self.handle, window
        event.client.message_type, event.client.format = self.atom(name), 32
        for i, value in enumerate(values):
            event.client.data[i] = value
        X.XSendEvent(self.handle, self.root, 0, (1 << 20) | (1 << 19), C.byref(event))
        X.XFlush(self.handle)

    def resize(self, window, size, position=False):
        # EWMH StaticGravity; source=pager. Only change position inside Xephyr.
        flags = 10 | (3 << 10) | (2 << 12) | ((3 << 8) if position else 0)
        self.message(window, '_NET_MOVERESIZE_WINDOW', [flags, 0, 0, *size])

    def maximized(self, window):
        return self.atom('_NET_WM_STATE_MAXIMIZED_VERT') in self.prop(window, '_NET_WM_STATE')

    def maximize(self, window, enabled):
        self.message(window, '_NET_WM_STATE', [int(enabled),
                     self.atom('_NET_WM_STATE_MAXIMIZED_VERT'),
                     self.atom('_NET_WM_STATE_MAXIMIZED_HORZ'), 2, 0])

    def refresh_input_bounds(self, size):
        # Host ConfigureNotify resizes Xephyr's pixels but does not update all
        # DIX desktop/input extents. The standard RandR 1.0 set-config path does.
        # Reapply the CURRENT mode; do not change resolution or touch host X.
        width, height = size
        if not (1 <= width <= 8192 and 1 <= height <= 8192):
            return False
        try:
            result = subprocess.run(['/usr/bin/xrandr', '--display', self.name,
                                     '--size', f'{width}x{height}'],
                                    env={'PATH': '/usr/bin:/bin', 'DISPLAY': self.name,
                                         'XAUTHORITY': self.authority},
                                    capture_output=True, check=False, timeout=3)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            # Xephyr replaces dynamic modes during an interactive host resize.
            # A just-observed mode may already be gone. Keep the helper alive;
            # the bridge will retry the newest stable dimensions instead.
            return False

    def dark(self, window):
        value = C.create_string_buffer(b'dark')
        X.XChangeProperty(self.handle, window, self.atom('_GTK_THEME_VARIANT'),
                          self.atom('UTF8_STRING'), 8, 0, value, 4)
        X.XFlush(self.handle)

    def close(self):
        X.XCloseDisplay(self.handle)


class GeometryBridge:
    def __init__(self, host, inner, outer_window, app_class=b'com.anthropic.Claude'):
        self.host, self.inner, self.outer = host, inner, outer_window
        self.app_class = app_class
        self.client = None
        self.previous_root = self.previous_size = self.previous_max = None
        self.pending_root = None
        self.pending_since = self.next_bounds_retry = 0
        self.settle_until = 0
        self.host.dark(self.outer)

    def tick(self):
        clients = self.inner.prop(self.inner.root, '_NET_CLIENT_LIST')
        if self.client not in clients:
            normal = self.inner.atom('_NET_WM_WINDOW_TYPE_NORMAL')
            self.client = next((w for w in clients
                                if self.app_class in (self.inner.prop(w, 'WM_CLASS') or b'').split(b'\0')
                                and normal in self.inner.prop(w, '_NET_WM_WINDOW_TYPE')), None)
            self.previous_root = self.previous_size = self.previous_max = None
            self.pending_root = None
            self.pending_since = self.next_bounds_retry = 0
        if not self.client:
            return
        root = self.inner.size(self.inner.root)
        size = self.inner.size(self.client)
        if not root or not size:
            return
        maximized = self.inner.maximized(self.client)
        now = time.monotonic()
        if self.host.size(self.outer) != root:
            # The outer WM can resize before Xephyr publishes the new root.
            # Never treat old inner pixels/input extents as a settled window.
            self.settle_until = now + 0.30
            return
        if root != self.pending_root:
            self.pending_root, self.pending_since = root, now
            self.next_bounds_retry = 0
            self.settle_until = max(self.settle_until, now + 0.30)
        if self.previous_root is None or root != self.previous_root:
            # Coalesce rapid host ConfigureNotify events. Do not run a RandR
            # subprocess for every transient mode while the user drags an edge.
            if now - self.pending_since < 0.20 or now < self.next_bounds_retry:
                return
            if not self.inner.refresh_input_bounds(root):
                self.next_bounds_retry = now + 0.30
                self.settle_until = self.next_bounds_retry + 0.10
                return
            if self.inner.size(self.inner.root) != root:
                # The host resized again while xrandr was in flight.
                self.pending_root = None
                self.settle_until = now + 0.30
                return
            if maximized:
                self.inner.maximize(self.client, False)
            self.inner.resize(self.client, root, position=True)
            self.settle_until = now + 0.4
        elif now >= self.settle_until:
            if maximized and not self.previous_max:
                # Claude's own maximize/restore button controls the real window.
                self.host.maximize(self.outer, not self.host.maximized(self.outer))
                # Xephyr/Openbox retains stale maximized monitor bounds after a
                # framebuffer resize. Keep the inner client normal and fitted;
                # maximize belongs to the actual host window, not a second desktop.
                self.inner.maximize(self.client, False)
                self.inner.resize(self.client, root, position=True)
                self.settle_until = now + 0.4
            elif size != self.previous_size and size != root:
                # Inner resize -> outer resize; no padded private desktop left.
                target = (max(640, min(8192, size[0])), max(480, min(8192, size[1])))
                self.host.resize(self.outer, target)
                self.inner.resize(self.client, target, position=True)
                self.settle_until = now + 0.4
            elif size != root:
                self.inner.resize(self.client, root, position=True)
                self.settle_until = now + 0.35
        self.previous_root, self.previous_size, self.previous_max = root, size, maximized


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--display', required=True)
    parser.add_argument('--authority', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--app-class', default='com.anthropic.Claude')
    args = parser.parse_args()
    host = Display(os.environ['DISPLAY'], os.environ.get('XAUTHORITY') or os.path.expanduser('~/.Xauthority'))
    inner = Display(args.display, args.authority)
    try:
        outer = None
        for _ in range(100):
            outer = next((w for w in host.prop(host.root, '_NET_CLIENT_LIST')
                          if b'Xephyr' in (host.prop(w, 'WM_CLASS') or b'').split(b'\0')
                          and host.prop(w, 'WM_NAME') == args.title.encode()), None)
            if outer:
                break
            time.sleep(0.1)
        if not outer:
            raise RuntimeError('Claude outer window unavailable for size synchronization.')
        bridge = GeometryBridge(host, inner, outer, args.app_class.encode())
        while outer in host.prop(host.root, '_NET_CLIENT_LIST'):
            bridge.tick()
            time.sleep(0.1)
    finally:
        inner.close()
        host.close()


if __name__ == '__main__':
    main()
