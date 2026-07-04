# PyInstaller spec for the macOS .app bundle.
# Build from the repo root:
#   .venv/bin/pyinstaller packaging/VideoLibrarySearch.spec --noconfirm

import os

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(SPECPATH), "."))

datas = [(os.path.join(ROOT, "ui"), "ui")]
binaries = []
hiddenimports = []

# transformers resolves models/configs dynamically; static analysis misses them.
hiddenimports += collect_submodules("transformers.models.siglip")
hiddenimports += collect_submodules("transformers.models.siglip2")
hiddenimports += collect_submodules("transformers.models.auto")
datas += collect_data_files("transformers")
# version metadata consulted at import time
for pkg in ("transformers", "tokenizers", "safetensors", "huggingface-hub",
            "regex", "requests", "packaging", "filelock", "numpy", "pyyaml",
            "tqdm", "torch"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

# sqlite-vec ships its loadable extension inside the package
datas += collect_data_files("sqlite_vec")
binaries += collect_dynamic_libs("sqlite_vec")

# uvicorn's loop/protocol classes are chosen by string name at runtime
hiddenimports += collect_submodules("uvicorn")

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "playwright"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="VideoLibrarySearch",
    console=False,
    target_arch="arm64",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="VideoLibrarySearch",
)

app = BUNDLE(
    coll,
    name="Video Library Search.app",
    icon=os.path.join(ROOT, "packaging", "AppIcon.icns"),
    bundle_identifier="io.writecraft.videolibrarysearch",
    info_plist={
        "CFBundleName": "Video Library Search",
        "CFBundleDisplayName": "Video Library Search",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.video",
        "NSAppleEventsUsageDescription":
            "Used to open videos in QuickTime Player at the matched timestamp.",
        # regular Dock app (not LSUIElement)
    },
)
