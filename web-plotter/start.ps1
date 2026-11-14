$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$port = 8765
$url = "http://127.0.0.1:$port"
$running = $null
try {
    $running = Invoke-RestMethod -Uri "$url/api/status" -TimeoutSec 2
} catch {
    $running = $null
}
if ($running.mode -eq 'controller' -and $running.apiVersion -eq 21 -and $running.demoAvailable -and $running.machine.directRoll -and $running.machine.continuousRoll -and $running.machine.stopDuringExternalRun -and $running.machine.liveCutProgress -and $running.machine.coordinateMove -and $running.machine.optimizedNesting -and $running.machine.ip) {
    Start-Process $url
    exit 0
}
if ($running.machine.ip) {
    Write-Host 'На ПК уже запущена предыдущая версия программы плоттера.'
    Write-Host 'Когда станок остановлен, закройте первое окно запуска и снова откройте «Запустить плоттер.cmd».'
    exit 1
}

$runtime = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $runtime)) {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $launcher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    $standard = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $bundled) {
        & $bundled -m venv .venv
    } elseif ($launcher) {
        & $launcher.Source -3 -m venv .venv
    } elseif ($standard) {
        & $standard.Source -m venv .venv
    } else {
        throw 'Python 3.10+ was not found. Install Python, then run this launcher again.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python environment.' }
}

$installed = & $runtime -c "from importlib.metadata import version; from importlib.util import find_spec; print(version('ezdxf') if find_spec('ezdxf') else '')"
if ($LASTEXITCODE -ne 0 -or $installed -ne '1.4.5') {
    & $runtime -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the drawing import dependency.' }
}

$toolDirectory = Join-Path $PSScriptRoot '.tools\libredwg'
$converter = Join-Path $toolDirectory 'dwg2dxf.exe'
if (!(Test-Path -LiteralPath $converter) -and !$env:PLOTTER_DWG2DXF -and !(Get-Command 'dwg2dxf.exe' -ErrorAction SilentlyContinue)) {
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ("plotter-libredwg-" + [guid]::NewGuid().ToString('N') + '.zip')
    try {
        Write-Host 'Downloading the local DWG converter (LibreDWG 0.14)...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 60 -Uri 'https://github.com/LibreDWG/libredwg/releases/download/0.14/libredwg-0.14-win64.zip' -OutFile $temporary
        $expected = '1AD7E15344D20B3426C3435B078D82FB84B35062815946B2CCA9C5FC9810FEA8'
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $temporary).Hash -ne $expected) { throw 'LibreDWG checksum mismatch.' }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        New-Item -ItemType Directory -Force -Path $toolDirectory | Out-Null
        $archive = [IO.Compression.ZipFile]::OpenRead($temporary)
        try {
            foreach ($name in @('libiconv-2.dll', 'libpcre2-16-0.dll', 'libpcre2-8-0.dll', 'libredwg-0.dll', 'dwg2dxf.exe')) {
                $entry = $archive.GetEntry($name)
                if (!$entry) { throw "LibreDWG file is missing: $name" }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $toolDirectory $name), $true)
            }
        } finally {
            $archive.Dispose()
        }
    } catch {
        Write-Warning "DWG converter setup failed. DXF import is available. $($_.Exception.Message)"
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

& $runtime server.py --port $port --open
exit $LASTEXITCODE
