[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')]
    [string]$Version,

    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
if (!$OutputDirectory) { $OutputDirectory = Join-Path $PSScriptRoot 'dist' }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$archiveName = "plotter-v$Version-windows-source.zip"
$destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $destination | Out-Null
$archivePath = Join-Path $destination $archiveName
if (Test-Path -LiteralPath $archivePath) {
    throw "Release archive already exists: $archivePath"
}

$files = @(
    'RELEASE.md',
    'CHANGELOG.md',
    'COMPANION_REVISION',
    'package-release.ps1',
    'Запустить плоттер.cmd',
    'web-plotter/.gitignore',
    'web-plotter/requirements.txt',
    'web-plotter/start.ps1',
    'web-plotter/install-companion.ps1',
    'web-plotter/companion-profile.ps1',
    'web-plotter/apply-companion-update.ps1',
    'deploy/README.md',
    'deploy/check-state.py',
    'deploy/deploy.conf.example',
    'deploy/plotter.nginx.conf.example',
    'deploy/plotter.http.nginx.conf.example',
    'deploy/plotter@.service',
    'deploy/release.sh',
    'research/machine-profile.example.json',
    'web-plotter/auto_run.py',
    'web-plotter/app_update.py',
    'web-plotter/cad.py',
    'web-plotter/contour_packing.py',
    'web-plotter/controller.py',
    'web-plotter/cut_journal.py',
    'web-plotter/cut_metrics.py',
    'web-plotter/cut_plan.py',
    'web-plotter/demo_controller.py',
    'web-plotter/designer_cad.py',
    'web-plotter/machine.py',
    'web-plotter/nesting.py',
    'web-plotter/order_analysis.py',
    'web-plotter/order_metrics.py',
    'web-plotter/panel_diagnostics.py',
    'web-plotter/panel_readback.py',
    'web-plotter/production_store.py',
    'web-plotter/registry_archive.py',
    'web-plotter/rect_packing.py',
    'web-plotter/route_optimizer.py',
    'web-plotter/run_monitor.py',
    'web-plotter/server.py',
    'web-plotter/sewing_document.py',
    'web-plotter/sewing_pdf.py',
    'web-plotter/sketchup_design.py',
    'web-plotter/model_surface.py',
    'web-plotter/skp_reader.py',
    'web-plotter/upholstery.py',
    'web-plotter/upholstery_fit.py',
    'web-plotter/fit_jobs.py',
    'web-plotter/workspace.py',
    'web-plotter/xiao_desktop.py',
    'web-plotter/xiao_job.py',
    'web-plotter/test_auto_run.py',
    'web-plotter/test_app_update.py',
    'web-plotter/test_cad.py',
    'web-plotter/test_controller.py',
    'web-plotter/test_cut_plan.py',
    'web-plotter/test_designer_cad.py',
    'web-plotter/test_designer_import_recovery.py',
    'web-plotter/test_designer_quick_trim.py',
    'web-plotter/test_designer_3d_drafting.mjs',
    'web-plotter/test_designer_3d_folds.mjs',
    'web-plotter/test_designer_3d_notches.mjs',
    'web-plotter/test_designer_3d_objects.mjs',
    'web-plotter/test_designer_3d_selection.mjs',
    'web-plotter/test_fabric_nap.mjs',
    'web-plotter/test_nesting.py',
    'web-plotter/test_order_analysis.py',
    'web-plotter/test_order_metrics.py',
    'web-plotter/test_panel_diagnostics.py',
    'web-plotter/test_production_store.py',
    'web-plotter/test_registry_archive.py',
    'web-plotter/test_sewing_document.py',
    'web-plotter/test_server_flow.py',
    'web-plotter/test_sketchup_design.py',
    'web-plotter/test_model_surface.py',
    'web-plotter/test_upholstery.py',
    'web-plotter/test_upholstery_fit.py',
    'web-plotter/test_upholstery_orientation.py',
    'web-plotter/test_workspace.py',
    'web-plotter/test_xiao_desktop.py',
    'web-plotter/test_xiao_job.py',
    'web-plotter/static/app.js',
    'web-plotter/static/confirm-action.js',
    'web-plotter/static/designer-cad.css',
    'web-plotter/static/OpenGostTypeB-Regular.ttf',
    'web-plotter/static/OpenGost-OFL.txt',
    'web-plotter/static/designer-cad.js',
    'web-plotter/static/designer-3d.css',
    'web-plotter/static/designer-3d.js',
    'web-plotter/static/designer-3d-magnet.js',
    'web-plotter/static/designer-3d-drafting.js',
    'web-plotter/static/designer-3d-fabric.js',
    'web-plotter/static/designer-3d-folds.js',
    'web-plotter/static/designer-3d-outline.js',
    'web-plotter/static/designer-3d-overlay.js',
    'web-plotter/static/designer-3d-selection.js',
    'web-plotter/static/designer-3d-objects.js',
    'web-plotter/static/designer-3d-patterns.js',
    'web-plotter/static/designer-3d-fit.js',
    'web-plotter/static/designer-shortcuts.js',
    'web-plotter/static/plotter-monochrome.css',
    'web-plotter/static/index.html',
    'web-plotter/static/simulation.js',
    'web-plotter/static/style.css',
    'web-plotter/static/three-3d.js',
    'web-plotter/static/three-license.txt',
    'web-plotter/static/workplaces.css',
    'web-plotter/static/workplaces.js'
)

foreach ($relativePath in $files) {
    if (!(Test-Path -LiteralPath (Join-Path $PSScriptRoot $relativePath) -PathType Leaf)) {
        throw "Release file is missing: $relativePath"
    }
}

# Ship a standalone example profile instead of copying an installed machine's settings.
$profile = [ordered]@{
    software = [ordered]@{ path = 'C:\Xiao\xiao.exe' }
    observedComputerConfiguration = [ordered]@{
        machineIp = '127.0.0.1'
        workrectMm = [ordered]@{ x = 2500; y = 1650 }
    }
    observedControllerConfiguration = [ordered]@{ zTravelMm = 0 }
} | ConvertTo-Json -Depth 5

$localReadme = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'web-plotter/README.md') -Encoding UTF8
$releaseReadme = ($localReadme | Where-Object {
    $_ -notmatch '\(\.\./research/' -and $_ -notmatch '^\s*На станке \d{2}\.\d{2}\.\d{4}'
}) -join "`n"
$releaseReadme = $releaseReadme -replace 'На этом ПК зависимости подготовлены в папке проекта\. На другом Windows x64', 'На Windows x64'
$releaseReadme = $releaseReadme -replace 'Поле \*\*2500 × 1650 мм\*\* считано из настроек подключённой платы\.', 'Размер поля берётся из профиля станка.'
$releaseReadme = $releaseReadme -replace 'Панель опрашивает физический контроллер `(?:\d{1,3}\.){3}\d{1,3}:1234`\.', 'Панель опрашивает контроллер по IPv4-адресу из профиля через порт UDP 1234.'

$stagedArchive = Join-Path $destination ([guid]::NewGuid().ToString('N') + '.tmp')
$entryRoot = "plotter-v$Version"
$utf8 = New-Object System.Text.UTF8Encoding($false)
try {
    $archive = [IO.Compression.ZipFile]::Open($stagedArchive, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($relativePath in $files) {
            if ($relativePath.EndsWith('.sh')) {
                $entry = $archive.CreateEntry("$entryRoot/$relativePath")
                $writer = New-Object IO.StreamWriter($entry.Open(), $utf8)
                try {
                    $source = [IO.File]::ReadAllText((Join-Path $PSScriptRoot $relativePath))
                    $writer.Write($source.Replace([string]([char]13) + [char]10, [string][char]10))
                } finally {
                    $writer.Dispose()
                }
                continue
            }
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive, (Join-Path $PSScriptRoot $relativePath), "$entryRoot/$relativePath",
                [IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
        $generatedFiles = @{
            'README.md' = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'RELEASE.md') -Encoding UTF8 -Raw).TrimEnd()
            'VERSION' = $Version
            'research/machine-profile.json' = $profile
            'web-plotter/README.md' = $releaseReadme.TrimEnd()
        }
        foreach ($relativePath in $generatedFiles.Keys) {
            $entry = $archive.CreateEntry("$entryRoot/$relativePath")
            $writer = New-Object IO.StreamWriter($entry.Open(), $utf8)
            try {
                $writer.WriteLine($generatedFiles[$relativePath])
            } finally {
                $writer.Dispose()
            }
        }
    } finally {
        $archive.Dispose()
    }
    [IO.File]::Move($stagedArchive, $archivePath)
} finally {
    if (Test-Path -LiteralPath $stagedArchive) {
        Remove-Item -LiteralPath $stagedArchive -Force
    }
}

Get-FileHash -LiteralPath $archivePath -Algorithm SHA256
