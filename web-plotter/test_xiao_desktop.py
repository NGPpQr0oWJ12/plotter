import ctypes as ct
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import xiao_desktop as desktop


class XiaoDesktopTests(unittest.TestCase):
    def test_drop_files_structure_has_unicode_and_double_terminator(self):
        path = Path("C:/Раскрой/Деталь.dxf")
        payload = desktop._drop_data(path)
        self.assertEqual(struct.unpack_from("<IiiII", payload), (20, 0, 0, 0, 1))
        self.assertEqual(payload[20:].decode("utf-16-le"), str(path) + "\0\0")

    def test_selects_registered_xiao_view_under_verified_main_window(self):
        api = Mock()
        api.callback.side_effect = lambda callback: callback
        api.window_visible.return_value = True
        api.window_enabled.return_value = True
        api.window_hung.return_value = False
        api.get_window.return_value = None
        api.window_style.side_effect = lambda hwnd, _: desktop.WS_EX_ACCEPTFILES if hwnd in (10, 21) else 0

        def enumerate_top(callback, _):
            callback(10, 0)
            callback(20, 0)
            return True

        def enumerate_children(parent, callback, _):
            self.assertEqual(parent, 20)
            callback(21, 0)

        api.enum_windows.side_effect = enumerate_top
        api.enum_children.side_effect = enumerate_children
        with patch.object(desktop, "_window_pid", side_effect=lambda _, hwnd: 77 if hwnd >= 20 else 99):
            self.assertEqual(desktop._drop_window(api, 77), 21)

    @unittest.skipUnless(os.name == "nt", "Windows error handling")
    def test_post_uipi_error_frees_only_memory_still_owned_by_sender(self):
        for consumed in (False, True):
            with self.subTest(consumed=consumed):
                api = Mock()
                buffer = ct.create_string_buffer(4096)
                api.allocate.return_value = 77
                api.lock_memory.return_value = ct.addressof(buffer)
                api.unlock_memory.return_value = False
                api.memory_flags.return_value = desktop.GMEM_INVALID_HANDLE if consumed else 0

                def blocked(*_):
                    ct.set_last_error(desktop.ERROR_ACCESS_DENIED)
                    return False

                api.post.side_effect = blocked
                with self.assertRaisesRegex(ValueError, "прав доступа"):
                    desktop._post_file(api, 123, Path("C:/Раскрой/Деталь.dxf"))
                api.post.assert_called_once_with(123, desktop.WM_DROPFILES, 77, 0)
                self.assertEqual(api.free.call_count, int(not consumed))

    @unittest.skipUnless(os.name == "nt", "Windows file handoff")
    def test_existing_localized_instance_is_selected_instead_of_starting_another_copy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / "xiao.exe"
            localized = root / "local" / "xiao.exe"
            drawing = root / "Деталь.dxf"
            localized.parent.mkdir()
            for path in (exe, localized, drawing):
                path.touch()

            with patch.object(desktop, "_Windows"), patch.object(desktop, "_running_instances", return_value={42}) as running, \
                    patch.object(desktop, "_drop_window", return_value=123), patch.object(desktop, "_window_pid", return_value=42), \
                    patch.object(desktop, "_image_path", return_value=desktop._path_key(localized)), \
                    patch.object(desktop, "_post_file") as post, patch.object(desktop.subprocess, "Popen") as launch:
                result = desktop.open_dxf(exe, drawing, (localized,))

            self.assertEqual(result, "queued")
            self.assertEqual(running.call_args.args[1], {desktop._path_key(exe), desktop._path_key(localized)})
            self.assertEqual(post.call_args.args[1:], (123, drawing.resolve()))
            launch.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows file handoff")
    def test_cold_launch_passes_only_file_option_and_leaves_dxf_available(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / "xiao.exe"
            drawing = root / "Деталь с пробелом.dxf"
            exe.touch()
            drawing.touch()
            with patch.object(desktop, "_Windows"), patch.object(desktop, "_running_instances", return_value=set()), \
                    patch.object(desktop.subprocess, "Popen") as launch:
                self.assertEqual(desktop.open_dxf(exe, drawing), "launched")
                self.assertEqual(launch.call_args.args[0], [str(exe.resolve()), "/f", str(drawing.resolve())])
                self.assertEqual(launch.call_args.kwargs["cwd"], exe.resolve().parent)
            self.assertTrue(drawing.is_file())
