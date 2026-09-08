param([Parameter(Mandatory)][string] $Message)

$ErrorActionPreference = 'Stop'
Write-Output $Message
$taskportArtifact = Join-Path $env:TASKPORT_OUTPUT_DIR 'message.txt'
[System.IO.File]::WriteAllText($taskportArtifact, $Message)
@{ message = $Message } | ConvertTo-Json | Set-Content -LiteralPath $env:TASKPORT_RESULT_FILE -Encoding utf8
