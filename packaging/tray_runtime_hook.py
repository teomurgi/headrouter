"""PyInstaller runtime hook for the tray binary.

The frozen tray interpreter is Python 3.14, matching the system python3-gi
bindings. PyInstaller's frozen importer does not search system dist-packages
by default, so prepend it to sys.path so ``import gi`` resolves to the OS
GObject introspection bindings (installed via the .deb Depends:).
"""

import glob
import os
import sys
import sysconfig

# Only meaningful on Linux; harmless elsewhere.
if sys.platform.startswith("linux"):
    candidates = [
        os.path.join(sysconfig.get_path("stdlib", vars={"base": "/usr"}), "dist-packages"),
        "/usr/lib/python3/dist-packages",
        "/usr/local/lib/python3/dist-packages",
    ]
    # Also handle versioned locations like /usr/lib/python3.14/dist-packages.
    candidates += glob.glob("/usr/lib/python3*/dist-packages")
    candidates += glob.glob("/usr/local/lib/python3*/dist-packages")

    for path in candidates:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
