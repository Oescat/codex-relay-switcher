param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('canva', 'github')]
    [string]$Server
)

$ErrorActionPreference = 'Stop'
$taskCodex = Get-Command codex.exe -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -match '[\\/]OpenAI[\\/]Codex[\\/]bin[\\/]' } |
    Select-Object -First 1 -ExpandProperty Source
if (-not $taskCodex) {
    throw 'Codex desktop CLI was not found on PATH. Open Codex once, then reopen PowerShell.'
}

if ($Server -eq 'canva') {
    Write-Host 'This resets only the Canva MCP login, then opens a new Canva authorization.'
    if ((Read-Host 'Continue? [y/N]') -ne 'y') { return }
    & $taskCodex mcp logout canva
    if ($LASTEXITCODE -ne 0) { throw 'Could not reset the Canva MCP login.' }
    & $taskCodex mcp login canva --oauth-client-registration dcr
} else {
    Write-Host 'Opening GitHub authorization using the installed plugin OAuth client.'
    & $taskCodex mcp login github
}
if ($LASTEXITCODE -ne 0) {
    throw 'Authorization did not finish. Keep the error text, but do not share tokens or callback URLs.'
}
Write-Host 'Authorization completed. Restart the local Canvas Agent before reconnecting Canvas.'
