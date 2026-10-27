import os
import platform
import socket
import subprocess
import shutil

def get_hostname() -> str:
    return socket.gethostname()

def get_os() -> str:
    name = platform.system()
    return "windows" if name == "Windows" else name

def _normalize_arch(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    v = value.strip().lower()
    if v in ("arm64", "aarch64", "arm64_8a"):
        return "arm64"
    if v in ("amd64", "x86_64", "x64", "x86-64"):
        return "amd64"
    if v in ("386", "i386", "i686", "x86"):
        return "386"
    return None

def _windows_native_arch() -> str | None:
    """Native Windows machine arch, independent of the calling process's emulation.

    IsWow64Process2 reports the OS's real machine even to an emulated x64
    Python under Prism; the HKLM Session Manager\\Environment value is the
    machine-wide arch written at setup. Returns None when neither is
    available (non-Windows, or restricted environments).
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.IsWow64Process2.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT)]
        kernel32.IsWow64Process2.restype = wintypes.BOOL
        process, native = wintypes.USHORT(), wintypes.USHORT()
        if kernel32.IsWow64Process2(kernel32.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
            norm = {0xAA64: "arm64", 0x8664: "amd64", 0x014C: "386"}.get(native.value)
            if norm:
                return norm
    except Exception:
        pass
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "PROCESSOR_ARCHITECTURE")
            return _normalize_arch(value)
    except Exception:
        return None


def get_arch() -> str:
    """Normalized CPU arch (arm64/amd64), native even under x64 emulation on ARM64 Windows.

    On Windows, native detection (IsWow64Process2 / registry) and
    platform.machine() are preferred: a native ARM64 Python inherits a WOW64
    PROCESSOR_ARCHITECTURE=AMD64 environment, so env vars are only the last
    fallback (W6432 first, the emulation marker, then the emulated value).
    Returns "unknown" when nothing normalizes.
    """
    if platform.system() == "Windows":
        try:
            norm = _windows_native_arch()
            if norm:
                return norm
        except Exception:
            pass
        try:
            norm = _normalize_arch(platform.machine())
            if norm:
                return norm
        except Exception:
            pass
        for key in ("PROCESSOR_ARCHITEW6432", "PROCESSOR_ARCHITECTURE"):
            try:
                norm = _normalize_arch(os.environ.get(key, ""))
                if norm:
                    return norm
            except Exception:
                pass
        return "unknown"
    try:
        norm = _normalize_arch(platform.machine())
        if norm:
            return norm
    except Exception:
        pass
    return "unknown"

def get_network() -> str:
    return "default"

def get_roles() -> list:
    return ["work"]

def _get_git_sha(path: str) -> str:
    try:
        res = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception:
        return "unknown"

def get_capabilities() -> dict:
    capabilities = {}
    
    # Check launcher
    launcher_path = shutil.which("agent-quota-launcher")
    if launcher_path:
        capabilities["launcher"] = True
        try:
            res = subprocess.run(["agent-quota-launcher", "--version"], capture_output=True, text=True)
            capabilities["launcher_version"] = res.stdout.strip()
        except Exception:
            pass
    
    # Check aplexer
    if shutil.which("aplexer"):
        capabilities["aplexer"] = True
    
    return capabilities

def get_system_info() -> dict:
    return {
        "hostname": get_hostname(),
        "os": get_os(),
        "arch": get_arch(),
        "network": get_network(),
        "roles": get_roles(),
        "capabilities": get_capabilities()
    }
