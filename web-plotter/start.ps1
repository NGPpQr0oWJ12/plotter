[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$checkout = Split-Path -Parent $PSScriptRoot
$version = (Get-Content -LiteralPath (Join-Path $checkout 'VERSION') -Raw).Trim()
$defaultPorts = @(8765, 8766, 8768, 8769)
$portRequested = $PSBoundParameters.ContainsKey('Port')
if ($portRequested -and $Port -eq 8767) {
    throw 'Порт 8767 зарезервирован для локального помощника Plotter.'
}
$probePorts = if ($portRequested -and $Port -notin $defaultPorts) { $defaultPorts + $Port } else { $defaultPorts }
try {
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object {
        $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::')
    } | Select-Object -ExpandProperty LocalPort -Unique)
} catch {
    $listeners = @([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
        Where-Object { $_.Address.ToString() -in @('127.0.0.1', '0.0.0.0', '::') } |
        Select-Object -ExpandProperty Port -Unique)
}
$runningApps = @()
foreach ($candidate in $probePorts) {
    if ($candidate -notin $listeners) { continue }
    try {
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:$candidate/api/status" -TimeoutSec 2 -MaximumRedirection 0
    } catch {
        continue
    }
    if ($status.mode -eq 'controller' -and $status.apiVersion -and $status.machine.ip -and
        !$status.centralMode -and !$status.companionMode) {
        $runningApps += [pscustomobject]@{ Port = $candidate; Status = $status }
    }
}
if ($runningApps.Count) {
    $running = $runningApps[0]
    if ($runningApps.Count -eq 1 -and (!$portRequested -or $Port -eq $running.Port) -and
        $running.Status.appVersion -ceq $version) {
        Start-Process "http://127.0.0.1:$($running.Port)/"
        exit 0
    }
    $runningPorts = ($runningApps | Select-Object -ExpandProperty Port) -join ', '
    Write-Host "Локальный Plotter уже запущен на порту $runningPorts."
    Write-Host 'Если нужна другая версия или другой порт, остановите станок, закройте прежнее окно запуска и снова запустите Plotter.'
    exit 1
}
if (!$portRequested) { $Port = $defaultPorts | Where-Object { $_ -notin $listeners } | Select-Object -First 1 }
if (!$Port -or $Port -in $listeners) {
    throw 'Нет свободного порта для Plotter. Укажите другой порт, например «Запустить плоттер.cmd -Port 8770».'
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

$installed = & $runtime -c "from importlib.metadata import distributions; from pathlib import Path; installed = {d.metadata['Name'].lower(): d.version for d in distributions()}; print(all(installed.get(name.lower()) == version for name, version in (line.split('==') for line in Path('requirements.txt').read_text().splitlines() if '==' in line)))"
if ($LASTEXITCODE -ne 0 -or $installed -ne 'True') {
    & $runtime -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the drawing dependencies.' }
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

$serverArguments = @('server.py', '--port', $Port, '--open')
& $runtime @serverArguments
exit $LASTEXITCODE
