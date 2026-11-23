"""User-authorized two-way text/image clipboard, with no persistence/logging.

Only CLIPBOARD is synchronized, not mouse-selection PRIMARY or file operations.
The host X connection stays in this host helper, never in the application.
Existing clipboard content is not imported until the next copy operation.
"""
import argparse
import hashlib
import os

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gtk


class ClipboardBridge:
    def __init__(self, first, second):
        self.clipboards = [Gtk.Clipboard.get_for_display(d, Gdk.SELECTION_CLIPBOARD)
                           for d in [first, second]]
        self.signatures = [None, None]
        self.generation = [0, 0]
        for index, clipboard in enumerate(self.clipboards):
            clipboard.connect('owner-change', self.changed, index)

    def changed(self, clipboard, event, index):
        self.generation[index] += 1
        generation = self.generation[index]
        GLib.timeout_add(80, self.fetch, index, generation)

    def fetch(self, index, generation):
        if generation == self.generation[index]:
            self.clipboards[index].request_targets(self.targets, (index, generation))
        return False

    def targets(self, clipboard, atoms, count, state):
        index, generation = state
        if generation != self.generation[index]:
            return
        if atoms and Gtk.targets_include_image(atoms, False):
            clipboard.request_image(self.image, state)
        elif atoms and Gtk.targets_include_text(atoms):
            clipboard.request_text(self.text, state)

    def forward(self, index, generation, signature, setter):
        if generation != self.generation[index] or self.signatures[index] == signature:
            return
        self.signatures[index] = self.signatures[1 - index] = signature
        setter(self.clipboards[1 - index])

    def text(self, clipboard, text, state):
        if text is not None and len(text.encode()) <= 2 * 1024 * 1024:
            signature = ('text', hashlib.sha256(text.encode()).digest())
            self.forward(*state, signature, lambda other: other.set_text(text, -1))

    def image(self, clipboard, pixbuf, state):
        if pixbuf and pixbuf.get_width() * pixbuf.get_height() <= 8_000_000:
            pixels = pixbuf.get_pixels()
            row_bytes = pixbuf.get_width() * pixbuf.get_n_channels()
            canonical = b''.join(pixels[y * pixbuf.get_rowstride():y * pixbuf.get_rowstride() + row_bytes]
                                 for y in range(pixbuf.get_height()))
            signature = ('image', pixbuf.get_width(), pixbuf.get_height(),
                         hashlib.sha256(canonical).digest())
            self.forward(*state, signature, lambda other: other.set_image(pixbuf))


def open_display(name, authority):
    previous = os.environ.get('XAUTHORITY')
    os.environ['XAUTHORITY'] = authority
    try:
        display = Gdk.Display.open(name)
    finally:
        if previous is None:
            os.environ.pop('XAUTHORITY', None)
        else:
            os.environ['XAUTHORITY'] = previous
    if display is None:
        raise RuntimeError('Clipboard display unavailable.')
    manager = Gdk.DisplayManager.get()
    if manager.get_default_display() is None:
        manager.set_default_display(display)
    Gtk.init_check([])
    return display


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # GTK consumes its standard --display option during import/initialization.
    # Use a private option name so it cannot select the inner display before
    # the matching authority has been installed by open_display().
    parser.add_argument('--inner-display', dest='display', required=True)
    parser.add_argument('--authority', required=True)
    args = parser.parse_args()
    host = open_display(os.environ['DISPLAY'], os.environ.get('XAUTHORITY') or os.path.expanduser('~/.Xauthority'))
    inner = open_display(args.display, args.authority)
    bridge = ClipboardBridge(host, inner)
    host.connect('closed', lambda *args: Gtk.main_quit())
    inner.connect('closed', lambda *args: Gtk.main_quit())
    Gtk.main()


if __name__ == '__main__':
    main()
