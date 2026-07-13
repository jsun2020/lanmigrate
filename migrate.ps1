# ============================================================
# LanMigrate M0 - 发送端无人值守迁移脚本(Windows 10/11)
# 用法:powershell -ExecutionPolicy Bypass -File migrate.ps1
# 特性:零弹窗、自动重试、断点续传、失败自动循环直到完成
# ============================================================

# ---------------- 配置区(只需要改这里)----------------
$SourceDir  = "D:\"                 # 要迁移的源目录
$RemoteHost = "192.168.1.8"         # 新电脑的局域网 IP(换网后改这里)
$RemotePort = 2022                  # 接收端端口
$RemoteUser = "lanmigrate"          # 与接收端 --user 一致
$RemotePass = "ChangeMe2026"        # 与接收端 --pass 一致
$DestSubDir = "/"                   # 接收目录下的子路径,一般保持 "/"
$MaxRounds  = 100                   # 最大自动重跑轮数
$RetryWait  = 60                    # 每轮之间等待秒数
# --------------------------------------------------------

$ErrorActionPreference = "Continue"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$FilterFile = Join-Path $ScriptDir "filters.txt"
$LogFile    = Join-Path $ScriptDir "migrate.log"

# 检查 rclone 是否可用
if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
    Write-Host "❌ 未找到 rclone,请先安装:winget install Rclone.Rclone" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $FilterFile)) {
    Write-Host "❌ 未找到 filters.txt,请与本脚本放在同一目录" -ForegroundColor Red
    exit 1
}

# 密码混淆(rclone 要求)
$Obscured = (& rclone obscure $RemotePass).Trim()
$Remote   = ":sftp,host=$RemoteHost,port=$RemotePort,user=$RemoteUser,pass=${Obscured}:$DestSubDir"

Write-Host ""
Write-Host "🚀 LanMigrate M0 启动" -ForegroundColor Cyan
Write-Host "   源目录: $SourceDir"
Write-Host "   目标:   sftp://$RemoteHost`:$RemotePort$DestSubDir"
Write-Host "   日志:   $LogFile"
Write-Host "   (随时 Ctrl+C 中断,重跑本脚本自动续传)"
Write-Host ""

for ($Round = 1; $Round -le $MaxRounds; $Round++) {
    Write-Host "════════ 第 $Round 轮传输 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ════════" -ForegroundColor Yellow

    & rclone copy $SourceDir $Remote `
        --filter-from $FilterFile `
        --transfers 8 `
        --checkers 16 `
        --partial-suffix ".part" `
        --retries 5 `
        --retries-sleep 15s `
        --low-level-retries 20 `
        --skip-links `
        --create-empty-src-dirs `
        --stats 10s `
        --stats-one-line `
        --log-file $LogFile `
        --log-level INFO `
        --progress

    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Write-Host "✅ 全部完成!所有文件已成功迁移。" -ForegroundColor Green
        Write-Host "   如需严格校验,参见 M0-迁移指南.md 第五节。"
        exit 0
    }

    Write-Host ""
    Write-Host "⚠️  本轮结束,仍有文件未完成(详见日志)。$RetryWait 秒后自动开始下一轮…" -ForegroundColor Yellow
    Write-Host "   常见原因:文件被占用、网络瞬断——下一轮会自动重试这些文件。"
    Start-Sleep -Seconds $RetryWait
}

Write-Host "❌ 已达最大轮数($MaxRounds)仍未全部完成,请检查日志:$LogFile" -ForegroundColor Red
Write-Host "   提示:grep 日志中的 ERROR 行,通常是个别文件被程序锁定,关闭相关程序后重跑即可。"
exit 1
