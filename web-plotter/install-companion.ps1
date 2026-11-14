[CmdletBinding()]
param(
    [string]$CentralUrl,
    [string]$ProfilePath
)

$ErrorActionPreference = 'Stop'
$port = 8767

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
    $address = $null
    if ($computer.machineIp -notmatch '^(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}$' -or
        ![Net.IPAddress]::TryParse($computer.machineIp, [ref]$address) -or
        [Net.IPAddress]::IsLoopback($address) -or $address.GetAddressBytes()[0] -eq 0 -or
        $address.GetAddressBytes()[0] -ge 224) {
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
    if ($profile.software.path -isnot [string] -or $profile.software.path -notmatch '^(?:[A-Za-z]:[\\/]|\\\\)') {
        throw 'The selected machine profile needs the absolute path to Xiao on this PC.'
    }
    if (!(Test-Path -LiteralPath $profile.software.path -PathType Leaf)) {
        Write-Warning 'Xiao was not found at the path in the selected profile. Check this path if Xiao handoff is used.'
    }
    return $content
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = "Xiao Plotter Companion ($($identity.User.Value))"
$taskPath = '\'
$installRoot = Join-Path $env:LOCALAPPDATA 'Plotter\Companion'
$installStatePath = Join-Path $installRoot 'install.json'
$saved = $null
if (Test-Path -LiteralPath $installStatePath -PathType Leaf) {
    $saved = Get-Content -LiteralPath $installStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
}

if (!$CentralUrl) {
    $prompt = 'HTTPS address of the central Plotter server'
    if ($saved.origin) { $prompt += ' (Enter to keep the current address)' }
    $CentralUrl = Read-Host $prompt
    if (!$CentralUrl) { $CentralUrl = $saved.origin }
}
$origin = Get-CentralOrigin $CentralUrl.Trim()

if (!$ProfilePath) {
    $defaultProfile = Join-Path (Split-Path $PSScriptRoot -Parent) 'research\machine-profile.json'
    if ($saved.release -match '^app-[a-f0-9]{32}$') {
        $defaultProfile = Join-Path $installRoot "$($saved.release)\machine-profile.json"
    }
    $prompt = 'Path to your configured machine-profile.json'
    if (Test-Path -LiteralPath $defaultProfile -PathType Leaf) { $prompt += " [Enter for $defaultProfile]" }
    $ProfilePath = Read-Host $prompt
    if (!$ProfilePath) { $ProfilePath = $defaultProfile }
}
$profileContent = Get-MachineProfile $ProfilePath.Trim().Trim('"')

$previous = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
if ($previous.State -eq 'Running') {
    throw 'The companion is running. Once the machine is idle, end the Xiao Plotter Companion task in Task Scheduler and rerun this installer.'
}
$listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
if ($listener) {
    throw "Local port $port is already in use. Close the program using it before installing the companion."
}

$environmentPath = Join-Path $installRoot '.venv'
$runtime = Join-Path $environmentPath 'Scripts\python.exe'
$runtimeHidden = Join-Path $environmentPath 'Scripts\pythonw.exe'
if (!(Test-Path -LiteralPath $runtime -PathType Leaf)) {
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
if (!(Test-Path -LiteralPath $runtimeHidden -PathType Leaf)) {
    throw 'The companion Python environment has no pythonw.exe.'
}
& $runtime -c 'import sys; sys.exit(sys.version_info < (3, 10))'
if ($LASTEXITCODE -ne 0) { throw 'The companion needs a working Python 3.10 or later environment.' }
& $runtime -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install the companion dependencies.' }

$release = 'app-' + [guid]::NewGuid().ToString('N')
$applicationPath = Join-Path $installRoot $release
New-Item -ItemType Directory -Path $applicationPath | Out-Null
Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.py' |
    Where-Object Name -NotLike 'test_*' |
    Copy-Item -Destination $applicationPath
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'static') -Destination $applicationPath -Recurse
$installedProfile = Join-Path $applicationPath 'machine-profile.json'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($installedProfile, $profileContent, $utf8)
$server = Join-Path $applicationPath 'server.py'
$serverOptions = & $runtime $server --help | Out-String
if ($LASTEXITCODE -ne 0 -or $serverOptions -notmatch '--companion' -or
    $serverOptions -notmatch '--profile' -or $serverOptions -notmatch '--central-url') {
    throw 'This package cannot start a companion. Use the complete companion release.'
}

$arguments = '"{0}" --companion --port {1} --profile "{2}" --central-url "{3}"' -f $server, $port, $installedProfile, $origin
$action = New-ScheduledTaskAction -Execute $runtimeHidden -Argument $arguments -WorkingDirectory $applicationPath
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$principal = New-ScheduledTaskPrincipal -UserId $identity.User.Value -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Runs the operator computer Plotter companion on loopback after signing in.' -Force | Out-Null
try {
    Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/status" -Headers @{ Origin = $origin } `
                -MaximumRedirection 0 -TimeoutSec 1
            if ($status.companionMode -eq $true) {
                $ready = $true
                break
            }
        } catch {
            # Task Scheduler starts the companion asynchronously.
        }
        Start-Sleep -Milliseconds 500
    }
    if (!$ready) { throw 'The installed task started but the local companion did not answer.' }
    $state = @{ origin = $origin; release = $release } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($installStatePath, $state, $utf8)
} catch {
    Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    if ($previous) {
        Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -InputObject $previous -Force | Out-Null
    } else {
        Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false
    }
    throw
}

Write-Host 'Plotter companion is running and will start when this Windows user signs in.'
