# Save only an encrypted Windows-user credential outside the repository.
$ErrorActionPreference = 'Stop'
$credentialDirectory = Join-Path $env:LOCALAPPDATA 'CompetitionHW'
New-Item -ItemType Directory -Force -Path $credentialDirectory | Out-Null
$credentialValue = Read-Host 'DeepSeek API key (hidden)' -AsSecureString
try {
    $credentialValue | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $credentialDirectory 'deepseek-key.dpapi')
} finally {
    $credentialValue.Dispose()
}
Write-Output 'Credential saved for the current Windows user. Restart the preview server.'
