param([Parameter(Mandatory=$true)][string]$ProyectoIntegrado)
$ErrorActionPreference = 'Stop'
function Invoke-Git {
    & git --no-pager @args
    if ($LASTEXITCODE -ne 0) { throw "Git fallo: $args" }
}
$src = (Resolve-Path -LiteralPath $ProyectoIntegrado).Path
$root = (Resolve-Path -LiteralPath (Invoke-Git rev-parse --show-toplevel)).Path
if ($src -eq $root -or $src.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'El ZIP debe estar fuera del repositorio.' }
if (!(Test-Path (Join-Path $src 'scripts/escenarios.py')) -or !(Test-Path (Join-Path $src '.env.example')) -or (Test-Path (Join-Path $src '.git'))) { throw 'Ruta incorrecta del proyecto integrado.' }
Set-Location -LiteralPath $root
if (@(Invoke-Git status --porcelain).Count -ne 0) { throw 'Hay cambios locales. No se modifico el repositorio.' }
if (Test-Path -LiteralPath (Invoke-Git rev-parse --git-path MERGE_HEAD)) { throw 'Ya existe un merge en curso.' }
Invoke-Git fetch origin --prune
$main = 'origin/main'
$orq = 'origin/feature/orquestador'
$bff = 'origin/feature/gateway-bff'
$old = 'a45bd72fe39a29d8dba8961fbf3a046a6894f1b9'
if ((Invoke-Git rev-parse $main) -ne '029ebfc6519db1c29ddc0d7d4afd113348998b52') { throw 'main cambio desde la revision. No se hizo el merge.' }
if ((Invoke-Git rev-parse $bff) -ne '92f832814faceeaaa929d17f3cfdb1d867065e21') { throw 'gateway-bff cambio desde la revision. No se hizo el merge.' }
if (!(Invoke-Git rev-parse $orq).StartsWith('ff5ee92')) { throw 'orquestador cambio nuevamente. No se hizo el merge.' }
if ((Invoke-Git rev-parse main) -ne (Invoke-Git rev-parse $main)) { throw 'main local no coincide con origin/main. Revisa tus commits locales primero.' }
Invoke-Git merge-base --is-ancestor $old $orq
$changed = @(Invoke-Git diff --name-only --no-renames $old $orq)
$ignores = @('.dockerignore','.gitignore')
$unexpected = @()
$stubPaths = @('orquestador/Dockerfile','orquestador/README.md')
function Test-ReviewedOrchestratorFile([string]$path) {
    # The integrated snapshot must contain the real file from the reviewed old branch.
    $reviewedBlob = Invoke-Git rev-parse "${old}:$path"
    $snapshotBlob = Invoke-Git hash-object --no-filters (Join-Path $src $path)
    if ($reviewedBlob -ne $snapshotBlob) { return $false }
    $remoteBlob = Invoke-Git rev-parse "${orq}:$path"
    if ($remoteBlob -eq $reviewedBlob) { return $true }
    # A union merge may have appended the old placeholder from main to the real file.
    # Allow only added lines found in the known placeholder, and no removed nonblank lines.
    $stubLines = @(Invoke-Git show "${main}:$path")
    $diffLines = @(Invoke-Git diff --no-ext-diff --no-textconv --no-color --unified=0 $old $orq -- $path)
    $hasHunk = $false
    $added = 0
    foreach ($line in $diffLines) {
        if ($line.StartsWith('@@ ')) { $hasHunk = $true; continue }
        if (!$hasHunk) { continue }
        if ($line.StartsWith('-') -and $line.Substring(1).Trim().Length -gt 0) { return $false }
        if ($line.StartsWith('+')) {
            $content = $line.Substring(1)
            if ($content.Trim().Length -gt 0) {
                if ($content -cnotin $stubLines) { return $false }
                $added++
            }
        }
    }
    if ($hasHunk -and $added -gt 0) {
        Write-Host "Verificado: $path solo incorpora lineas del stub de main. Se conserva la implementacion real."
        return $true
    }
    return $false
}
foreach ($p in $changed) {
    if ($p -in $ignores) { continue }
    # Only accept newer files whose contents/mode are exactly those already reviewed in main.
    & git --no-pager diff --quiet $main $orq -- $p
    if ($LASTEXITCODE -ne 0) {
        if ($p -in $stubPaths) {
            if (Test-ReviewedOrchestratorFile $p) { continue }
            Write-Host "Cambios en ${p}:"
            Invoke-Git diff --no-color $old $orq -- $p | Out-Host
        }
        $unexpected += $p; continue
    }
    & git cat-file -e "${orq}:$p" 2>$null
    if ($LASTEXITCODE -ne 0) { $unexpected += $p }
}
if ($unexpected.Count -gt 0) { throw ('Cambios nuevos no revisados; no se hizo el merge: ' + ($unexpected -join ', ')) }
# Preserve every new ignore rule; add the reviewed ignore rules below.
$ignoreContents = @{}
foreach ($p in $ignores) {
    $ignoreContents[$p] = @(Invoke-Git show "${orq}:$p")
}
$branch = 'integracion/unidad3-forma-c'
& git show-ref --verify --quiet "refs/heads/$branch"
if ($LASTEXITCODE -eq 0) { throw "La rama $branch ya existe. No se modifico." }
Invoke-Git switch -c $branch $main
# Use the reviewed integrated tree to resolve the merge, retaining original parent commits.
Invoke-Git merge --no-ff --no-commit -s ours $orq $bff
if (!(Test-Path -LiteralPath (Invoke-Git rev-parse --git-path MERGE_HEAD))) { throw 'No se creo MERGE_HEAD. Revisa git status.' }
Get-ChildItem -LiteralPath $src -Force | Where-Object { $_.Name -notin @('.git','.env','.venv','.pytest_cache','__pycache__','.dockerignore','.gitignore') } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $root -Recurse -Force
}
foreach ($p in $ignores) {
    $lines = @($ignoreContents[$p]); foreach ($line in (Get-Content -LiteralPath (Join-Path $src $p))) { if ($line -cnotin $lines) { $lines += $line } }
    [IO.File]::WriteAllLines((Join-Path $root $p), [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
}
# Replace the older script in the reviewed snapshot with this corrected script.
Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $root 'scripts/preparar_pr.ps1') -Force
Invoke-Git add -A
Invoke-Git status --short
Invoke-Git diff --cached --stat
Write-Host 'Integracion preparada. No se hizo commit, push ni PR. Verifica Docker y ejecuta los escenarios.'
