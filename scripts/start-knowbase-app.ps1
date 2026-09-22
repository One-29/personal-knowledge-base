#Requires -Version 5.1

[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Show-LaunchError {
    param([Parameter(Mandatory = $true)][string]$Message)

    try {
        Add-Type -AssemblyName PresentationFramework
        [void][System.Windows.MessageBox]::Show(
            $Message,
            "KnowBase 启动失败",
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        )
    }
    catch {
        Write-Error $Message
    }
}

try {
    $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $pythonwExe = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
    $envFile = Join-Path $projectRoot ".env"
    $frontendBuilder = Join-Path $PSScriptRoot "build-frontend.ps1"

    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $pythonwExe -PathType Leaf)) {
        throw "缺少 .venv。请按 README 创建虚拟环境并安装 desktop 依赖。"
    }
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        throw "缺少 .env。请复制 .env.example 并配置模型 API Key。"
    }
    if (-not (Test-Path -LiteralPath $frontendBuilder -PathType Leaf)) {
        throw "找不到前端构建脚本：$frontendBuilder"
    }

    & $pythonExe -c "import webview" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw '桌面组件未安装。请运行：.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"'
    }

    Set-Location -LiteralPath $projectRoot
    & $frontendBuilder
    if ($LASTEXITCODE -ne 0) {
        throw "前端生产资源准备失败。"
    }

    $process = Start-Process `
        -FilePath $pythonwExe `
        -ArgumentList @("-m", "app.desktop", "--port", $Port) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0) {
        exit $process.ExitCode
    }
}
catch {
    Show-LaunchError -Message $_.Exception.Message
    exit 1
}
