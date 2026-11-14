[CmdletBinding()]
param(
    [string]$CentralUrl,
    [string]$ProfilePath,
    [string]$CentralCa,
    [ValidatePattern('^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')]
    [string]$ExpectedVersion,
    [switch]$Graphical
)

$ErrorActionPreference = 'Stop'
$port = 8767
if ($Graphical) {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    Add-Type -AssemblyName System.Windows.Forms
    . (Join-Path $PSScriptRoot 'companion-profile.ps1')
}

function Get-CentralOrigin([string]$Value) {
    $uri = $null
    if ($Value -notmatch '^https://[^/?#\\@\s"%]+/?$' -or
        ![uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -or
        !$uri.Host -or $uri.UserInfo -or $uri.Port -lt 1) {
        throw 'Enter an HTTPS server address with an optional port and no path, query, or credentials.'
    }
    $authority = $uri.IdnHost
    if ($uri.HostNameType -eq [UriHostNameType]::IPv6) { $authority = $uri.Host }
    if (!$uri.IsDefaultPort) { $authority += ":$($uri.Port)" }
    return "https://$authority"
}

function Test-ControllerAddress([string]$Value) {
    $address = $null
    return ($Value -match '^(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}$' -and
        [Net.IPAddress]::TryParse($Value, [ref]$address) -and
        ![Net.IPAddress]::IsLoopback($address) -and $address.GetAddressBytes()[0] -ne 0 -and
        $address.GetAddressBytes()[0] -lt 224)
}

function Get-MachineProfile([string]$Path) {
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Select the machine-profile.json file from this PC with working controller settings.'
    }
    $resolved = (Resolve-Path -LiteralPath $Path).ProviderPath
    $content = [IO.File]::ReadAllText($resolved)
    try {
        $profile = ConvertFrom-Json -InputObject $content
    } catch {
        throw 'The selected machine profile is not valid JSON.'
    }
    $computer = $profile.observedComputerConfiguration
    if (!(Test-ControllerAddress $computer.machineIp)) {
        throw 'The profile must already contain the real IPv4 address of the controller.'
    }
    $area = $computer.workrectMm
    if ($null -ne $profile.observedControllerConfiguration.workrectMm) {
        $area = $profile.observedControllerConfiguration.workrectMm
    }
    foreach ($axis in @($area.x, $area.y)) {
        if ($axis -isnot [ValueType] -or $axis -is [bool] -or
            [double]::IsNaN($axis) -or [double]::IsInfinity($axis) -or $axis -le 0) {
            throw 'The selected machine profile needs positive X and Y work dimensions.'
        }
    }
    if ($profile.software.path -isnot [string] -or
        ($profile.software.path -and $profile.software.path -notmatch '^(?:[A-Za-z]:[\\/]|\\\\)')) {
        throw 'The selected machine profile needs the absolute path to Xiao on this PC.'
    }
    if ($profile.software.path -and !(Test-Path -LiteralPath $profile.software.path -PathType Leaf)) {
        Write-Warning 'Xiao was not found at the path in the selected profile. Check this path if Xiao handoff is used.'
    }
    return $content
}

function Get-CentralStatus([string]$Python, [string]$Origin, [string]$CaPath) {
    $program = @'
from http.client import HTTPSConnection
from urllib.parse import urlsplit
import json
import ssl
import sys

origin = urlsplit(sys.argv[1])
context = ssl.create_default_context(cafile=sys.argv[2])
connection = HTTPSConnection(origin.hostname, origin.port, timeout=6, context=context)
try:
    connection.request("GET", "/api/status")
    with connection.getresponse() as response:
        if response.status != 200:
            raise ValueError("Unexpected server status: " + str(response.status))
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("Server status response was too large")
    status = json.loads(payload)
    if status.get("centralMode") is not True:
        raise ValueError("This address does not serve the central Plotter panel")
    print(json.dumps(status))
finally:
    connection.close()
'@
    $result = $program | & $Python - $Origin $CaPath
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось подтвердить подключение к центральной панели по HTTPS.' }
    return $result | ConvertFrom-Json
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = "Xiao Plotter Companion ($($identity.User.Value))"
$taskPath = '\'
$installRoot = Join-Path $env:LOCALAPPDATA 'Plotter\Companion'
$installStatePath = Join-Path $installRoot 'install.json'
$update = $PSBoundParameters.ContainsKey('ExpectedVersion')
$versionPath = Join-Path $PSScriptRoot 'VERSION'
$version = if (Test-Path -LiteralPath $versionPath -PathType Leaf) {
    (Get-Content -LiteralPath $versionPath -Raw).Trim()
}
$saved = $null
if (Test-Path -LiteralPath $installStatePath -PathType Leaf) {
    $saved = Get-Content -LiteralPath $installStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
}

if ($update) {
    if ($Graphical -or $CentralUrl -or $ProfilePath -or $CentralCa -or $version -cne $ExpectedVersion -or
        !$saved.origin -or $saved.release -notmatch '^app-[a-f0-9]{32}$') {
        throw 'The update does not match the installed companion or the expected release.'
    }
    $previousPath = Join-Path $installRoot $saved.release
    $CentralUrl = $saved.origin
    $ProfilePath = Join-Path $previousPath 'machine-profile.json'
    $CentralCa = Join-Path $previousPath 'central-ca.pem'
}
if (!$CentralUrl) {
    $prompt = 'HTTPS address of the central Plotter server'
    if ($saved.origin) { $prompt += ' (Enter to keep the current address)' }
    $CentralUrl = Read-Host $prompt
    if (!$CentralUrl) { $CentralUrl = $saved.origin }
}
$origin = Get-CentralOrigin $CentralUrl.Trim()
$bundledCa = Join-Path $PSScriptRoot 'central-ca.pem'
if ($Graphical) {
    $CentralCa = $bundledCa
} elseif (!$CentralCa -and $saved.origin -eq $origin -and $saved.release -match '^app-[a-f0-9]{32}$') {
    $previousCa = Join-Path $installRoot "$($saved.release)\central-ca.pem"
    if (Test-Path -LiteralPath $previousCa -PathType Leaf) { $CentralCa = $previousCa }
}
if ($CentralCa -and !(Test-Path -LiteralPath $CentralCa -PathType Leaf)) {
    throw 'The central CA certificate file was not found.'
}

if (!$ProfilePath) {
    $defaultProfile = Join-Path (Split-Path $PSScriptRoot -Parent) 'research\machine-profile.json'
    if ($Graphical -and $env:PLOTTER_SETUP_DIRECTORY) {
        $defaultProfile = Join-Path $env:PLOTTER_SETUP_DIRECTORY 'research\machine-profile.json'
    }
    if ($saved.release -match '^app-[a-f0-9]{32}$') {
        $defaultProfile = Join-Path $installRoot "$($saved.release)\machine-profile.json"
    }
    if (!$Graphical) {
        $prompt = 'Path to your configured machine-profile.json'
        if (Test-Path -LiteralPath $defaultProfile -PathType Leaf) { $prompt += " [Enter for $defaultProfile]" }
        $ProfilePath = Read-Host $prompt
        if (!$ProfilePath) { $ProfilePath = $defaultProfile }
    }
}
$profileContent = if ($ProfilePath -or !$Graphical) { Get-MachineProfile (([string]$ProfilePath).Trim().Trim('"')) }

$previous = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
$wasRunning = $previous.State -eq 'Running'
if ($update -and !$wasRunning) {
    throw 'The installed companion task is no longer running.'
}
if ($wasRunning -and !$Graphical -and !$update) {
    throw 'The companion is running. Once the machine is idle, end the Xiao Plotter Companion task in Task Scheduler and rerun this installer.'
}
$listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
if ($listener -and !$wasRunning) {
    throw "Local port $port is already in use. Close the program using it before installing the companion."
}

if ($Graphical) {
    $message = "Установить помощник Plotter для $origin?" + [Environment]::NewLine + 'Станок должен быть остановлен.'
    if ([Windows.Forms.MessageBox]::Show($message, 'Помощник Plotter', 'OKCancel', 'Question') -ne 'OK') { exit 2 }
}

$packedRuntime = Join-Path $PSScriptRoot 'runtime'
$hasPackedRuntime = Test-Path -LiteralPath (Join-Path $packedRuntime 'pythonw.exe') -PathType Leaf
if (($Graphical -or $update) -and !$hasPackedRuntime) { throw 'Во встроенном комплекте отсутствует Python.' }
$environmentPath = Join-Path $installRoot '.venv'
$runtime = Join-Path $environmentPath 'Scripts\python.exe'
$runtimeHidden = Join-Path $environmentPath 'Scripts\pythonw.exe'
if (!$hasPackedRuntime -and !(Test-Path -LiteralPath $runtime -PathType Leaf)) {
    $sourceRuntime = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $launcher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    $standard = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $bundled -PathType Leaf) {
        & $bundled -m venv $environmentPath
    } elseif ($launcher) {
        & $launcher.Source -3 -m venv $environmentPath
    } elseif (Test-Path -LiteralPath $sourceRuntime -PathType Leaf) {
        & $sourceRuntime -m venv $environmentPath
    } elseif ($standard) {
        & $standard.Source -m venv $environmentPath
    } else {
        throw 'Python 3.10 or later is required on this PC.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the companion Python environment.' }
}
if (!$hasPackedRuntime) {
    if (!(Test-Path -LiteralPath $runtimeHidden -PathType Leaf)) {
        throw 'The companion Python environment has no pythonw.exe.'
    }
    & $runtime -c 'import sys; sys.exit(sys.version_info < (3, 10))'
    if ($LASTEXITCODE -ne 0) { throw 'The companion needs a working Python 3.10 or later environment.' }
    & $runtime -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the companion dependencies.' }
}

$release = 'app-' + [guid]::NewGuid().ToString('N')
$applicationPath = Join-Path $installRoot $release
New-Item -ItemType Directory -Path $applicationPath -Force | Out-Null
if ($hasPackedRuntime) {
    Copy-Item -LiteralPath $packedRuntime -Destination $applicationPath -Recurse
    $runtime = Join-Path $applicationPath 'runtime\python.exe'
    $runtimeHidden = Join-Path $applicationPath 'runtime\pythonw.exe'
}
Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.py' |
    Where-Object Name -NotLike 'test_*' |
    Copy-Item -Destination $applicationPath
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'static') -Destination $applicationPath -Recurse
$updateWorker = Join-Path $PSScriptRoot 'apply-companion-update.ps1'
foreach ($asset in @($versionPath, $updateWorker)) {
    if (Test-Path -LiteralPath $asset -PathType Leaf) {
        Copy-Item -LiteralPath $asset -Destination $applicationPath
    } elseif ($Graphical -or $update) {
        throw 'The companion package is missing a version or update worker.'
    }
}
$installedProfile = Join-Path $applicationPath 'machine-profile.json'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$server = Join-Path $applicationPath 'server.py'
$serverOptions = & $runtime $server --help | Out-String
if ($LASTEXITCODE -ne 0 -or $serverOptions -notmatch '--companion' -or
    $serverOptions -notmatch '--profile' -or $serverOptions -notmatch '--central-url' -or
    ($CentralCa -and $serverOptions -notmatch '--central-ca')) {
    throw 'This package cannot start a companion. Use the complete companion release.'
}

$installedCa = $null
if ($CentralCa) {
    $installedCa = Join-Path $applicationPath 'central-ca.pem'
    Copy-Item -LiteralPath $CentralCa -Destination $installedCa
}
$workspace = Join-Path $installRoot 'workspace.json'
$arguments = '"{0}" --companion --port {1} --profile "{2}" --central-url "{3}" --workspace "{4}"' -f $server, $port, $installedProfile, $origin, $workspace
if ($installedCa) { $arguments += ' --central-ca "{0}"' -f $installedCa }
$action = New-ScheduledTaskAction -Execute $runtimeHidden -Argument $arguments -WorkingDirectory $applicationPath
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$principal = New-ScheduledTaskPrincipal -UserId $identity.User.Value -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

function Register-CompanionTask {
    $parameters = @{
        TaskName = $taskName
        TaskPath = $taskPath
        Action = $action
        Trigger = $trigger
        Principal = $principal
        Settings = $settings
        Description = 'Runs the operator computer Plotter companion on loopback after signing in.'
    }
    Register-ScheduledTask @parameters -Force | Out-Null
}

$taskUpdated = $false
$stopRequested = $false
try {
    if ($installedCa) {
        $status = Get-CentralStatus $runtime $origin $installedCa
    }
    if ($Graphical) {
        if (!$profileContent -and (Test-Path -LiteralPath $defaultProfile -PathType Leaf)) {
            try { $profileContent = Get-MachineProfile $defaultProfile }
            catch { Write-Warning $_.Exception.Message }
        }
        if (!$profileContent) {
            $profileContent = Show-MachineSetup $status.machine
            if (!$profileContent) { exit 2 }
        }
        $profile = ConvertFrom-Json -InputObject $profileContent
        $area = $profile.observedControllerConfiguration.workrectMm
        if (!$area) { $area = $profile.observedComputerConfiguration.workrectMm }
        if ($area.x -ne $status.machine.width -or $area.y -ne $status.machine.height) {
            throw 'Размеры рабочего поля в профиле станка отличаются от настроек панели.'
        }
    }
    [IO.File]::WriteAllText($installedProfile, $profileContent, $utf8)
    if ($wasRunning) {
        $stopRequested = $true
        Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            if (!(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 250
        }
        if ($attempt -eq 20) { throw 'Прежний помощник не освободил порт. Закройте его и повторите установку.' }
    }
    if ($saved.release -match '^app-[a-f0-9]{32}$') {
        $oldJournal = Join-Path $installRoot "$($saved.release)\cut-journal.json"
        $journal = Join-Path $installRoot 'cut-journal.json'
        if ((Test-Path -LiteralPath $oldJournal -PathType Leaf) -and !(Test-Path -LiteralPath $journal)) {
            Copy-Item -LiteralPath $oldJournal -Destination $journal
        }
    }
    Register-CompanionTask
    $taskUpdated = $true
    Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath
    $ready = $false
    $statusRequest = @{ Uri = "http://127.0.0.1:$port/api/status"; MaximumRedirection = 0; TimeoutSec = 1 }
    if (!$installedCa) { $statusRequest.Headers = @{ Origin = $origin } }
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            $status = Invoke-RestMethod @statusRequest
            if ($status.companionMode -eq $true -and (!$version -or $status.appVersion -ceq $version)) {
                $ready = $true
                break
            }
        } catch {
            # Task Scheduler starts the companion asynchronously.
        }
        Start-Sleep -Milliseconds 500
    }
    if (!$ready) { throw 'The installed companion did not report the expected version.' }
    $state = @{ origin = $origin; release = $release } | ConvertTo-Json -Compress
    $pendingState = Join-Path $installRoot "$release.json.tmp"
    try {
        [IO.File]::WriteAllText($pendingState, $state, $utf8)
        if (Test-Path -LiteralPath $installStatePath -PathType Leaf) {
            [IO.File]::Replace($pendingState, $installStatePath, [NullString]::Value)
        } else {
            [IO.File]::Move($pendingState, $installStatePath)
        }
    } finally {
        if (Test-Path -LiteralPath $pendingState -PathType Leaf) {
            Remove-Item -LiteralPath $pendingState -ErrorAction SilentlyContinue
        }
    }
} catch {
    $failure = $_
    $cleanupWarnings = @()
    if ($taskUpdated) {
        Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
        try {
            if ($previous) {
                Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -InputObject $previous -Force | Out-Null
            } else {
                Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false
            }
        } catch { $cleanupWarnings += $_.Exception.Message }
    }
    if ($stopRequested -and !$cleanupWarnings.Count) {
        try { Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath }
        catch { $cleanupWarnings += $_.Exception.Message }
    }
    if ($cleanupWarnings.Count) {
        throw ($failure.Exception.Message + [Environment]::NewLine + ($cleanupWarnings -join [Environment]::NewLine))
    }
    throw $failure
}

if ($Graphical) {
    try {
        $programs = [Environment]::GetFolderPath('Programs')
        if (!$programs) { throw 'Start menu is unavailable.' }
        $shortcut = "[InternetShortcut]`r`nURL=http://127.0.0.1:$port/`r`n"
        [IO.File]::WriteAllText((Join-Path $programs 'Plotter.url'), $shortcut, [Text.Encoding]::ASCII)
    } catch {
        Write-Warning 'Не удалось создать ярлык Plotter в меню «Пуск». Панель доступна по адресу http://127.0.0.1:8767/.'
    }
}
Write-Host 'Plotter companion is running and will start when this Windows user signs in.'
