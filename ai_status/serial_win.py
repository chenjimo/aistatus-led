"""Windows 串口读写：优先 pyserial，未安装时用 ctypes 调 Win32 API（零依赖）。

关键点（见 API_Doc/Dev_Notes.md）：打开后 DTR/RTS 必须置 false，否则 ESP32-S3 可能复位。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PURGE_RXCLEAR = 0x0008
PURGE_TXCLEAR = 0x0004
CLRRTS = 4
CLRDTR = 6


class SerialIOError(OSError):
    pass


class _LineReader:
    """按换行切帧；设备按行解析协议帧，其余行按普通日志跳过。"""

    def __init__(self) -> None:
        self._buf = b""

    def _read_chunk(self) -> bytes:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def read_line(self, deadline: float) -> bytes | None:
        while time.monotonic() < deadline:
            chunk = self._read_chunk()
            if chunk:
                self._buf += chunk
                if b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    return line.strip()
                if len(self._buf) > 8192:
                    self._buf = self._buf[-4096:]
            else:
                time.sleep(0.01)
        if self._buf:
            line, self._buf = self._buf, b""
            return line.strip()
        return None

    def drain(self, max_wait: float = 0.06) -> None:
        """丢弃上一条命令遗留的广播/回执，避免把旧帧错配成本次应答。"""
        self._buf = b""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            try:
                chunk = self._read_chunk()
            except Exception:
                break
            if not chunk:
                break


if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _DCB(ctypes.Structure):
        _fields_ = [
            ("DCBlength", wintypes.DWORD),
            ("BaudRate", wintypes.DWORD),
            ("fBinary", wintypes.DWORD, 1),
            ("fParity", wintypes.DWORD, 1),
            ("fOutxCtsFlow", wintypes.DWORD, 1),
            ("fOutxDsrFlow", wintypes.DWORD, 1),
            ("fDtrControl", wintypes.DWORD, 2),
            ("fDsrSensitivity", wintypes.DWORD, 1),
            ("fTXContinueOnXoff", wintypes.DWORD, 1),
            ("fOutX", wintypes.DWORD, 1),
            ("fInX", wintypes.DWORD, 1),
            ("fErrorChar", wintypes.DWORD, 1),
            ("fNull", wintypes.DWORD, 1),
            ("fRtsControl", wintypes.DWORD, 2),
            ("fAbortOnError", wintypes.DWORD, 1),
            ("fDummy2", wintypes.DWORD, 17),
            ("wReserved", wintypes.WORD),
            ("XonLim", wintypes.WORD),
            ("XoffLim", wintypes.WORD),
            ("ByteSize", wintypes.BYTE),
            ("Parity", wintypes.BYTE),
            ("StopBits", wintypes.BYTE),
            ("XonChar", ctypes.c_char),
            ("XoffChar", ctypes.c_char),
            ("ErrorChar", ctypes.c_char),
            ("EofChar", ctypes.c_char),
            ("EvtChar", ctypes.c_char),
            ("wReserved1", wintypes.WORD),
        ]

    class _COMMTIMEOUTS(ctypes.Structure):
        _fields_ = [
            ("ReadIntervalTimeout", wintypes.DWORD),
            ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
            ("ReadTotalTimeoutConstant", wintypes.DWORD),
            ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
            ("WriteTotalTimeoutConstant", wintypes.DWORD),
        ]

    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.ReadFile.restype = wintypes.BOOL
    _k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.WriteFile.restype = wintypes.BOOL
    _k32.GetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(_DCB)]
    _k32.SetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(_DCB)]
    _k32.SetCommTimeouts.argtypes = [wintypes.HANDLE, ctypes.POINTER(_COMMTIMEOUTS)]
    _k32.SetupComm.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    _k32.PurgeComm.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.EscapeCommFunction.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    class Win32Serial(_LineReader):
        def __init__(self, port: str, baud: int = 115200, read_timeout_ms: int = 100):
            super().__init__()
            self.port = "\\\\.\\" + port if not port.startswith("\\\\") else port
            self.baud = baud
            self.read_timeout_ms = read_timeout_ms
            self._h = None

        def _check(self, ok: bool, what: str) -> None:
            if not ok:
                raise SerialIOError(f"{what} 失败 (Win32 error={ctypes.get_last_error()})")

        def open(self) -> None:
            h = _k32.CreateFileW(self.port, GENERIC_READ | GENERIC_WRITE, 0, None,
                                 OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
            if not h or h == INVALID_HANDLE_VALUE:
                raise SerialIOError(f"无法打开 {self.port} (Win32 error={ctypes.get_last_error()})")
            self._h = h
            _k32.SetupComm(h, 4096, 4096)
            dcb = _DCB()
            dcb.DCBlength = ctypes.sizeof(_DCB)
            self._check(_k32.GetCommState(h, ctypes.byref(dcb)), "GetCommState")
            dcb.BaudRate = self.baud
            dcb.ByteSize = 8
            dcb.Parity = 0
            dcb.StopBits = 0
            dcb.fBinary = 1
            dcb.fParity = 0
            dcb.fOutxCtsFlow = 0
            dcb.fOutxDsrFlow = 0
            dcb.fDtrControl = 0     # DTR_CONTROL_DISABLE
            dcb.fDsrSensitivity = 0
            dcb.fTXContinueOnXoff = 0
            dcb.fOutX = 0
            dcb.fInX = 0
            dcb.fErrorChar = 0
            dcb.fNull = 0
            dcb.fRtsControl = 0     # RTS_CONTROL_DISABLE
            dcb.fAbortOnError = 0
            self._check(_k32.SetCommState(h, ctypes.byref(dcb)), "SetCommState")
            _k32.EscapeCommFunction(h, CLRDTR)
            _k32.EscapeCommFunction(h, CLRRTS)
            timeouts = _COMMTIMEOUTS(50, 0, self.read_timeout_ms, 0, 0)
            _k32.SetCommTimeouts(h, ctypes.byref(timeouts))
            _k32.PurgeComm(h, PURGE_RXCLEAR | PURGE_TXCLEAR)

        def _read_chunk(self) -> bytes:
            buf = ctypes.create_string_buffer(1024)
            got = wintypes.DWORD()
            if not _k32.ReadFile(self._h, buf, 1024, ctypes.byref(got), None):
                raise SerialIOError(f"读取 {self.port} 失败 (Win32 error={ctypes.get_last_error()})")
            return buf.raw[:got.value]

        def write(self, data: bytes) -> None:
            written = wintypes.DWORD()
            buf = ctypes.create_string_buffer(data)
            if not _k32.WriteFile(self._h, buf, len(data), ctypes.byref(written), None):
                raise SerialIOError(f"写入 {self.port} 失败 (Win32 error={ctypes.get_last_error()})")
            if written.value != len(data):
                raise SerialIOError(f"串口写入不完整：{written.value}/{len(data)}")

        def close(self) -> None:
            if self._h:
                _k32.CloseHandle(self._h)
                self._h = None


class PySerialAdapter(_LineReader):
    def __init__(self, ser) -> None:
        super().__init__()
        self.ser = ser

    def _read_chunk(self) -> bytes:
        return self.ser.read(256) or b""

    def write(self, data: bytes) -> None:
        self.ser.write(data)

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


def open_serial(port: str, baud: int = 115200, read_timeout_ms: int = 100):
    """返回具备 write / read_line / close 的串口对象。"""
    try:
        import serial  # type: ignore
    except ImportError:
        ser = Win32Serial(port, baud, read_timeout_ms)
        ser.open()
        return ser
    handle = serial.Serial()
    handle.port = port
    handle.baudrate = baud
    handle.timeout = read_timeout_ms / 1000
    handle.dtr = False       # 打开前置 false，避免 pyserial open() 触发板子复位
    handle.rts = False
    handle.open()
    handle.dtr = False
    handle.rts = False
    return PySerialAdapter(handle)
