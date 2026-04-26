$lines = Get-Content 'app/pjsua2_service.py'
$match = Select-String -Path 'app/pjsua2_service.py' -Pattern 'terminal_state_detected ='
if ($null -eq $match) { Write-Output 'NO_MATCH'; exit 1 }
$first = $match | Select-Object -First 1
$lineNumber = [int]$first.LineNumber
$start = [Math]::Max(0, $lineNumber - 25)
$end = [Math]::Min($lines.Length - 1, $lineNumber + 55)
for ($i = $start; $i -le $end; $i++) {
  '{0}: {1}' -f ($i + 1), $lines[$i]
}
