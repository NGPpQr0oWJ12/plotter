"""Print the same label bitmap as the preview through a Windows label driver."""

import ctypes as ct
from ctypes import wintypes as w
from io import BytesIO
from math import ceil, floor
import sys

from PIL import Image, ImageChops


NON_GDI_ATTRIBUTES = 0x1000 | 0x4000  # PRINTER_ATTRIBUTE_RAW_ONLY and PRINTER_ATTRIBUTE_FAX.
INTERACTIVE_PORTS = frozenset(("FILE:", "PORTPROMPT:", "SHRFAX:", "NUL:"))
UNAVAILABLE_STATUS = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x40 | 0x80 | 0x800 | 0x1000 | 0x100000 | 0x400000 | 0x2000000
DM_SCALE, DM_COPIES, DM_FORMNAME = 0x10, 0x100, 0x10000


class PrinterUnavailable(OSError):
    pass


class SubmissionUnknown(OSError):
    pass


class DriverFormatError(ValueError):
    pass


class QueueInfo(ct.Structure):
    _fields_ = [("name", w.LPWSTR), ("port", w.LPWSTR), ("attributes", w.DWORD),
                ("deviceTimeout", w.DWORD), ("retryTimeout", w.DWORD)]


class DeviceMode(ct.Structure):
    # Only the documented print prefix is edited. Keep the driver's private tail intact.
    _fields_ = [("name", w.WCHAR * 32), ("spec", w.WORD), ("driver", w.WORD),
                ("size", w.WORD), ("extra", w.WORD), ("fields", w.DWORD),
                ("orientation", ct.c_short), ("paper", ct.c_short),
                ("length", ct.c_short), ("width", ct.c_short), ("scale", ct.c_short), ("copies", ct.c_short)]


class DocInfo(ct.Structure):
    _fields_ = [("size", ct.c_int), ("name", w.LPCWSTR), ("output", w.LPCWSTR),
                ("datatype", w.LPCWSTR), ("flags", w.DWORD)]


class BitmapHeader(ct.Structure):
    _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD), ("imageSize", w.DWORD),
                ("xPerMeter", w.LONG), ("yPerMeter", w.LONG), ("colorsUsed", w.DWORD), ("colorsImportant", w.DWORD)]


class BitmapInfo(ct.Structure):
    _fields_ = [("header", BitmapHeader), ("colors", w.DWORD * 1)]


def _api(library, name, result, *arguments):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result
    return function


def printers():
    if sys.platform != "win32":
        return []
    spool = ct.WinDLL("winspool.drv", use_last_error=True)
    enum = _api(spool, "EnumPrintersW", w.BOOL, w.DWORD, w.LPWSTR, w.DWORD, ct.c_void_p,
                w.DWORD, ct.POINTER(w.DWORD), ct.POINTER(w.DWORD))
    size, count = w.DWORD(), w.DWORD()
    enum(6, None, 5, None, 0, ct.byref(size), ct.byref(count))
    if not size.value:
        return []
    buffer = ct.create_string_buffer(size.value)
    if not enum(6, None, 5, buffer, size, ct.byref(size), ct.byref(count)):
        raise PrinterUnavailable("Не удалось прочитать список принтеров Windows.")
    # Interactive file/fax ports would display a save dialog in the companion.
    return sorted({p.name for p in (QueueInfo * count.value).from_buffer(buffer)
                   if p.name and p.port and not p.attributes & NON_GDI_ATTRIBUTES
                   and INTERACTIVE_PORTS.isdisjoint(port.strip().upper() for port in p.port.split(","))})


def _mode_view(buffer):
    form = DeviceMode.from_buffer(buffer)
    if form.size < ct.sizeof(DeviceMode) or form.size + form.extra > ct.sizeof(buffer):
        raise DriverFormatError("Драйвер вернул неполные настройки бумаги.")
    return form


def _single_copy(form):
    return (not form.fields & DM_SCALE or form.scale == 100) and (not form.fields & DM_COPIES or form.copies == 1)


def _bitmap_geometry(bitmap, width, height, left, top, printable_width, printable_height):
    if min(printable_width, printable_height) <= 0 or min(left, top) < 0:
        raise DriverFormatError("Драйвер не предоставил область печати бирки.")
    ink = ImageChops.difference(bitmap, Image.new("RGB", bitmap.size, "white")).getbbox()
    if ink and (floor(ink[0] * width / bitmap.width) < left or floor(ink[1] * height / bitmap.height) < top
                or ceil(ink[2] * width / bitmap.width) > left + printable_width
                or ceil(ink[3] * height / bitmap.height) > top + printable_height):
        raise DriverFormatError("Содержимое бирки выходит за область печати. Увеличьте поля шаблона.")
    pixels = bitmap.tobytes("raw", "BGRX")
    info = BitmapInfo(BitmapHeader(ct.sizeof(BitmapHeader), bitmap.width, -bitmap.height, 1, 32, 0, len(pixels)))
    return pixels, info


def print_label(printer, png, width_mm, height_mm, title):
    if printer not in printers():
        raise PrinterUnavailable("Принтер недоступен в Windows.")
    if any(type(value) not in (int, float) or not 0.1 <= value <= 3276.7 for value in (width_mm, height_mm)):
        raise DriverFormatError("Недопустимый размер бумаги в миллиметрах.")
    spool = ct.WinDLL("winspool.drv", use_last_error=True)
    gdi = ct.WinDLL("gdi32", use_last_error=True)
    ptr, handle = ct.c_void_p, w.HANDLE
    open_printer = _api(spool, "OpenPrinterW", w.BOOL, w.LPWSTR, ct.POINTER(handle), ptr)
    close = _api(spool, "ClosePrinter", w.BOOL, handle)
    properties = _api(spool, "DocumentPropertiesW", w.LONG, handle, handle, w.LPWSTR, ptr, ptr, w.DWORD)
    get_printer = _api(spool, "GetPrinterW", w.BOOL, handle, w.DWORD, ptr, w.DWORD, ct.POINTER(w.DWORD))
    create_dc = _api(gdi, "CreateDCW", handle, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ptr)
    delete_dc = _api(gdi, "DeleteDC", w.BOOL, handle)
    caps = _api(gdi, "GetDeviceCaps", ct.c_int, handle, ct.c_int)
    draw = _api(gdi, "StretchDIBits", ct.c_int, handle, *(ct.c_int,) * 8, ptr,
                ct.POINTER(BitmapInfo), w.UINT, w.DWORD)
    begin = _api(gdi, "StartDocW", ct.c_int, handle, ct.POINTER(DocInfo))
    start_page = _api(gdi, "StartPage", ct.c_int, handle)
    end_page = _api(gdi, "EndPage", ct.c_int, handle)
    end = _api(gdi, "EndDoc", ct.c_int, handle)
    abort = _api(gdi, "AbortDoc", ct.c_int, handle)
    device = handle()
    if not open_printer(printer, ct.byref(device), None):
        raise PrinterUnavailable("Не удалось открыть выбранный принтер.")
    def open_page(mode, *, custom=False):
        candidate = create_dc("WINSPOOL", printer, None, mode)
        if not candidate:
            if custom:
                raise PrinterUnavailable("Драйвер принтера недоступен.")
            return None, 0, 0
        xdpi, ydpi = caps(candidate, 88), caps(candidate, 90)
        width, height = caps(candidate, 110), caps(candidate, 111)
        if (min(xdpi, ydpi, width, height) <= 0 or abs(width - round(width_mm * xdpi / 25.4)) > 2
                or abs(height - round(height_mm * ydpi / 25.4)) > 2):
            delete_dc(candidate)
            return None, 0, 0
        return candidate, width, height

    dc = None
    try:
        status, needed = w.DWORD(), w.DWORD()
        if get_printer(device, 6, ct.byref(status), ct.sizeof(status), ct.byref(needed)):
            if status.value & UNAVAILABLE_STATUS:
                raise PrinterUnavailable("Проверьте питание, бумагу и состояние очереди Windows.")
        size = properties(None, device, printer, None, None, 0)
        if size < ct.sizeof(DeviceMode) or size > 1024 * 1024:
            raise DriverFormatError("Драйвер не предоставил настройки бумаги.")
        mode = ct.create_string_buffer(size)
        if properties(None, device, printer, mode, None, 2) != 1:
            raise DriverFormatError("Не удалось получить настройки драйвера.")
        form = _mode_view(mode)
        if not _single_copy(form):
            form.scale, form.copies = 100, 1
            if properties(None, device, printer, mode, mode, 10) != 1 or not _single_copy(_mode_view(mode)):
                raise DriverFormatError("Драйвер не подтвердил печать одной копии без масштабирования.")
        # Accept a stock already configured at the correct size before asking for custom media.
        dc, width, height = open_page(mode)
        if not dc:
            form.fields = (form.fields & ~DM_FORMNAME) | 0x0F
            form.orientation, form.paper = 1, 0
            form.length, form.width = round(height_mm * 10), round(width_mm * 10)
            if properties(None, device, printer, mode, mode, 10) != 1 or not _single_copy(_mode_view(mode)):
                raise DriverFormatError("Драйвер не принял размер этикетки без масштабирования.")
            dc, width, height = open_page(mode, custom=True)
            if not dc:
                raise DriverFormatError("Задайте в драйвере Windows формат бумаги, совпадающий с шаблоном.")
        left, top = caps(dc, 112), caps(dc, 113)
        with Image.open(BytesIO(png), formats=("PNG",)) as source:
            bitmap = source.convert("RGB")
        pixels, bitmap_info = _bitmap_geometry(bitmap, width, height, left, top, caps(dc, 8), caps(dc, 10))
        info = DocInfo(ct.sizeof(DocInfo), title, None, None, 0)
        job = begin(dc, ct.byref(info))
        if job <= 0:
            raise PrinterUnavailable("Windows не приняла задание печати.")
        try:
            if start_page(dc) <= 0:
                raise OSError("StartPage")
            if draw(dc, -left, -top, width, height, 0, 0, bitmap.width, bitmap.height, ct.c_char_p(pixels),
                    ct.byref(bitmap_info), 0, 0xCC0020) <= 0:
                raise OSError("StretchDIBits")
            if end_page(dc) <= 0 or end(dc) <= 0:
                raise OSError("EndDoc")
        except Exception as error:
            abort(dc)
            raise SubmissionUnknown("Проверьте ленту и очередь Windows перед повтором.") from error
        return job
    finally:
        if dc:
            delete_dc(dc)
        close(device)
