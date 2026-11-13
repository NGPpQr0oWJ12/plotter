"""Open a prepared DXF in the native Xiao desktop application on Windows."""

import ctypes as ct
from ctypes import wintypes as win
import os
from pathlib import Path
import struct
import subprocess
from threading import Lock


WM_DROPFILES = 0x0233
WS_EX_ACCEPTFILES = 0x00000010
GWL_EXSTYLE = -20
GW_OWNER = 4
TH32CS_SNAPPROCESS = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GMEM_MOVEABLE_ZEROINIT = 0x0042
GMEM_INVALID_HANDLE = 0x8000
ERROR_ACCESS_DENIED = 5
ERROR_NO_MORE_FILES = 18
ERROR_INVALID_PARAMETER = 87
ERROR_NOT_FOUND = 1168
INVALID_HANDLE = ct.c_void_p(-1).value
_handoff_lock = Lock()


class _ProcessEntry(ct.Structure):
    _fields_ = [
        ("dwSize", win.DWORD),
        ("cntUsage", win.DWORD),
        ("th32ProcessID", win.DWORD),
        ("th32DefaultHeapID", ct.c_size_t),
        ("th32ModuleID", win.DWORD),
        ("cntThreads", win.DWORD),
        ("th32ParentProcessID", win.DWORD),
        ("pcPriClassBase", win.LONG),
        ("dwFlags", win.DWORD),
        ("szExeFile", win.WCHAR * 260),
    ]


def _bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = arguments
    return function


class _Windows:
    def __init__(self):
        kernel = ct.WinDLL("kernel32", use_last_error=True)
        user = ct.WinDLL("user32", use_last_error=True)
        self.callback = ct.WINFUNCTYPE(win.BOOL, win.HWND, win.LPARAM)

        self.snapshot = _bind(kernel, "CreateToolhelp32Snapshot", win.HANDLE, win.DWORD, win.DWORD)
        self.first_process = _bind(kernel, "Process32FirstW", win.BOOL, win.HANDLE, ct.POINTER(_ProcessEntry))
        self.next_process = _bind(kernel, "Process32NextW", win.BOOL, win.HANDLE, ct.POINTER(_ProcessEntry))
        self.open_process = _bind(kernel, "OpenProcess", win.HANDLE, win.DWORD, win.BOOL, win.DWORD)
        self.process_image = _bind(kernel, "QueryFullProcessImageNameW", win.BOOL,
                                   win.HANDLE, win.DWORD, win.LPWSTR, win.LPDWORD)
        self.close_handle = _bind(kernel, "CloseHandle", win.BOOL, win.HANDLE)

        self.enum_windows = _bind(user, "EnumWindows", win.BOOL, self.callback, win.LPARAM)
        self.enum_children = _bind(user, "EnumChildWindows", win.BOOL, win.HWND, self.callback, win.LPARAM)
        self.window_process = _bind(user, "GetWindowThreadProcessId", win.DWORD, win.HWND, win.LPDWORD)
        self.window_visible = _bind(user, "IsWindowVisible", win.BOOL, win.HWND)
        self.window_enabled = _bind(user, "IsWindowEnabled", win.BOOL, win.HWND)
        self.window_hung = _bind(user, "IsHungAppWindow", win.BOOL, win.HWND)
        self.get_window = _bind(user, "GetWindow", win.HWND, win.HWND, win.UINT)
        self.window_style = _bind(user, "GetWindowLongW", win.LONG, win.HWND, ct.c_int)
        self.post = _bind(user, "PostMessageW", win.BOOL, win.HWND, win.UINT, win.WPARAM, win.LPARAM)

        self.allocate = _bind(kernel, "GlobalAlloc", win.HGLOBAL, win.UINT, ct.c_size_t)
        self.lock_memory = _bind(kernel, "GlobalLock", ct.c_void_p, win.HGLOBAL)
        self.unlock_memory = _bind(kernel, "GlobalUnlock", win.BOOL, win.HGLOBAL)
        self.memory_flags = _bind(kernel, "GlobalFlags", win.UINT, win.HGLOBAL)
        self.free = _bind(kernel, "GlobalFree", win.HGLOBAL, win.HGLOBAL)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _image_path(api, pid):
    handle = api.open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ct.get_last_error() in (ERROR_INVALID_PARAMETER, ERROR_NOT_FOUND):
            return None
        raise ValueError("Не удалось проверить путь запущенной копии Xiao.")
    try:
        buffer = ct.create_unicode_buffer(32768)
        length = win.DWORD(len(buffer))
        if not api.process_image(handle, 0, buffer, ct.byref(length)):
            if ct.get_last_error() in (ERROR_INVALID_PARAMETER, ERROR_NOT_FOUND):
                return None
            raise ValueError("Не удалось прочитать путь запущенной копии Xiao.")
        return _path_key(Path(buffer.value))
    finally:
        api.close_handle(handle)


def _running_instances(api, trusted):
    snapshot = api.snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE:
        raise ValueError("Не удалось проверить, запущена ли Xiao.")

    matching = set()
    try:
        entry = _ProcessEntry()
        entry.dwSize = ct.sizeof(entry)
        found = api.first_process(snapshot, ct.byref(entry))
        while found:
            if entry.szExeFile.casefold() == "xiao.exe":
                if _image_path(api, entry.th32ProcessID) in trusted:
                    matching.add(entry.th32ProcessID)
            found = api.next_process(snapshot, ct.byref(entry))
        if ct.get_last_error() != ERROR_NO_MORE_FILES:
            raise ValueError("Не удалось проверить список запущенных приложений Windows.")
    finally:
        api.close_handle(snapshot)
    return matching


def _window_pid(api, handle):
    pid = win.DWORD()
    api.window_process(handle, ct.byref(pid))
    return pid.value


def _drop_window(api, pid):
    windows = []

    @api.callback
    def collect(handle, _):
        windows.append(handle)
        return True

    if not api.enum_windows(collect, 0):
        raise ValueError("Не удалось найти окно Xiao в текущем сеансе Windows.")

    def accepts_files(handle):
        return (api.window_visible(handle) and api.window_enabled(handle)
                and api.window_style(handle, GWL_EXSTYLE) & WS_EX_ACCEPTFILES
                and _window_pid(api, handle) == pid and not api.window_hung(handle))

    for main in windows[:]:
        if (_window_pid(api, main) != pid or not api.window_visible(main) or not api.window_enabled(main)
                or api.get_window(main, GW_OWNER) or api.window_hung(main)):
            continue
        if accepts_files(main):
            return main
        windows.clear()
        api.enum_children(main, collect, 0)
        for child in windows:
            if accepts_files(child):
                return child
    raise ValueError("Окно Xiao недоступно для открытия DXF. Разверните Xiao и закройте открытые в ней диалоги.")


def _drop_data(path: Path) -> bytes:
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    return header + (str(path) + "\x00\x00").encode("utf-16-le")


def _post_file(api, target, path):
    data = _drop_data(path)
    memory = api.allocate(GMEM_MOVEABLE_ZEROINIT, len(data))
    if not memory:
        raise ValueError("Windows не смогла подготовить передачу файла в Xiao.")

    owned = True
    try:
        destination = api.lock_memory(memory)
        if not destination:
            raise ValueError("Windows не смогла записать путь к DXF для Xiao.")
        try:
            ct.memmove(destination, data, len(data))
        finally:
            ct.set_last_error(0)
            unlocked = api.unlock_memory(memory)
        if not unlocked and ct.get_last_error():
            raise ValueError("Windows не смогла завершить подготовку передачи DXF.")

        ct.set_last_error(0)
        if api.post(target, WM_DROPFILES, memory, 0):
            # Windows marshals the HDROP to the other process and consumes this handle.
            # The receiving window releases its copy with DragFinish.
            owned = False
            return
        error = ct.get_last_error()
        # Posting may fail either before or after the cross-process marshaler consumes the handle.
        owned = api.memory_flags(memory) != GMEM_INVALID_HANDLE
        if error == ERROR_ACCESS_DENIED:
            raise ValueError("Windows блокирует передачу в Xiao из-за разных прав доступа. Откройте подготовленный DXF в Xiao вручную.")
        raise ValueError("Windows не передала DXF в Xiao. Откройте подготовленный файл в Xiao вручную.")
    finally:
        if owned:
            api.free(memory)


def open_dxf(executable: Path, path: Path, alternate_executables=()) -> str:
    """Queue a file drop in a trusted Xiao instance, or start Xiao with its /f option."""
    if os.name != "nt":
        raise ValueError("Открытие файла в штатной Xiao доступно в Windows.")
    try:
        if not path.is_absolute() or path.suffix.lower() != ".dxf" or not path.is_file():
            raise ValueError("Подготовленный DXF не найден.")
        path = path.resolve(strict=True)
        candidates = [candidate.resolve() for candidate in (executable, *alternate_executables)
                      if candidate.is_absolute() and candidate.name.casefold() == "xiao.exe" and candidate.is_file()]
        if not candidates:
            raise ValueError("Не найден установленный xiao.exe.")
        trusted = {_path_key(candidate) for candidate in candidates}

        with _handoff_lock:
            api = _Windows()
            instances = _running_instances(api, trusted)
            if len(instances) > 1:
                raise ValueError("Запущено несколько копий Xiao. Откройте подготовленный DXF в нужной копии вручную.")
            if instances:
                pid = instances.pop()
                target = _drop_window(api, pid)
                if _window_pid(api, target) != pid or _image_path(api, pid) not in trusted:
                    raise ValueError("Окно Xiao изменилось. Повторите открытие файла.")
                _post_file(api, target, path)
                return "queued"

            program = candidates[0]
            subprocess.Popen([str(program), "/f", str(path)], cwd=program.parent,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "launched"
    except OSError as error:
        raise ValueError("Не удалось открыть DXF в Xiao. Подготовленный файл доступен для ручного открытия.") from error
