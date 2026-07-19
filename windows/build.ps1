[CmdletBinding()]
param(
    [ValidateSet("win-x64", "win-arm64")]
    [string]$RuntimeIdentifier = "win-x64",
    [string]$OutputDirectory = "$PSScriptRoot\..\dist\windows",
    [string]$PythonRuntime
)

$ErrorActionPreference = "Stop"
$project = Join-Path $PSScriptRoot "Huginn.Tray\Huginn.Tray.csproj"
$publish = Join-Path $OutputDirectory "Huginn"

# Never let removed files from an earlier build leak into a release archive.
if (Test-Path $publish) { Remove-Item $publish -Recurse -Force }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

dotnet publish $project `
    --configuration Release `
    --runtime $RuntimeIdentifier `
    --self-contained true `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    --output $publish

if ($PythonRuntime) {
    if (-not (Test-Path $PythonRuntime)) { throw "Python runtime not found: $PythonRuntime" }
    $runtimeDestination = Join-Path $publish "runtime"
    New-Item -ItemType Directory -Path $runtimeDestination -Force | Out-Null
    Copy-Item (Join-Path $PythonRuntime "*") $runtimeDestination -Recurse -Force
} else {
    Write-Warning "No Python runtime was bundled; this package requires huginn.exe on PATH."
}

$archive = Join-Path $OutputDirectory "Huginn-$RuntimeIdentifier.zip"
if (Test-Path $archive) { Remove-Item $archive }
Compress-Archive -Path (Join-Path $publish "*") -DestinationPath $archive
Write-Host "Built $archive"
