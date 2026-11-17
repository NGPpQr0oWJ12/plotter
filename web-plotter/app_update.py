"""Download and stage trusted releases for the Windows operator panel."""

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
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile


VERSION_PATTERN = re.compile(r"(?:0|[1-9]\d{0,5})\.(?:0|[1-9]\d{0,5})\.(?:0|[1-9]\d{0,5})", re.ASCII)
REPOSITORY = "NGPpQr0oWJ12/plotter"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases"
RELEASE_PAGE_SIZE = 30
DOWNLOAD_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"})
MAX_METADATA = 512 * 1024
MAX_ARCHIVE = 160 * 1024 * 1024
MAX_EXPANDED = 480 * 1024 * 1024
MAX_COMPANION_REVISION = 2_147_483_647
REVISION_FILE = "COMPANION_REVISION"
CENTRAL_MANIFEST = "/api/companion-update"
CENTRAL_ARCHIVE = "/api/companion-update.zip"
MAX_CENTRAL_MANIFEST = 4096
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


def parse_companion_revision(contents):
    try:
        value = contents.decode("ascii").strip()
    except UnicodeError as error:
        raise UpdateError("Некорректная ревизия помощника.") from error
    if len(contents) > 16 or not re.fullmatch(r"0|[1-9][0-9]{0,9}", value) or int(value) > MAX_COMPANION_REVISION:
        raise UpdateError("Некорректная ревизия помощника.")
    return int(value)


def read_companion_revision(root):
    for path in (root / REVISION_FILE, root.parent / REVISION_FILE):
        try:
            with path.open("rb") as marker:
                return parse_companion_revision(marker.read(17))
        except FileNotFoundError:
            continue
    return 0


def read_release_notes(root, version):
    if not release_version(version):
        return []
    for path in (root / "CHANGELOG.md", root.parent / "CHANGELOG.md"):
        try:
            with path.open(encoding="utf-8-sig") as changelog:
                contents = changelog.read(256 * 1024)
        except (OSError, UnicodeError):
            continue
        active = False
        notes = []
        for line in contents.splitlines():
            if line.startswith("## "):
                if active:
                    break
                active = line[3:].strip() == version
            elif active and line.startswith("- "):
                text = line[2:].strip()
                if text:
                    notes.append(text[:1000])
        if active:
            return notes[:30]
    return []


@dataclass(frozen=True)
class Release:
    version: str
    name: str
    size: int
    digest: str
    revision: int = 0
    central: bool = False

    @property
    def build(self):
        return release_version(self.version), self.revision


class GitHubRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if (url.scheme != "https" or url.netloc not in DOWNLOAD_HOSTS
                or urlsplit(req.full_url).netloc not in DOWNLOAD_HOSTS):
            raise UpdateError("GitHub перенаправил загрузку на неожиданный адрес.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_release_page(opener, page):
    url = f"{RELEASES_URL}?per_page={RELEASE_PAGE_SIZE}&page={page}"
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Xiao-Plotter-Updater"})
    try:
        with opener.open(request, timeout=15) as response:
            contents = response.read(MAX_METADATA + 1)
    except HTTPError as error:
        with error:
            if error.code == 429 or error.headers.get("X-RateLimit-Remaining") == "0":
                raise UpdateError("GitHub временно ограничил проверки. Повторим позже.") from error
        raise UpdateError("Не удалось проверить релизы GitHub. Проверьте подключение к интернету.") from error
    if len(contents) > MAX_METADATA:
        raise UpdateError("Ответ GitHub о релизе слишком велик.")
    releases = json.loads(contents)
    if not isinstance(releases, list) or len(releases) > RELEASE_PAGE_SIZE:
        raise UpdateError("GitHub вернул некорректный список релизов.")
    return releases


def find_companion_update(opener, current):
    newest = None
    for page in range(1, 5):
        releases = fetch_release_page(opener, page)
        for metadata in releases:
            candidate = latest_update(metadata, newest.version if newest else current)
            if candidate is not None:
                newest = candidate
        if len(releases) < RELEASE_PAGE_SIZE:
            break
    return newest


def fetch_central_update(opener, origin):
    request = Request(origin + CENTRAL_MANIFEST, headers={"Accept": "application/json", "User-Agent": "Xiao-Plotter-Updater"})
    try:
        with opener.open(request, timeout=10) as response:
            contents = response.read(MAX_CENTRAL_MANIFEST + 1)
    except HTTPError as error:
        with error:
            if error.code == 404:
                return None
        raise UpdateError("Не удалось проверить обновление на центральной панели.") from error
    if len(contents) > MAX_CENTRAL_MANIFEST:
        raise UpdateError("Ответ центральной панели об обновлении слишком велик.")
    metadata = json.loads(contents)
    if isinstance(metadata, dict):
        version, revision = metadata.get("version"), metadata.get("revision")
        size, digest = metadata.get("size"), metadata.get("digest")
        if (release_version(version) and type(revision) is int and 0 <= revision <= MAX_COMPANION_REVISION
                and type(size) is int and 0 < size <= MAX_ARCHIVE
                and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)):
            return Release(version, "companion-update.zip", size, digest, revision, central=True)
    raise UpdateError("Центральная панель вернула некорректные сведения об обновлении.")


def download_release(opener, release, destination, *, central_origin=None):
    url = (central_origin + CENTRAL_ARCHIVE if central_origin is not None else
           f"https://github.com/{REPOSITORY}/releases/download/v{release.version}/{release.name}")
    request = Request(url, headers={"User-Agent": "Xiao-Plotter-Updater"})
    deadline = monotonic() + 300
    remaining = release.size
    with opener.open(request, timeout=20) as response, destination.open("xb") as output:
        while True:
            if monotonic() > deadline:
                raise UpdateError("Истекло время скачивания обновления.")
            chunk = response.read(min(remaining + 1, 1024 * 1024))
            if len(chunk) > remaining:
                raise UpdateError("Размер архива обновления отличается от заявленного.")
            if not chunk:
                break
            output.write(chunk)
            remaining -= len(chunk)
    if remaining:
        raise UpdateError("Скачивание архива обновления прервалось.")


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
        revision_entry = files.get(REVISION_FILE.casefold())
        revision = 0
        if revision_entry is not None:
            if revision_entry.filename != REVISION_FILE or revision_entry.file_size > 16:
                raise UpdateError("Некорректная ревизия в архиве помощника.")
            revision = parse_companion_revision(archive.read(revision_entry))
        if (release.central or release.revision) and revision != release.revision:
            raise UpdateError("Ревизия в архиве отличается от ревизии обновления.")
        for entry in files.values():
            destination = target.joinpath(*entry.filename.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)
        return revision


class AppUpdates:
    def __init__(self, app_root, version, install_root=None, central_source=None):
        self.worker = app_root / "apply-companion-update.ps1"
        self.version = version
        self.central_source = central_source
        self.install_root = install_root
        self.supported = bool(install_root and version and self.worker.is_file())
        self.revision = 0
        self._lock = Lock()
        self._requests = 0
        self._state = "idle"
        self._message = None
        self._available = None
        self._check_after = 0
        self._checking = False
        self._restarted_at = 0
        try:
            self.revision = read_companion_revision(app_root)
        except (UpdateError, OSError):
            self.supported = False
            self._message = "Не удалось прочитать ревизию установленного помощника. Переустановите помощник."

    def _refresh(self):
        central = None
        if self.central_source:
            try:
                central = fetch_central_update(*self.central_source)
            except (OSError, ValueError, TypeError):
                pass
        try:
            if central is not None:
                available = central if central.build > (release_version(self.version), self.revision) else None
            else:
                available = find_companion_update(build_opener(GitHubRedirect()), self.version)
            message = None
        except UpdateError as error:
            available, message = None, str(error)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            available, message = None, "Не удалось проверить релизы GitHub."
        with self._lock:
            self._available = available
            if self._state == "idle":
                self._message = message
            self._checking = False
            self._check_after = monotonic() + (1800 if message else 900)

    def status(self):
        with self._lock:
            state, message = self._state, self._message
            if self.supported:
                result_path = self.install_root / "update-result.json"
                try:
                    if result_path.stat().st_size > 4096:
                        raise ValueError("Invalid update state")
                    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
                    version = release_version(result.get("version"))
                    revision = result.get("revision", 0)
                    build = (version, revision) if version and type(revision) is int and 0 <= revision <= MAX_COMPANION_REVISION else None
                    current = release_version(self.version), self.revision
                    if (state != "preparing" and result.get("state") == "failed"
                            and (build > current if build else state == "restarting" and "version" not in result)):
                        state = self._state = "failed"
                        message = self._message = str(result.get("message") or "Обновление не завершилось.")[:500]
                    if result.get("state") == "done" and build == current:
                        state = "done"
                except (OSError, ValueError, AttributeError):
                    pass
                if state == "restarting" and monotonic() - self._restarted_at > 11 * 60:
                    state = self._state = "failed"
                    message = self._message = "Windows не завершила перезапуск помощника. Повторите обновление."
                if state not in ("preparing", "restarting") and not self._checking and monotonic() >= self._check_after:
                    self._checking = True
                    Thread(target=self._refresh, daemon=True).start()
            return {"version": self.version, "revision": self.revision, "supported": self.supported,
                    "availableVersion": self._available.version if self._available else None,
                    "availableRevision": self._available.revision if self._available else None,
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

    def start(self, version, verify_idle, revision=None):
        with self._lock:
            if not self.supported:
                raise UpdateError("Обновление доступно в установленной панели станка.")
            if self._state in ("preparing", "restarting"):
                raise UpdateError("Обновление уже выполняется.")
            if (not self._available or version != self._available.version
                    or (revision is not None and (type(revision) is not int or revision != self._available.revision))):
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
                if release.central:
                    opener, origin = self.central_source
                    download_release(opener, release, archive, central_origin=origin)
                else:
                    download_release(build_opener(GitHubRedirect()), release, archive)
                candidate = root / "payload"
                revision = extract_update(archive, candidate, release)
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
                                    "-InstallRoot", str(self.install_root), "-ExpectedVersion", release.version,
                                    "-ExpectedRevision", str(revision), "-Schedule"],
                                   creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=25, check=True)
                except subprocess.TimeoutExpired:
                    # Windows may have started the separate task before the scheduler call timed out.
                    return
                except subprocess.CalledProcessError as error:
                    raise UpdateError(f"Windows не запустила задачу обновления помощника (код {error.returncode}).") from error
                except OSError as error:
                    raise UpdateError("Не удалось запустить PowerShell для обновления помощника.") from error
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise
        except UpdateError as error:
            with self._lock:
                self._state, self._message = "failed", str(error)
        except HTTPError as error:
            error.close()
            with self._lock:
                source = "центральной панели" if release.central else "GitHub"
                self._state, self._message = "failed", f"Не удалось скачать обновление с {source}."
        except (OSError, ValueError, KeyError, BadZipFile, RuntimeError, subprocess.SubprocessError):
            with self._lock:
                self._state, self._message = "failed", "Не удалось подготовить обновление. Попробуйте ещё раз."
