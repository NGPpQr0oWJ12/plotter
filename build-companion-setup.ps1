[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][uri]$CentralUrl,
    [Parameter(Mandatory = $true)][string]$CertificatePath,
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
if ($CentralUrl.Scheme -ne 'https' -or !$CentralUrl.Host -or $CentralUrl.UserInfo -or
    $CentralUrl.AbsolutePath -ne '/' -or $CentralUrl.Query -or $CentralUrl.Fragment -or
    $CentralUrl.HostNameType -eq 'IPv6') {
    throw 'Supply a plain HTTPS origin with an optional port.'
}
$origin = $CentralUrl.GetLeftPart([UriPartial]::Authority)
if (!$OutputDirectory) { $OutputDirectory = Join-Path $PSScriptRoot 'dist' }
$output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
$certificate = (Resolve-Path -LiteralPath $CertificatePath).ProviderPath
$source = Join-Path $PSScriptRoot 'web-plotter'
$version = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'VERSION') -Raw).Trim()
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$python = Get-Command 'python.exe' -ErrorAction Stop
$pythonVersion = '3.13.17'
$pythonHash = '9E35319BFFEDAB8B36AD44D8F377CB4F1733BCEC257A0B6D04D94F01486DDB38'
$setupName = "Plotter-Companion-Setup-$version-x64.exe"
$destination = Join-Path $output $setupName
$updateName = "plotter-companion-v$version-windows-x64.zip"
$updateDestination = Join-Path $output $updateName
foreach ($artifact in @($destination, $updateDestination)) {
    if (Test-Path -LiteralPath $artifact) { throw "Release artifact already exists: $artifact" }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem

$build = Join-Path ([IO.Path]::GetTempPath()) ('plotter-setup-build-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $build | Out-Null
try {
    $payload = Join-Path $build 'payload'
    $runtime = Join-Path $payload 'runtime'
    $libraries = Join-Path $runtime 'Lib\site-packages'
    $archive = Join-Path $build 'python.zip'
    $url = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive -TimeoutSec 45
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $pythonHash) {
        throw 'Python download checksum mismatch.'
    }
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $runtime)
    Add-Content -LiteralPath (Join-Path $runtime 'python313._pth') -Value 'Lib\site-packages' -Encoding Ascii
    Add-Content -LiteralPath (Join-Path $runtime 'python313._pth') -Value '..' -Encoding Ascii
    $pipArguments = @('-m', 'pip', 'install', '--disable-pip-version-check', '--only-binary=:all:',
        '--platform', 'win_amd64', '--python-version', '3.13', '--implementation', 'cp', '--abi', 'cp313',
        '--target', $libraries, '-r', (Join-Path $source 'requirements.txt'))
    & $python.Source @pipArguments
    if ($LASTEXITCODE -ne 0) { throw 'Could not build bundled Python dependencies.' }
    Get-ChildItem -LiteralPath $source -File -Filter '*.py' | Where-Object Name -NotLike 'test_*' |
        Copy-Item -Destination $payload
    Copy-Item -LiteralPath (Join-Path $source 'static') -Destination $payload -Recurse
    foreach ($script in @('install-companion.ps1', 'companion-profile.ps1', 'apply-companion-update.ps1')) {
        $installerScript = Get-Content -LiteralPath (Join-Path $source $script) -Raw -Encoding UTF8
        [IO.File]::WriteAllText((Join-Path $payload $script), $installerScript, [Text.UTF8Encoding]::new($true))
    }
    [IO.File]::WriteAllText((Join-Path $payload 'VERSION'), $version, [Text.Encoding]::ASCII)
    $serverOptions = & (Join-Path $runtime 'python.exe') -B (Join-Path $payload 'server.py') --help | Out-String
    if ($LASTEXITCODE -ne 0 -or $serverOptions -notmatch '--central-ca') {
        throw 'Bundled runtime did not load companion support for an application-local CA.'
    }
    $updateArchive = Join-Path $build $updateName
    [IO.Compression.ZipFile]::CreateFromDirectory($payload, $updateArchive, [IO.Compression.CompressionLevel]::Optimal, $false)

    $caFile = Join-Path $payload 'central-ca.pem'
    $prepareCa = @'
from pathlib import Path
import ssl
import sys

source, target = map(Path, sys.argv[1:])
data = source.read_bytes()
if b'-----BEGIN CERTIFICATE-----' not in data:
    data = ssl.DER_cert_to_PEM_cert(data).encode('ascii')
target.write_bytes(data)
ssl.create_default_context(cafile=str(target))
'@
    $prepareCa | & (Join-Path $runtime 'python.exe') - $certificate $caFile
    if ($LASTEXITCODE -ne 0) { throw 'The central CA must be a valid PEM or DER certificate.' }
    [IO.File]::WriteAllText((Join-Path $payload 'origin.txt'), $origin)

    $bundle = Join-Path $build 'payload.zip'
    [IO.Compression.ZipFile]::CreateFromDirectory($payload, $bundle, [IO.Compression.CompressionLevel]::Optimal, $false)
    $exe = Join-Path $build $setupName
    $compilerArguments = @('/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001', "/out:$exe",
        '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', '/reference:System.IO.Compression.dll',
        '/reference:System.IO.Compression.FileSystem.dll', "/resource:$bundle,plotter.payload",
        (Join-Path $source 'companion-setup.cs'))
    & $compiler @compilerArguments
    if ($LASTEXITCODE -ne 0) { throw 'Could not compile the Windows installer.' }

    New-Item -ItemType Directory -Path $output -Force | Out-Null
    Copy-Item -LiteralPath $exe -Destination $destination
    Copy-Item -LiteralPath $updateArchive -Destination $updateDestination
    Get-FileHash -LiteralPath $destination, $updateDestination -Algorithm SHA256
} finally {
    $expected = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $resolved = (Resolve-Path -LiteralPath $build).ProviderPath
    if ($resolved.StartsWith($expected, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $resolved -Leaf) -match '^plotter-setup-build-[a-f0-9]{32}$') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
