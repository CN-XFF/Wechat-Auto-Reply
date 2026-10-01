$ErrorActionPreference = 'Stop'

$distributionDir = $PSScriptRoot
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $distributionDir '..')).Path
$versionDefinitionPath = Join-Path $distributionDir 'Version.iss'
if (-not (Test-Path -LiteralPath $versionDefinitionPath)) { throw "Version definition not found: $versionDefinitionPath" }
$versionDefinition = Get-Content -LiteralPath $versionDefinitionPath -Raw
$versionMatch = [regex]::Match($versionDefinition, '(?m)^\s*#define\s+AppVersion\s+"([^"]+)"\s*$')
if (-not $versionMatch.Success) { throw "Could not read AppVersion from $versionDefinitionPath" }
$version = $versionMatch.Groups[1].Value
$buildEnv = Join-Path $distributionDir 'build-env'
$python = Join-Path $buildEnv 'Scripts\python.exe'
$pyinstaller = Join-Path $buildEnv 'Scripts\pyinstaller.exe'
$vendorRoot = Join-Path $projectRoot 'vendor\wechatauto-replica'
$outputRoot = Join-Path $distributionDir "output\$version"
$appOutput = Join-Path $outputRoot 'WeChatAutoReply'
$workRoot = Join-Path $distributionDir "build\$version"
$specRoot = Join-Path $distributionDir "spec\$version"
$licenseRoot = Join-Path $appOutput 'licenses'
$innoCompiler = Join-Path $distributionDir 'tools\InnoSetup7\ISCC.exe'
$installerOutput = Join-Path $distributionDir "release\$version"
$uiautomationBin = Join-Path $buildEnv 'Lib\site-packages\uiautomation\bin'

if (-not (Test-Path -LiteralPath $python)) { throw "Build Python not found: $python" }
if (-not (Test-Path -LiteralPath $pyinstaller)) { throw "PyInstaller not found: $pyinstaller" }
if (-not (Test-Path -LiteralPath $vendorRoot)) { throw "Vendored dependency not found: $vendorRoot" }
if (-not (Test-Path -LiteralPath $innoCompiler)) { throw "Inno Setup compiler not found: $innoCompiler" }
foreach ($dllName in @('UIAutomationClient_VC140_X64.dll', 'UIAutomationClient_VC140_X86.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $uiautomationBin $dllName))) {
        throw "Required UIAutomation DLL not found: $(Join-Path $uiautomationBin $dllName)"
    }
}
if (Test-Path -LiteralPath $outputRoot) { throw "Refusing to overwrite existing build output: $outputRoot" }
if (Test-Path -LiteralPath $workRoot) { throw "Refusing to overwrite existing build work directory: $workRoot" }
if (Test-Path -LiteralPath $specRoot) { throw "Refusing to overwrite existing spec directory: $specRoot" }
if (Test-Path -LiteralPath $installerOutput) { throw "Refusing to overwrite existing installer output: $installerOutput" }

$env:PIP_CACHE_DIR = Join-Path $distributionDir 'pip-cache'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $distributionDir 'pyi-cache'
$env:TEMP = Join-Path $distributionDir 'temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
New-Item -ItemType Directory -Path $specRoot | Out-Null

& $python -m pip install --no-deps --force-reinstall $vendorRoot
if ($LASTEXITCODE -ne 0) { throw 'The vendored WeChat package could not be installed into the isolated build environment.' }

$vendorCheck = & $python -c "import pathlib, wechatauto; print(pathlib.Path(wechatauto.__file__).resolve())"
if ($LASTEXITCODE -ne 0) { throw 'The vendored WeChat package could not be imported in the isolated build environment.' }
if (-not ([string]$vendorCheck).StartsWith((Join-Path $buildEnv 'Lib\site-packages'), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unexpected WeChat package origin; refusing to build: $vendorCheck"
}

$pyinstallerArgs = @(
    '--noconfirm',
    '--clean',
    '--onedir',
    '--windowed',
    '--name', 'WeChatAutoReply',
    '--paths', $vendorRoot,
    '--add-data', "$(Join-Path $projectRoot 'config.example.json');.",
    '--add-data', "$(Join-Path $projectRoot 'schemas');schemas",
    '--add-binary', "$(Join-Path $uiautomationBin 'UIAutomationClient_VC140_X64.dll');uiautomation\bin",
    '--add-binary', "$(Join-Path $uiautomationBin 'UIAutomationClient_VC140_X86.dll');uiautomation\bin",
    '--collect-all', 'wechatauto',
    '--distpath', $outputRoot,
    '--workpath', $workRoot,
    '--specpath', $specRoot,
    (Join-Path $projectRoot 'app.py')
)
& $pyinstaller @pyinstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
if (-not (Test-Path -LiteralPath (Join-Path $appOutput 'WeChatAutoReply.exe'))) { throw 'Built executable was not found.' }
if (Test-Path -LiteralPath (Join-Path $appOutput '_internal\config.json')) { throw 'A live config.json unexpectedly entered the build output.' }
$appInternal = Join-Path $appOutput '_internal'
foreach ($metadataDirectory in Get-ChildItem -LiteralPath $appInternal -Directory -Filter '*.dist-info' -ErrorAction SilentlyContinue) {
    $directUrlPath = Join-Path $metadataDirectory.FullName 'direct_url.json'
    if (-not (Test-Path -LiteralPath $directUrlPath)) { continue }
    $directUrlMetadata = Get-Content -LiteralPath $directUrlPath -Raw | ConvertFrom-Json
    if ([string]$directUrlMetadata.url -match '(?i)^file:') {
        [System.IO.File]::WriteAllText($directUrlPath, '{}', [System.Text.UTF8Encoding]::new($false))
    }
    $remainingMetadata = Get-Content -LiteralPath $directUrlPath -Raw
    if ($remainingMetadata -match '(?i)"url"\s*:\s*"(?:file:|[A-Z]:\\|/Users/)') {
        throw "A local build path remains in packaged metadata: $directUrlPath"
    }
}
foreach ($dllName in @('UIAutomationClient_VC140_X64.dll', 'UIAutomationClient_VC140_X86.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $appOutput "_internal\uiautomation\bin\$dllName"))) {
        throw "Built app is missing UIAutomation runtime dependency: $dllName"
    }
}

$docsDir = Join-Path $appOutput 'docs'
$vendorLicenseDir = Join-Path $licenseRoot 'wechatauto-replica'
New-Item -ItemType Directory -Path $docsDir,$vendorLicenseDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $distributionDir 'INSTALLATION_GUIDE.md') -Destination $docsDir
Copy-Item -LiteralPath (Join-Path $distributionDir 'README.md') -Destination $docsDir
Copy-Item -LiteralPath (Join-Path $projectRoot 'CODEX_AFTER_INSTALL_HANDOFF.md') -Destination $docsDir
Copy-Item -LiteralPath (Join-Path $projectRoot 'CHANGELOG.md') -Destination $docsDir
Copy-Item -LiteralPath (Join-Path $projectRoot 'CONFIG_TUNING.md') -Destination $docsDir
Copy-Item -LiteralPath (Join-Path $distributionDir "RELEASE_NOTES_$version.md") -Destination $docsDir
Copy-Item -LiteralPath (Join-Path $vendorRoot 'LICENSE') -Destination (Join-Path $vendorLicenseDir 'LICENSE')

$sitePackages = Join-Path $buildEnv 'Lib\site-packages'
$licenseReport = Join-Path $licenseRoot 'THIRD_PARTY_COMPONENTS.txt'
$reportLines = [System.Collections.Generic.List[string]]::new()
$reportLines.Add('Third-party Python distributions included in this release candidate')
$reportLines.Add('License metadata is copied from each installed distribution where provided.')
$reportLines.Add('')
foreach ($distInfo in Get-ChildItem -LiteralPath $sitePackages -Directory -Filter '*.dist-info' | Sort-Object Name) {
    $metadataPath = Join-Path $distInfo.FullName 'METADATA'
    if (-not (Test-Path -LiteralPath $metadataPath)) { continue }
    $metadata = Get-Content -LiteralPath $metadataPath -Raw
    $name = [regex]::Match($metadata, '(?m)^Name: (.+)$').Groups[1].Value.Trim()
    $packageVersion = [regex]::Match($metadata, '(?m)^Version: (.+)$').Groups[1].Value.Trim()
    $license = [regex]::Match($metadata, '(?m)^License-Expression: (.+)$').Groups[1].Value.Trim()
    if (-not $license) { $license = [regex]::Match($metadata, '(?m)^License: (.+)$').Groups[1].Value.Trim() }
    if (-not $name) { $name = $distInfo.BaseName -replace '\.dist-info$','' }
    if (-not $packageVersion) { $packageVersion = 'version metadata unavailable' }
    if (-not $license) { $license = 'see copied distribution license files / package metadata' }
    $reportLines.Add("$name $packageVersion - $license")

    $packageLicenseDir = Join-Path $distInfo.FullName 'licenses'
    $destinationLicenseDir = Join-Path $licenseRoot ('python\' + ($distInfo.BaseName -replace '\.dist-info$',''))
    if (Test-Path -LiteralPath $packageLicenseDir) {
        New-Item -ItemType Directory -Path $destinationLicenseDir -Force | Out-Null
        Copy-Item -Path (Join-Path $packageLicenseDir '*') -Destination $destinationLicenseDir -Recurse -Force
    } else {
        $licenseFiles = Get-ChildItem -LiteralPath $distInfo.FullName -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^(LICENSE|COPYING|NOTICE|COPYRIGHT)' }
        if ($licenseFiles) {
            New-Item -ItemType Directory -Path $destinationLicenseDir -Force | Out-Null
            $licenseFiles | Copy-Item -Destination $destinationLicenseDir -Force
        }
    }
}
Set-Content -LiteralPath $licenseReport -Value $reportLines -Encoding UTF8

New-Item -ItemType Directory -Path $installerOutput -Force | Out-Null
& $innoCompiler (Join-Path $distributionDir 'WeChatAutoReply.iss')
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed with exit code $LASTEXITCODE" }

$installer = Join-Path $installerOutput "WeChatAutoReply-Setup-$version.exe"
if (-not (Test-Path -LiteralPath $installer)) { throw "Installer was not produced: $installer" }
$hash = Get-FileHash -LiteralPath $installer -Algorithm SHA256
"Release candidate created: $installer"
"SizeBytes=$((Get-Item -LiteralPath $installer).Length)"
"SHA256=$($hash.Hash)"
