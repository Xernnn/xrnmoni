# PyInstaller spec for xrnmoni-agent.exe: the agent with Python built in, so the PC it records
# needs no Python. Windowed (console=False): no console window, ever; windows\launcher.py
# sends its output to %LOCALAPPDATA%\xrnmoni\logs instead. One folder, not one file: a
# one-file exe unpacks itself to %TEMP% at every start, runs as two processes, and is the
# kind antivirus programs flag most.
#
# Build with windows\build-exe.ps1, which writes dist\xrnmoni-agent\xrnmoni-agent.exe.
import os

ROOT = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[os.path.join(ROOT, "agent")],
    hiddenimports=["agent", "platform_win"],
    excludes=["platform_linux", "Xlib", "tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="xrnmoni-agent",
    console=False,
    icon=os.path.join(ROOT, "extension", "icons", "icon128.png"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="xrnmoni-agent")
