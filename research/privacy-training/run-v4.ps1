[CmdletBinding()]
param(
    [string]$RunDirectory = 'artifacts/privacy-training/pilot-v4-repro',
    [string]$OldRunDirectory = 'artifacts/privacy-training/pilot-v3-842d988',
    [string]$Distribution = 'Debian',
    [string]$Python,
    [string]$Kaponir,
    [ValidateSet('all', 'prepare', 'train', 'assess')][string]$Phase = 'all',
    [switch]$WaitForTraining
)

$ErrorActionPreference = 'Stop'
$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$protocol = '842d9887f8023403eadaab713248aea34ceeceeb'

function Get-RepositoryPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $repository $Path))
}

function ConvertTo-WslPath([string]$Path) {
    $lines = @(& wsl.exe -d $Distribution -- wslpath -u ($Path.Replace('\', '/')) 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "WSL could not resolve $Path" }
    $translated = @($lines | ForEach-Object { "$_".Replace([string][char]0, '') } | Where-Object { $_.StartsWith('/') })
    if ($translated.Count -ne 1) { throw "Unexpected WSL path for $Path" }
    return $translated[0].Trim()
}

$env:WSL_UTF8 = '1'
$profileLines = @(& wsl.exe -d $Distribution -- printenv HOME 2>&1)
if ($LASTEXITCODE -ne 0) { throw "WSL distribution '$Distribution' is unavailable." }
$wslProfile = @($profileLines | ForEach-Object { "$_".Replace([string][char]0, '') } | Where-Object { $_.StartsWith('/') })[-1].Trim()
$pilotEnvironment = "$wslProfile/kaponir-privacy-v3-842d988"
if (-not $Python) { $Python = "$pilotEnvironment/venv/bin/python" }
if (-not $Kaponir) { $Kaponir = "$pilotEnvironment/build-source/target/release/kaponir" }

$run = Get-RepositoryPath $RunDirectory
$old = Get-RepositoryPath $OldRunDirectory
$snapshot = Join-Path $run 'source'
$pilot = Join-Path $snapshot 'research/privacy-training'
$policy = Join-Path $snapshot 'field-experiment'
$sourceFiles = @(
    'dataset.py', 'synthetic_v4.py', 'prepare.py', 'laptop.py', 'evaluate.py', 'grounding.py',
    'replay.py', 'context_policy.py', 'context_controls.py', 'test_context_policy.py',
    'test_v4.py', 'v4_audit.py', 'v4_experiment.py', 'v4_correction.py', 'v4_report.py'
)
$fieldFiles = @('rebind.py', 'field_policy.py', 'replay_fields.py')

Push-Location $repository
try {
    [void][IO.Directory]::CreateDirectory($run)
    if (-not (Test-Path -LiteralPath (Join-Path $pilot 'data/v3/manifest.json'))) {
        [void][IO.Directory]::CreateDirectory($snapshot)
        $tar = Join-Path $run 'protocol.tar'
        & git -c core.autocrlf=false archive --format=tar "--output=$tar" $protocol research/privacy-training scripts/kaponir_inference.py
        if ($LASTEXITCODE -ne 0) { throw 'Could not archive the protocol revision.' }
        & tar.exe -xf $tar -C $snapshot
        if ($LASTEXITCODE -ne 0) { throw 'Could not extract the protocol revision.' }
    }
    $trainingRecord = Join-Path $run 'full/run.json'
    $trainingStatus = Join-Path $run 'training-stage.txt'
    if ($WaitForTraining) {
        while (Test-Path -LiteralPath $trainingStatus) {
            $state = (Get-Content -LiteralPath $trainingStatus -Raw).Trim()
            if ($state -eq 'complete') { break }
            if ($state.StartsWith('failed')) { throw "V4 training status is $state." }
            $pidPath = Join-Path $run 'training-windows-pid.txt'
            if ((Test-Path -LiteralPath $pidPath) -and -not (Get-Process -Id (Get-Content -LiteralPath $pidPath) -ErrorAction SilentlyContinue)) {
                throw 'The trainer exited before recording completion.'
            }
            Start-Sleep -Seconds 12
        }
    }
    if (-not (Test-Path -LiteralPath $trainingRecord)) {
        $corpus = Join-Path $pilot 'data/v4'
        [void][IO.Directory]::CreateDirectory($corpus)
        foreach ($file in @('manifest.json', 'train.jsonl', 'validation.jsonl', 'test.jsonl')) {
            Copy-Item -LiteralPath "research/privacy-training/data/v4/$file" -Destination $corpus -Force
        }
        Copy-Item -LiteralPath 'research/privacy-training/data/context-controls.jsonl' -Destination (Join-Path $pilot 'data') -Force
        foreach ($file in $sourceFiles) { Copy-Item -LiteralPath "research/privacy-training/$file" -Destination $pilot -Force }
    } else {
        foreach ($file in @('v4_audit.py', 'v4_experiment.py', 'v4_correction.py', 'v4_report.py', 'test_v4.py')) {
            Copy-Item -LiteralPath "research/privacy-training/$file" -Destination $pilot -Force
        }
    }
    [void][IO.Directory]::CreateDirectory($policy)
    foreach ($file in $fieldFiles) { Copy-Item -LiteralPath "field-experiment/$file" -Destination $policy -Force }
    $head = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not verify the checkout revision.' }
    $status = @(& git status --short --branch)
    $hashes = [ordered]@{}
    foreach ($file in $sourceFiles) {
        $hashes["research/privacy-training/$file"] = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $pilot $file)).Hash.ToLowerInvariant()
    }
    foreach ($file in $fieldFiles) {
        $hashes["field-experiment/$file"] = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $policy $file)).Hash.ToLowerInvariant()
    }
    [IO.File]::WriteAllText(
        (Join-Path $run 'code-provenance.json'),
        ([ordered]@{protocol = $protocol; checkout = $head; status = $status; execution_source_sha256 = $hashes} |
            ConvertTo-Json -Depth 5)
    )
    $wslRun = ConvertTo-WslPath $run
    $wslOld = ConvertTo-WslPath $old
    $wslPilot = "$wslRun/source/research/privacy-training"
    $testOutput = @(& wsl.exe -d $Distribution -- env PYTHONDONTWRITEBYTECODE=1 $Python -B -m unittest discover -s $wslPilot -p 'test_*.py' 2>&1)
    $testExit = $LASTEXITCODE
    $testOutput | ForEach-Object { "$_".Replace([string][char]0, '') } | Where-Object { $_ -notmatch '\.wslconfig:' }
    if ($testExit -ne 0) { throw 'Pilot regression tests failed.' }
    $command = @('-d', $Distribution, '--', 'env', "HF_HOME=$pilotEnvironment/hf-cache", 'HF_HUB_OFFLINE=1',
        'PYTHONUNBUFFERED=1', 'PYTHONDONTWRITEBYTECODE=1', $Python, '-B', "$wslPilot/v4_experiment.py",
        '--run', $wslRun, '--old-run', $wslOld, '--binary', $Kaponir, '--phase', $Phase)
    & wsl.exe @command 2>&1 |
        ForEach-Object { "$_".Replace([string][char]0, '') } | Where-Object { $_ -notmatch '\.wslconfig:' }
    if ($LASTEXITCODE -ne 0) { throw 'The v4 experiment exited with an error. Check the study log.' }
} finally {
    Pop-Location
}
