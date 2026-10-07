# Run: powershell -ExecutionPolicy Bypass -File .\check.ps1
$base = (Read-Host 'Render URL, e.g. https://your-service.onrender.com').TrimEnd('/')
$secure = Read-Host 'PROXY_API_KEY (hidden)' -AsSecureString
$key = [System.Net.NetworkCredential]::new('', $secure).Password
$headers = @{ Authorization = "Bearer $key" }
try {
    Write-Host '1. SQL connection test (no model generation)'
    Invoke-RestMethod -Method Post -Uri "$base/diagnostics/sql" -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 210 | ConvertTo-Json -Depth 5
    Write-Host '2. Short model test (uses AI credits)'
    $body = @{model='claude-opus-4-6'; messages=@(@{role='user';content='Reply with one word: hello'});max_tokens=32;stream=$false} | ConvertTo-Json -Depth 5
    Invoke-RestMethod -Method Post -Uri "$base/v1/chat/completions" -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 210 | ConvertTo-Json -Depth 8
} finally {
    $key = $null
    $headers = $null
}
