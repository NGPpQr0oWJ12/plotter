import ctypes as ct
from ctypes import wintypes as w
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import label_printer as printer


def png_at(x=1, y=1):
    image = Image.new("RGB", (4, 4), "white")
    image.putpixel((x, y), (17, 34, 51))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class FakeDriver:
    CAPABILITIES = {88: 203, 90: 203, 110: 464, 111: 320, 112: 8, 113: 6, 8: 448, 10: 308}

    def __init__(self, *, configured=True, custom=True, copies=1):
        self.configured, self.custom, self.copies = configured, custom, copies
        self.default_dc, self.custom_dc = 0x100000301, 0x100000302
        self.printer_handle = 0x100000401
        self.merge_requests = []
        self.mode_size = ct.sizeof(printer.DeviceMode) + 16
        self.status = 0
        self.spool = SimpleNamespace(OpenPrinterW=Mock(side_effect=self.open_printer), ClosePrinter=Mock(return_value=1),
                                     DocumentPropertiesW=Mock(side_effect=self.properties), GetPrinterW=Mock(side_effect=self.get_printer))
        self.gdi = SimpleNamespace(CreateDCW=Mock(side_effect=self.create_dc), DeleteDC=Mock(return_value=1),
                                   GetDeviceCaps=Mock(side_effect=self.caps), StretchDIBits=Mock(return_value=4),
                                   StartDocW=Mock(return_value=17), StartPage=Mock(return_value=1),
                                   EndPage=Mock(return_value=1), EndDoc=Mock(return_value=1), AbortDoc=Mock(return_value=1))

    def factory(self, name, **_):
        return {"winspool.drv": self.spool, "gdi32": self.gdi}[name]

    def open_printer(self, name, handle, defaults):
        ct.cast(handle, ct.POINTER(w.HANDLE)).contents.value = self.printer_handle
        return 1

    def get_printer(self, handle, level, status, size, needed):
        ct.cast(status, ct.POINTER(w.DWORD)).contents.value = self.status
        return 1

    def properties(self, hwnd, handle, name, output, input_, flags):
        if flags == 0:
            return self.mode_size
        mode = printer.DeviceMode.from_buffer(output)
        if flags == 2:
            mode.size, mode.extra = self.mode_size - 8, 8
            mode.scale, mode.copies = 100, self.copies
            mode.fields = printer.DM_FORMNAME | printer.DM_SCALE | printer.DM_COPIES
            output[-8:] = b"DRIVER00"
        if flags == 10:
            self.merge_requests.append((mode.orientation, mode.length, mode.width, mode.fields, mode.scale, mode.copies,
                                        output[-8:]))
        return 1

    def create_dc(self, driver, printer_name, port, raw):
        mode = printer.DeviceMode.from_buffer(raw)
        return self.custom_dc if mode.width else self.default_dc

    def caps(self, dc, field):
        matching = self.custom if dc == self.custom_dc else self.configured
        return 3300 if not matching and field in (110, 111) else self.CAPABILITIES[field]

    def print(self, png=None):
        with patch.object(printer, "printers", return_value=["Этикеточный"]), \
                patch.object(ct, "WinDLL", side_effect=self.factory, create=True):
            return printer.print_label("Этикеточный", png or png_at(), 58, 40, "Бирка")


class PrinterTests(unittest.TestCase):
    def test_enumeration_keeps_local_usb_but_excludes_file_fax_and_raw_only(self):
        records = (printer.QueueInfo * 5)(printer.QueueInfo("Этикеточный", "USB001", 0x40),
                                           printer.QueueInfo("Сетевой", "IP_00", 0x10),
                                           printer.QueueInfo("PDF", "USB002, PORTPROMPT:", 0x40),
                                           printer.QueueInfo("Факс", "PORT3", 0x4000),
                                           printer.QueueInfo("RAW", "USB003", 0x1040))

        def enumerate_queues(flags, name, level, buffer, length, needed, count):
            self.assertEqual((flags, level), (6, 5))
            ct.cast(needed, ct.POINTER(w.DWORD)).contents.value = ct.sizeof(records)
            if buffer is None:
                return 0
            ct.memmove(buffer, records, ct.sizeof(records))
            ct.cast(count, ct.POINTER(w.DWORD)).contents.value = len(records)
            return 1

        spool = SimpleNamespace(EnumPrintersW=Mock(side_effect=enumerate_queues))
        with patch.object(printer.sys, "platform", "win32"), patch.object(ct, "WinDLL", return_value=spool, create=True):
            self.assertEqual(printer.printers(), ["Сетевой", "Этикеточный"])

    def test_configured_stock_prints_top_down_bitmap_at_physical_offsets(self):
        driver = FakeDriver()

        def inspect(dc, x, y, width, height, sx, sy, sw, sh, pixels, info, usage, rop):
            self.assertEqual((dc, x, y, width, height), (driver.default_dc, -8, -6, 464, 320))
            self.assertEqual((sx, sy, sw, sh, usage, rop), (0, 0, 4, 4, 0, 0xCC0020))
            header = ct.cast(info, ct.POINTER(printer.BitmapInfo)).contents.header
            self.assertEqual((header.width, header.height, header.bits, header.planes, header.imageSize), (4, -4, 32, 1, 64))
            self.assertEqual(ct.string_at(pixels, 64)[20:24], b"\x33\x22\x11\x00")
            return 4

        driver.gdi.StretchDIBits.side_effect = inspect
        self.assertEqual(driver.print(), 17)
        self.assertEqual(driver.merge_requests, [])
        driver.gdi.EndDoc.assert_called_once_with(driver.default_dc)
        driver.gdi.DeleteDC.assert_called_once_with(driver.default_dc)
        self.assertEqual(driver.spool.ClosePrinter.call_args.args[0].value, driver.printer_handle)

    def test_requests_custom_mm_stock_without_overwriting_driver_private_bytes(self):
        driver = FakeDriver(configured=False, copies=3)
        self.assertEqual(driver.print(), 17)
        self.assertEqual(len(driver.merge_requests), 2)
        _, _, _, _, scale, copies, extra = driver.merge_requests[0]
        self.assertEqual((scale, copies, extra), (100, 1, b"DRIVER00"))
        orientation, length, width, fields, scale, copies, extra = driver.merge_requests[1]
        self.assertEqual((orientation, length, width, scale, copies, extra), (1, 400, 580, 100, 1, b"DRIVER00"))
        self.assertFalse(fields & printer.DM_FORMNAME)
        self.assertEqual(fields & 0x0F, 0x0F)
        self.assertEqual(driver.gdi.DeleteDC.call_count, 2)

    def test_refuses_wrong_driver_stock_or_clipped_ink_before_start_doc(self):
        mismatch, clipped = FakeDriver(configured=False, custom=False), FakeDriver()
        for driver, png in ((mismatch, png_at()), (clipped, png_at(x=0))):
            with self.subTest(driver=driver), self.assertRaises(printer.DriverFormatError):
                driver.print(png)
            driver.gdi.StartDocW.assert_not_called()
            driver.spool.ClosePrinter.assert_called_once()

    def test_drawing_failure_aborts_and_cannot_be_reported_as_submitted(self):
        driver = FakeDriver()
        driver.gdi.StretchDIBits.return_value = 0
        with self.assertRaises(printer.SubmissionUnknown):
            driver.print()
        driver.gdi.AbortDoc.assert_called_once_with(driver.default_dc)
        driver.gdi.EndPage.assert_not_called()
        driver.gdi.EndDoc.assert_not_called()

    def test_paper_problem_waits_before_starting_a_job(self):
        driver = FakeDriver()
        driver.status = 0x40  # PRINTER_STATUS_PAPER_PROBLEM, unrelated to the local queue attribute.
        with self.assertRaises(printer.PrinterUnavailable):
            driver.print()
        driver.gdi.StartDocW.assert_not_called()


if __name__ == "__main__":
    unittest.main()
