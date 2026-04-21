$repos = @(
    @{
        Id = "2368808569"
        GitDir = "C:\Users\asm2m\AppData\Roaming\Code\User\globalStorage\sixth.sixth-ai\checkpoints\2368808569\.git"
        GoodSha = "94345a5bfc30b2d7c7ceaeff964b476bcebadfcc"
    },
    @{
        Id = "3071252191"
        GitDir = "C:\Users\asm2m\AppData\Roaming\Code\User\globalStorage\sixth.sixth-ai\checkpoints\3071252191\.git"
        GoodSha = "d07f5e213b4f7a3379b916e89152e33ac655cebb"
    },
    @{
        Id = "677299254"
        GitDir = "C:\Users\asm2m\AppData\Roaming\Code\User\globalStorage\sixth.sixth-ai\checkpoints\677299254\.git"
        GoodSha = "e0254da1314deabc21c3bd489cc33675ddcedbbd"
    }
)

foreach ($repo in $repos) {
    Write-Output "=== $($repo.Id) ==="

    $refPath = Join-Path $repo.GitDir "refs\heads\master"
    $headPath = Join-Path $repo.GitDir "HEAD"

    if (Test-Path $refPath) {
        Copy-Item $refPath "$refPath.bak" -Force
        Write-Output "backup: $refPath.bak"
    }

    Set-Content -Path $refPath -Value $repo.GoodSha -Encoding ascii -NoNewline
    Add-Content -Path $refPath -Value "" -Encoding ascii

    if (Test-Path $headPath) {
        $headContent = (Get-Content $headPath -Raw).Trim()
        Write-Output "HEAD: $headContent"
    }

    $resolved = (& git --git-dir="$($repo.GitDir)" rev-parse refs/heads/master 2>$null).Trim()
    if ($LASTEXITCODE -eq 0) {
        Write-Output "resolved master: $resolved"
    } else {
        Write-Output "resolved master: FAILED"
    }

    Write-Output ""
}
