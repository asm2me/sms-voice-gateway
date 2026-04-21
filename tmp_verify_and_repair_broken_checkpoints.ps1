$repos = @(
    @{
        Id = "2368808569"
        GitDir = "C:\Users\asm2m\AppData\Roaming\Code\User\globalStorage\sixth.sixth-ai\checkpoints\2368808569\.git"
        OldSha = "f298c0abac0caa204492817721750ac467db5f76"
        NewSha = "94345a5bfc30b2d7c7ceaeff964b476bcebadfcc"
    },
    @{
        Id = "3071252191"
        GitDir = "C:\Users\asm2m\AppData\Roaming\Code\User\globalStorage\sixth.sixth-ai\checkpoints\3071252191\.git"
        OldSha = "71780b9963bfbdec8860102ddbbc01d946111f24"
        NewSha = "d07f5e213b4f7a3379b916e89152e33ac655cebb"
    },
    @{
        Id = "677299254"
        GitDir = "C:\Users\asm2m\AppData\Roaming\Code\User\globalStorage\sixth.sixth-ai\checkpoints\677299254\.git"
        OldSha = "28a271aba9ea832953683212a32c366d8ce4f540"
        NewSha = "e0254da1314deabc21c3bd489cc33675ddcedbbd"
    }
)

foreach ($repo in $repos) {
    Write-Output "=== $($repo.Id) ==="

    $currentRef = ""
    $refPath = Join-Path $repo.GitDir "refs\heads\master"
    if (Test-Path $refPath) {
        $currentRef = (Get-Content $refPath -Raw).Trim()
        Write-Output "current master: $currentRef"
    } else {
        Write-Output "current master: MISSING"
    }

    & git --git-dir="$($repo.GitDir)" cat-file -e "$($repo.OldSha)^{commit}" 2>$null
    $oldExists = ($LASTEXITCODE -eq 0)
    Write-Output "old exists: $oldExists ($($repo.OldSha))"

    & git --git-dir="$($repo.GitDir)" cat-file -e "$($repo.NewSha)^{commit}" 2>$null
    $newExists = ($LASTEXITCODE -eq 0)
    Write-Output "new exists: $newExists ($($repo.NewSha))"

    if ($oldExists -and (-not $newExists)) {
        & git --git-dir="$($repo.GitDir)" update-ref refs/heads/master "$($repo.OldSha)"
        if ($LASTEXITCODE -eq 0) {
            Write-Output "repair: updated master to old sha"
        } else {
            Write-Output "repair: FAILED update-ref"
        }
    } else {
        Write-Output "repair: skipped"
    }

    Write-Output ""
}
