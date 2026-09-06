# PyInstaller recipe for the released exe.
#
#     pip install pyinstaller
#     pyinstaller VoidBuster.spec
#
# One file, no Python needed on the target machine. Built as a console
# application deliberately: the headless viewer is half the tool. The window
# hides that console itself when the exe is double-clicked (see voidbuster.py).

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("imgui_bundle")

# imgui_bundle ships ~8MB of C++ and Python demos. They are a fine thing for
# the library to carry and no reason for every download of this tool to.
DROP = ("demos_cpp", "demos_python", "demos_assets")
datas = [d for d in datas
         if not any(part in d[1].replace("\\", "/").split("/") for part in DROP)]

# Default profiles travel inside the build; paths.py also looks for a profiles
# folder beside the exe, which is where anything the user adds should go.
# *.local.json is somebody's own game and has no business inside a release, so
# the folder is listed file by file rather than wholesale.
import glob as _glob
datas += [(_p, "profiles") for _p in _glob.glob("profiles/*.json")
          if not _p.endswith(".local.json")]

a = Analysis(
    ["voidbuster.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here is imported by the tool; excluding them keeps the download
    # to roughly what the GUI toolkit actually costs.
    excludes=["tkinter", "unittest", "pydoc_data", "test", "lib2to3",
              "pip", "setuptools", "email", "html", "xml", "PIL",
              "matplotlib", "numpy", "pandas", "scipy", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VoidBuster",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    icon="docs/voidbuster.ico" if __import__("os").path.exists("docs/voidbuster.ico") else None,
)
