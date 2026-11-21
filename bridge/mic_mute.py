# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Mute and unmute the default microphone: what the right CyberFinger's pink button does when set to it
(VR mode, pink_button.py).

The mute is the capture device's own, so it holds for every app at once (Resonite, VRChat, Discord: they record
from the default device, which SteamVR can switch to the headset's microphone).

Windows: Core Audio through ctypes, so no package is needed. Both default capture devices are set, the console one
and the communications one, when they differ.

Linux: the default source of PipeWire (wpctl, WirePlumber's tool) or, where that isn't there or can't reach its
server, of PulseAudio (pactl), so no package is needed either.
"""

import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as wt

    _CLSCTX_ALL = 23
    _E_CAPTURE = 1
    _ROLES = (0, 2)                  # eConsole, eCommunications


    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


    def _guid(text):
        g = _GUID()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g


    def _method(obj, index, *argtypes):
        """COM method `index` of the interface pointer `obj` (HRESULT-returning: failures raise OSError)."""
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])


    def _release(obj):
        if obj:
            vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


    def _with_endpoints(fn):
        """Call fn(volume) for each default capture device's IAudioEndpointVolume (each device once); returns the
        results in role order. [] without a microphone."""
        ole32 = ctypes.oledll.ole32
        try:
            ole32.CoInitializeEx(None, 0)            # COINIT_MULTITHREADED, this thread
        except OSError:
            pass                                     # already initialised in another mode: usable as it is
        enum = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(_guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")), None, _CLSCTX_ALL,
                               ctypes.byref(_guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")), ctypes.byref(enum))
        results, seen = [], set()
        try:
            for role in _ROLES:
                dev, vol, dev_id = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_wchar_p()
                try:
                    _method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                        enum, _E_CAPTURE, role, ctypes.byref(dev))              # GetDefaultAudioEndpoint
                except OSError:
                    continue                                                    # no device for this role
                try:
                    _method(dev, 5, ctypes.POINTER(ctypes.c_wchar_p))(dev, ctypes.byref(dev_id))   # GetId
                    key = dev_id.value
                    ole32.CoTaskMemFree(dev_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    _method(dev, 3, ctypes.POINTER(_GUID), wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                        dev, ctypes.byref(_guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")), _CLSCTX_ALL, None,
                        ctypes.byref(vol))                                      # Activate(IAudioEndpointVolume)
                    results.append(fn(vol))
                finally:
                    _release(vol)
                    _release(dev)
        finally:
            _release(enum)
        return results


    def _get_mute(vol):
        muted = wt.BOOL()
        _method(vol, 15, ctypes.POINTER(wt.BOOL))(vol, ctypes.byref(muted))     # GetMute
        return bool(muted.value)


    def is_muted():
        """The default microphone's mute, or None without one."""
        states = _with_endpoints(_get_mute)
        return states[0] if states else None


    def set_muted(muted):
        """Mute (True) or unmute every default microphone. False without one."""
        return bool(_with_endpoints(
            lambda vol: _method(vol, 14, wt.BOOL, ctypes.c_void_p)(vol, 1 if muted else 0, None)))   # SetMute

else:
    import os
    import shutil
    import subprocess

    _TIMEOUT_S = 2.0


    def _run(*args):
        """A command's output, or None when it failed (no server, no default source)."""
        try:
            done = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT_S,
                                  env={**os.environ, "LC_ALL": "C"})      # pactl's "Mute: yes" is translated
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None


    def _wpctl_muted():
        out = _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@")          # "Volume: 0.45 [MUTED]"
        return "[MUTED]" in out if out and out.startswith("Volume:") else None


    def _wpctl_set(muted):
        return _run("wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1" if muted else "0") is not None


    def _pactl_muted():
        out = _run("pactl", "get-source-mute", "@DEFAULT_SOURCE@")           # "Mute: yes"
        return out.split("Mute:", 1)[1].strip().startswith("yes") if out and "Mute:" in out else None


    def _pactl_set(muted):
        return _run("pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1" if muted else "0") is not None


    _BACKENDS = (("wpctl", _wpctl_muted, _wpctl_set), ("pactl", _pactl_muted, _pactl_set))


    def _backend():
        """(muted, set) of the first tool that reports the default source's mute: wpctl, else pactl.
        (None, None) without a microphone; OSError when neither tool is installed."""
        installed = [b for b in _BACKENDS if shutil.which(b[0])]
        if not installed:
            raise FileNotFoundError("neither wpctl (PipeWire) nor pactl (PulseAudio) is installed")
        for _tool, get_muted, set_mute in installed:
            muted = get_muted()
            if muted is not None:
                return muted, set_mute
        return None, None


    def is_muted():
        """The default microphone's mute, or None without one."""
        return _backend()[0]


    def set_muted(muted):
        """Mute (True) or unmute the default microphone. False without one."""
        _state, set_mute = _backend()
        return set_mute is not None and set_mute(muted)


def toggle():
    """Flip the default microphone's mute; returns the new state, or None without a microphone."""
    state = is_muted()
    if state is None:
        return None
    set_muted(not state)
    return not state
