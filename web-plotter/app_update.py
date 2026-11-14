"""Download and stage signed-in GitHub releases for the Windows operator panel."""

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from tempfile import mkdtemp
from threading import Lock, Thread
from time import monotonic
from zipfile import BadZipFile, ZipFile


VERSION_PATTERN = re.compile(r"(?:0|[1-9]\d{0,5})\.(?:0|[1-9]\d{0,5})\.(?:0|[1-9]\d{0,5})", re.ASCII)
REPOSITORY = "NGPpQr0oWJ12/plotter"
MAX_ARCHIVE = 160 * 1024 * 1024
MAX_EXPANDED = 480 * 1024 * 1024
WINDOWS_NAME = re.compile(r"[A-Za-z0-9_.+@ -]+", re.ASCII)
WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
REQUIRED = {
    "VERSION", "server.py", "install-companion.ps1", "apply-companion-update.ps1",
    "runtime/python.exe", "runtime/pythonw.exe", "static/index.html", "static/app.js",
}


class UpdateError(ValueError):
    pass


def release_version(value):
    if isinstance(value, str) and VERSION_PATTERN.fullmatch(value):
        return tuple(map(int, value.split(".")))
    return ()


def read_version(root):
    for path in (root / "VERSION", root.parent / "VERSION"):
        try:
            value = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if release_version(value):
            return value
    return None


@dataclass(frozen=True)
class Release:
    version: str
    name: str
    size: int
    digest: str


def latest_update(metadata, current):
    if not isinstance(metadata, dict) or metadata.get("draft") is not False or metadata.get("prerelease") is not False:
        return None
    tag = metadata.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        return None
    version = tag[1:]
    if release_version(version) <= release_version(current):
        return None
    name = f"plotter-companion-v{version}-windows-x64.zip"
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != name:
            continue
        digest, size = asset.get("digest"), asset.get("size")
        if (isinstance(digest, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
                and type(size) is int and 0 < size <= MAX_ARCHIVE and asset.get("state") == "uploaded"):
            return Release(version, name, size, digest[7:])
    return None


def extract_update(path, target, release):
    if path.stat().st_size != release.size:
        raise UpdateError("Размер архива обновления отличается от указанного в GitHub.")
    digest = sha256()
    with path.open("rb") as contents:
        for block in iter(lambda: contents.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != release.digest:
        raise UpdateError("Контрольная сумма архива обновления не совпала.")
    with ZipFile(path) as archive:
        files = {}
        total = 0
        for entry in archive.infolist():
            name = entry.filename
            mode = stat.S_IFMT(entry.external_attr >> 16)
            parts = name.rstrip("/").split("/")
            if (len(parts) > 24 or len(name) > 245 or "\\" in name or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or any(not WINDOWS_NAME.fullmatch(part) or part.endswith((".", " "))
                           or part.partition(".")[0].strip().upper() in WINDOWS_DEVICES for part in parts)):
                raise UpdateError("Архив обновления содержит недопустимый путь.")
            if entry.is_dir():
                continue
            key = name.casefold()
            if key in files or len(files) >= 20_000:
                raise UpdateError("Архив обновления содержит повторяющиеся пути или слишком много файлов.")
            total += entry.file_size
            if total > MAX_EXPANDED:
                raise UpdateError("Архив обновления слишком велик.")
            files[key] = entry
        if not REQUIRED <= {entry.filename for entry in files.values()}:
            raise UpdateError("В релизе не хватает файлов помощника.")
        version_entry = files["version"]
        if version_entry.file_size > 32 or archive.read(version_entry).decode("ascii").strip() != release.version:
            raise UpdateError("Версия в архиве отличается от версии релиза.")
        for entry in files.values():
            destination = target.joinpath(*entry.filename.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)


class AppUpdates:
    def __init__(self, app_root, version, install_root=None):
        self.worker = app_root / "apply-companion-update.ps1"
        self.version = version
        self.install_root = install_root
        self.supported = bool(install_root and version and self.worker.is_file())
        self._lock = Lock()
        self._requests = 0
        self._state = "idle"
        self._message = None
        self._available = None
        self._check_after = 0
        self._checking = False
        self._restarted_at = 0

    @staticmethod
    def github(*args, timeout):
        executable = shutil.which("gh.exe")
        if not executable:
            candidates = [
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "GitHub CLI" / "gh.exe",
            ]
            executable = next((str(candidate) for candidate in candidates if candidate.is_file()), None)
        if not executable:
            raise UpdateError("Для обновлений установите GitHub CLI и войдите в GitHub.")
        environment = dict(os.environ, GH_PROMPT_DISABLED="1")
        completed = subprocess.run([executable, *args], env=environment, capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout, check=False)
        if completed.returncode:
            raise UpdateError("GitHub недоступен. Проверьте интернет и вход в GitHub CLI с доступом к Plotter.")
        return completed.stdout

    def _refresh(self):
        try:
            data = self.github("api", "--hostname", "github.com", f"repos/{REPOSITORY}/releases/latest", timeout=15)
            available, message = latest_update(json.loads(data), self.version), None
        except UpdateError as error:
            available, message = None, str(error)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            available, message = None, "Не удалось проверить релизы GitHub."
        with self._lock:
            self._available = available
            if self._state == "idle":
                self._message = message
            self._checking = False
            self._check_after = monotonic() + (60 if message else 300)

    def status(self):
        with self._lock:
            state, message = self._state, self._message
            if self.supported:
                result_path = self.install_root / "update-result.json"
                try:
                    if result_path.stat().st_size > 4096:
                        raise ValueError("Invalid update state")
                    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
                    if state != "preparing" and result.get("state") == "failed":
                        state = self._state = "failed"
                        message = self._message = str(result.get("message") or "Обновление не завершилось.")[:500]
                    if result.get("state") == "done" and result.get("version") == self.version:
                        state = "done"
                except (OSError, ValueError, AttributeError):
                    pass
                if state == "restarting" and monotonic() - self._restarted_at > 11 * 60:
                    state = self._state = "failed"
                    message = self._message = "Windows не завершила перезапуск помощника. Повторите обновление."
                if state not in ("preparing", "restarting") and not self._checking and monotonic() >= self._check_after:
                    self._checking = True
                    Thread(target=self._refresh, daemon=True).start()
            return {"version": self.version, "supported": self.supported,
                    "availableVersion": self._available.version if self._available else None,
                    "state": state, "message": message}

    @contextmanager
    def protected_request(self, *, allow_restart=False):
        with self._lock:
            if self._state == "restarting" and not allow_restart:
                raise UpdateError("Помощник перезапускается для обновления.")
            self._requests += 1
        try:
            yield
        finally:
            with self._lock:
                self._requests -= 1

    def start(self, version, verify_idle):
        with self._lock:
            if not self.supported:
                raise UpdateError("Обновление доступно в установленной панели станка.")
            if self._state in ("preparing", "restarting"):
                raise UpdateError("Обновление уже выполняется.")
            if not self._available or version != self._available.version:
                raise UpdateError("Версия обновления изменилась. Повторите проверку.")
            (self.install_root / "update-result.json").unlink(missing_ok=True)
            self._state, self._message = "preparing", None
            Thread(target=self._prepare, args=(self._available, verify_idle), daemon=True).start()

    def _prepare(self, release, verify_idle):
        updates_dir = self.install_root / "updates"
        try:
            updates_dir.mkdir(exist_ok=True)
            root = Path(mkdtemp(prefix="update-", dir=updates_dir))
            try:
                archive = root / "update.zip"
                self.github("release", "download", f"v{release.version}", "--repo", f"github.com/{REPOSITORY}",
                            "--pattern", release.name, "--output", str(archive), timeout=300)
                candidate = root / "payload"
                extract_update(archive, candidate, release)
                archive.unlink()
                with self._lock:
                    if self._requests:
                        raise UpdateError("Дождитесь завершения текущих действий и повторите обновление.")
                    self._state = "restarting"
                    self._restarted_at = monotonic()
                verify_idle()
                powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
                try:
                    subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-File", str(self.worker), "-CandidatePath", str(candidate),
                                    "-InstallRoot", str(self.install_root), "-ExpectedVersion", release.version, "-Schedule"],
                                   creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=25, check=True)
                except subprocess.TimeoutExpired:
                    # Windows may have started the separate task before the scheduler call timed out.
                    return
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise
        except UpdateError as error:
            with self._lock:
                self._state, self._message = "failed", str(error)
        except (OSError, ValueError, KeyError, BadZipFile, RuntimeError, subprocess.SubprocessError):
            with self._lock:
                self._state, self._message = "failed", "Не удалось подготовить обновление. Попробуйте ещё раз."
