$base = "C:\Users\asm2m\AppData\Roaming\Code\User\globalStorage\sixth.sixth-ai\checkpoints"

Get-ChildItem -Path $base -Directory | ForEach-Object {
    $repoId = $_.Name
    $gitDir = Join-Path $_.FullName ".git"

    if (-not (Test-Path $gitDir)) {
        return
    }

    Write-Output "=== $repoId ==="

    $headPath = Join-Path $gitDir "HEAD"
    if (Test-Path $headPath) {
        Write-Output ("HEAD: " + (Get-Content $headPath -Raw).Trim())
    } else {
        Write-Output "HEAD: MISSING"
    }

    $masterRef = Join-Path $gitDir "refs\heads\master"
    if (Test-Path $masterRef) {
        Write-Output ("master ref: " + (Get-Content $masterRef -Raw).Trim())
    } else {
        Write-Output "master ref: MISSING"
    }

    $revParse = cmd /c "set GIT_DIR=$gitDir&& git rev-parse HEAD 2>&1"
    Write-Output ("rev-parse: " + (($revParse | Out-String).Trim()))

    $fsck = cmd /c "set GIT_DIR=$gitDir&& git fsck --full 2>&1"
    $fsckText = ($fsck | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($fsckText)) {
        Write-Output "fsck: clean"
    } else {
        Write-Output ("fsck: " + $fsckText)
    }

    Write-Output ""
}
