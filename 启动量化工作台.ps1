$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Candidates = @(
    @(
        $env:FQ_PYTHON,
        (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
        (Join-Path $HOME ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
    ) | Where-Object { $_ -and (Test-Path $_) }
)

if (-not $Candidates) {
    throw "Python not found. Create .venv or set FQ_PYTHON to the full interpreter path."
}

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Set-Location $ProjectRoot
$Python = [string]$Candidates[0]
& $Python -m futures_quant dashboard
if ($LASTEXITCODE -ne 0) {
    throw "Workbench exited with code $LASTEXITCODE."
}
