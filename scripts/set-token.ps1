# Save a long-lived Claude token for prwatch and restart the container.
#   1. In any terminal: claude setup-token. It opens a browser sign-in. The page then shows a CODE
#      (it contains '#'): paste that code back into the same terminal. The terminal then prints the
#      TOKEN (sk-ant-oat01-...). That token is what this script wants.
#   2. From the prwatch folder: powershell -ExecutionPolicy Bypass -File scripts\set-token.ps1
# The token is read hidden, tried with one tiny Haiku call in the prwatch image, and written to
# claude.env only if that call works. It is never echoed or put on a command line.
# With -Second the token goes to claude2.env as PRWATCH_SECOND_OAUTH_TOKEN: another plan's token, used while the
# first plan is at its usage limit.
param([switch]$Second)
$ErrorActionPreference = "Stop"
$secure = Read-Host "Paste the token from 'claude setup-token'" -AsSecureString
$token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)) -replace '\s', ''
if ($token -match '#') {
    throw "This is the browser sign-in code ($($token.Length) characters, contains '#'). Paste it into the terminal where 'claude setup-token' is waiting; it then prints the token. Nothing written."
}
Write-Host "Trying the token with one Haiku call ($($token.Length) characters)..."
$env:CLAUDE_CODE_OAUTH_TOKEN = $token
try {
    $out = docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN --entrypoint claude prwatch:local -p "Reply with OK" --model haiku 2>&1 | Out-String
    $ok = $LASTEXITCODE -eq 0
} finally { Remove-Item Env:CLAUDE_CODE_OAUTH_TOKEN }
if (-not $ok) { throw "Claude refused this token, nothing written: $($out.Trim())" }
$root = Split-Path $PSScriptRoot -Parent  # the prwatch folder
$envFile = Join-Path $root $(if ($Second) { "claude2.env" } else { "claude.env" })
$envVar = if ($Second) { "PRWATCH_SECOND_OAUTH_TOKEN" } else { "CLAUDE_CODE_OAUTH_TOKEN" }
[IO.File]::WriteAllText($envFile, "$envVar=$token`n", (New-Object Text.UTF8Encoding $false))
Remove-Variable token, secure
Write-Host "Token works; saved $envFile"
$ghUser = (Select-String -Path (Join-Path $root "prwatch.env") -Pattern '^PRWATCH_GH_USER=(\S+)' |
           Select-Object -First 1).Matches.Groups[1].Value
$env:GH_TOKEN = if ($ghUser) { gh auth token -u $ghUser } else { gh auth token }
Push-Location $root
try { docker compose up -d --force-recreate } finally { Pop-Location; Remove-Item Env:GH_TOKEN }
Start-Sleep 20
docker logs --tail 1 prwatch-prwatch-1
