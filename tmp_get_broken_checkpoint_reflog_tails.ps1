$logs = @(
    @{
        Id = "2368808569"
        Path = "C:\Users\asm2m\AppData\Roaming\Code\User\globalStorage\sixth.sixth-ai\checkpoints\2368808569\.git\logs\refs\heads\master"
    },
    @{
        Id = "3071252191"
        Path = "C:\Users\asm2m\AppData\Roaming\Code\User\globalStorage\sixth.sixth-ai\checkpoints\3071252191\.git\logs\refs\heads\master"
    },
    @{
        Id = "677299254"
        Path = "C:\Users\asm2m\AppData\Roaming\Code\User\globalStorage\sixth.sixth-ai\checkpoints\677299254\.git\logs\refs\heads\master"
    }
)

foreach ($entry in $logs) {
    Write-Output "=== $($entry.Id) ==="
    if (Test-Path $entry.Path) {
        Get-Content $entry.Path -Tail 1
    } else {
        Write-Output "MISSING"
    }
    Write-Output ""
}
