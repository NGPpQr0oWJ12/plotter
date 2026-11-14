[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$CandidatePath,
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')]
    [string]$ExpectedVersion,
    [switch]$Schedule
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = "Xiao Plotter Companion Update ($($identity.User.Value))"
$taskPath = '\'
$powershell = Join-Path $PSHOME 'powershell.exe'

function Set-UpdateResult([string]$State, [string]$Message) {
    $result = @{ state = $State; version = $ExpectedVersion }
    if ($Message) { $result.message = $Message }
    $resultPath = Join-Path $InstallRoot 'update-result.json'
    $temporary = Join-Path $InstallRoot ("update-result-" + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, ($result | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
            [IO.File]::Replace($temporary, $resultPath, [NullString]::Value)
        } else {
            [IO.File]::Move($temporary, $resultPath)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -ErrorAction SilentlyContinue
        }
    }
}

function Get-CandidateInstaller {
    $path = (Resolve-Path -LiteralPath $CandidatePath).ProviderPath
    $updates = (Join-Path $InstallRoot 'updates').TrimEnd('\') + '\'
    if (!$path.StartsWith($updates, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The update is outside the companion update directory.'
    }
    $candidateVersion = (Get-Content -LiteralPath (Join-Path $path 'VERSION') -Raw -Encoding ASCII).Trim()
    if ($candidateVersion -cne $ExpectedVersion) { throw 'The update version does not match the download.' }
    $installer = Join-Path $path 'install-companion.ps1'
    if (!(Test-Path -LiteralPath $installer -PathType Leaf)) { throw 'The downloaded installer is missing.' }
    return $installer
}

function Assert-CurrentInstallation {
    $saved = Get-Content -LiteralPath (Join-Path $InstallRoot 'install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($saved.release -notmatch '^app-[a-f0-9]{32}$' -or
        (Split-Path -Leaf $PSScriptRoot) -cne $saved.release -or
        (Split-Path -Parent $PSScriptRoot) -ine $InstallRoot) {
        throw 'The installed companion changed before applying the update.'
    }
    $currentVersion = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'VERSION') -Raw -Encoding ASCII).Trim()
    if ($currentVersion -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
        [version]$ExpectedVersion -le [version]$currentVersion) {
        throw 'The downloaded version is not newer than the installed companion.'
    }
}

function Remove-CandidateStage {
    # Delete only our unique stage, never the updates directory or a redirected path.
    $updates = Join-Path $InstallRoot 'updates'
    $path = (Resolve-Path -LiteralPath $CandidatePath).ProviderPath
    $stage = if ((Split-Path -Leaf $path) -ceq 'payload') { Split-Path -Parent $path } else { $path }
    if ((Split-Path -Parent $stage) -ine $updates -or (Split-Path -Leaf $stage) -notmatch '^update-[a-z0-9_-]{8}$') {
        return
    }
    foreach ($directory in @($InstallRoot, $updates, $stage, $path)) {
        if ((Get-Item -LiteralPath $directory).Attributes -band [IO.FileAttributes]::ReparsePoint) { return }
    }
    Remove-Item -LiteralPath $stage -Recurse -Force
}

$expectedRoot = (Resolve-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'Plotter\Companion')).ProviderPath
$InstallRoot = (Resolve-Path -LiteralPath $InstallRoot).ProviderPath
if ($InstallRoot -ine $expectedRoot) { throw 'The companion update requires the current user installation.' }

if ($Schedule) {
    $null = Get-CandidateInstaller
    Assert-CurrentInstallation
    $existing = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    if ($existing.State -in ('Queued', 'Running')) { throw 'A companion update is already in progress.' }
    $arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -CandidatePath "{1}" -InstallRoot "{2}" -ExpectedVersion "{3}"' -f
        $PSCommandPath, (Resolve-Path -LiteralPath $CandidatePath).ProviderPath, $InstallRoot, $ExpectedVersion
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $InstallRoot
    $principal = New-ScheduledTaskPrincipal -UserId $identity.User.Value -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Principal $principal -Settings $settings -Force | Out-Null
    try {
        Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath
    } catch {
        Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction SilentlyContinue
        throw
    }
    return
}

$mutex = [Threading.Mutex]::new($false, 'Local\XiaoPlotterCompanionSetup')
$ownsMutex = $false
try {
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (!$ownsMutex) { throw 'Another companion installation is already in progress.' }

    $installer = Get-CandidateInstaller
    Assert-CurrentInstallation

    & $installer -ExpectedVersion $ExpectedVersion | Out-Null
    Set-UpdateResult 'done' ''
} catch {
    Set-UpdateResult 'failed' $_.Exception.Message
    exit 1
} finally {
    try { Remove-CandidateStage } catch { Write-Warning $_.Exception.Message }
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
    Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction SilentlyContinue
}
