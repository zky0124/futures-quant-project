$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = $env:FQ_PYTHON
if (-not $Python -or -not (Test-Path -LiteralPath $Python)) {
    $VenvPython = Join-Path $Project ".venv\Scripts\python.exe"
    $BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $VenvPython) {
        $Python = $VenvPython
    } elseif (Test-Path -LiteralPath $BundledPython) {
        $Python = $BundledPython
    } else {
        $Python = "python"
    }
}

$Config = Join-Path $Project "configs\api.okx.local.json"
if (-not (Test-Path -LiteralPath $Config)) {
    throw "Missing local OKX config: $Config"
}

Write-Host "OKX 子账户只读诊断：只查询校时、身份、余额/持仓/未完成订单条目数，不会下单。"
Write-Host "请确认本地配置已设置 enabled=true、private_api_enabled=true，并正确选择 demo/live。"
$PreviousPythonPath = $env:PYTHONPATH
$PreviousApiKey = $env:OKX_API_KEY
$PreviousSecretKey = $env:OKX_SECRET_KEY
$PreviousPassphrase = $env:OKX_PASSPHRASE
$LocationPushed = $false
try {
    $ApiKey = Read-Host "OKX API Key（输入不显示）" -AsSecureString
    $env:OKX_API_KEY = [Net.NetworkCredential]::new("", $ApiKey).Password
    $Secret = Read-Host "OKX Secret Key（输入不显示）" -AsSecureString
    $env:OKX_SECRET_KEY = [Net.NetworkCredential]::new("", $Secret).Password
    $Passphrase = Read-Host "OKX API Passphrase（不是登录密码，输入不显示）" -AsSecureString
    $env:OKX_PASSPHRASE = [Net.NetworkCredential]::new("", $Passphrase).Password

    $env:PYTHONPATH = Join-Path $Project "src"
    Push-Location $Project
    $LocationPushed = $true
    & $Python -m futures_quant okx-diagnose --config $Config --connect-read-only
    if ($LASTEXITCODE -ne 0) {
        throw "OKX read-only diagnostic failed with exit code $LASTEXITCODE"
    }
} finally {
    if ($LocationPushed) {
        Pop-Location
    }
    if ($null -eq $PreviousApiKey) {
        Remove-Item Env:OKX_API_KEY -ErrorAction SilentlyContinue
    } else {
        $env:OKX_API_KEY = $PreviousApiKey
    }
    if ($null -eq $PreviousSecretKey) {
        Remove-Item Env:OKX_SECRET_KEY -ErrorAction SilentlyContinue
    } else {
        $env:OKX_SECRET_KEY = $PreviousSecretKey
    }
    if ($null -eq $PreviousPassphrase) {
        Remove-Item Env:OKX_PASSPHRASE -ErrorAction SilentlyContinue
    } else {
        $env:OKX_PASSPHRASE = $PreviousPassphrase
    }
    if ($null -eq $PreviousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $PreviousPythonPath
    }
    Remove-Variable ApiKey, Secret, Passphrase, PreviousApiKey, PreviousSecretKey, PreviousPassphrase -ErrorAction SilentlyContinue
}
