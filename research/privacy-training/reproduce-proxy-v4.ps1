[CmdletBinding()]
param(
    [string]$Distro = 'Debian',
    [string]$Runtime = '/home/goloshchanov/kaponir-privacy-v3-842d988',
    [string]$Baseline,
    [string]$Assets,
    [string]$Adapter,
    [string]$Output,
    [switch]$SkipRecordInference
)

$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if (!$Baseline) { $Baseline = "$Runtime/build-source/target/release/kaponir" }
if (!$Assets) {
    $Assets = Join-Path $repo 'artifacts/privacy-training/pilot-v3-842d988/assets'
    if (!(Test-Path -LiteralPath $Assets)) {
        $Assets = Join-Path (Split-Path $repo) 'pilot-v3-842d988/assets'
    }
}
if (!$Adapter) { $Adapter = Join-Path $PSScriptRoot 'results/v4/full/epoch-2' }
if (!$Output) {
    $name = 'proxy-v4-repro-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff')
    $Output = Join-Path $repo "artifacts/privacy-training/$name"
}
New-Item -ItemType Directory -Path $Output -Force | Out-Null

function Linux-Path([string]$Path) {
    if ($Path.StartsWith('/')) { return $Path }
    $fullPath = [System.IO.Path]::GetFullPath($Path).Replace('\', '/')
    $converted = & wsl.exe -d $Distro -- wslpath -a -u $fullPath 2>&1 |
        ForEach-Object { "$_" } | Where-Object { $_ -notmatch '\.wslconfig:' }
    if ($LASTEXITCODE -ne 0) { throw "Cannot convert $Path to a WSL path" }
    return ($converted | Select-Object -Last 1).Trim()
}

$linuxRepo = Linux-Path $repo
$linuxOut = Linux-Path (Resolve-Path $Output).Path
$assetsPath = Linux-Path $Assets
$adapterPath = Linux-Path $Adapter
$baselinePath = Linux-Path $Baseline
$target = "$Runtime/../kaponir-privacy-v4-proxy-target"
$scripts = 'research/privacy-training'

function Invoke-Wsl([string[]]$Arguments) {
    & wsl.exe -d $Distro --cd $linuxRepo -- @Arguments 2>&1 |
        ForEach-Object { "$_" } | Where-Object { $_ -notmatch '\.wslconfig:' }
    if ($LASTEXITCODE -ne 0) { throw "WSL exit code $LASTEXITCODE for $($Arguments -join ' ')" }
}

Invoke-Wsl @('test', '-x', $baselinePath)
$cargo = @('env', "CARGO_HOME=$Runtime/cargo", "RUSTUP_HOME=$Runtime/rustup",
           "CARGO_TARGET_DIR=$target", "$Runtime/cargo/bin/cargo")
Invoke-Wsl ($cargo + @('build', '--locked', '--offline', '--release', '-p', 'kaponir-cli'))
Invoke-Wsl @('python3', "$scripts/proxy_research_v4.py", 'control',
             '--binary', $baselinePath, '--output', "$linuxOut/research")
Invoke-Wsl @('python3', "$scripts/proxy_matrix_v4.py",
             '--baseline', $baselinePath, '--candidate', "$target/release/kaponir",
             '--output', $linuxOut)
if (!$SkipRecordInference) {
    Invoke-Wsl @('python3', "$scripts/proxy_research_v4.py", 'windows',
                 '--adapter', $adapterPath, '--output', "$linuxOut/research")
    Invoke-Wsl @('env', "HF_HOME=$Runtime/hf-cache", 'HF_HUB_OFFLINE=1', 'TRANSFORMERS_OFFLINE=1',
                 "$Runtime/venv/bin/python", "$scripts/laptop.py", 'predict',
                 '--windows', "$linuxOut/research/record-windows.jsonl",
                 '--adapter', $adapterPath, '--assets', $assetsPath,
                 '--output', "$linuxOut/research/record-answers.jsonl")
    Invoke-Wsl @('python3', "$scripts/proxy_research_v4.py", 'record-results',
                 '--answers', "$linuxOut/research/record-answers.jsonl", '--output', "$linuxOut/research")
}
Invoke-Wsl ($cargo + @('test', '--locked', '--offline', '--release', '--workspace')) |
    Tee-Object -FilePath (Join-Path $Output 'rust-tests.log')
Write-Output (Join-Path $Output 'native-matrix.json')
