$ErrorActionPreference = 'Stop'
$credentialFolder = Join-Path $env:LOCALAPPDATA 'CompetitionHW'
New-Item -ItemType Directory -Path $credentialFolder -Force | Out-Null
$openrouterSecret = Read-Host 'OpenRouter API key' -AsSecureString
if ($openrouterSecret.Length -eq 0) { throw 'API key cannot be empty' }
$openrouterSecret | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $credentialFolder 'openrouter-key.dpapi') -Encoding utf8
Write-Output 'OpenRouter credential saved for this Windows user. Restart the local simulator service to use it.'
