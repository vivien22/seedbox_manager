<#
  Deploiement de Seedbox Manager depuis le PC (Windows 10/11, client OpenSSH integre).

  Exemples :
    .\deploy.ps1 -PiHost 192.168.1.50            # seedbox puis Raspberry Pi
    .\deploy.ps1 -SeedboxOnly                    # seedbox seulement
    .\deploy.ps1 -PiHost 192.168.1.50 -PiOnly    # Raspberry Pi seulement
    .\deploy.ps1 -PiHost 192.168.1.50 -SetupKeys # installe ta cle SSH (plus de mot de passe ensuite)
#>
param(
    [string]$PiHost = "192.168.1.172",
    [string]$PiUser = "root",
    [string]$SeedboxHost = "bibien@clymene.feralhosting.com",
    [switch]$SeedboxOnly,
    [switch]$PiOnly,
    [switch]$SetupKeys
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nERREUR : $text" -ForegroundColor Red; exit 1 }

function Invoke-Native {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { Fail "$What (code $LASTEXITCODE)" }
}

foreach ($tool in "ssh", "scp") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Fail "$tool introuvable. Installe le 'Client OpenSSH' (Parametres > Applications > Fonctionnalites facultatives)."
    }
}
foreach ($item in "seedbox", "web", "pi", "install-seedbox.sh", "install-kodi.sh") {
    if (-not (Test-Path (Join-Path $here $item))) { Fail "$item manquant a cote de deploy.ps1" }
}

$doSeedbox = -not $PiOnly
$doPi = -not $SeedboxOnly
if ($doPi -and -not $PiHost) {
    $PiHost = Read-Host "Adresse IP de la Raspberry Pi (vide = ignorer la Pi)"
    if (-not $PiHost) { $doPi = $false }
}
$pi = "$PiUser@$PiHost"

# ------------------------------------------------------------- cles SSH
if ($SetupKeys) {
    $key = Join-Path $HOME ".ssh\id_ed25519"
    if (-not (Test-Path $key)) {
        Step "Creation de ta cle SSH ($key)"
        New-Item -ItemType Directory -Force (Split-Path $key) | Out-Null
        Invoke-Native "ssh-keygen" { ssh-keygen -t ed25519 -f $key -N '""' -q }
    }
    $pub = (Get-Content "$key.pub" -Raw).Trim()
    $add = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qxF '$pub' ~/.ssh/authorized_keys 2>/dev/null || echo '$pub' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
    if ($doSeedbox) { Step "Cle SSH -> seedbox"; Invoke-Native "copie de la cle" { ssh $SeedboxHost $add } }
    if ($doPi)      { Step "Cle SSH -> Pi";      Invoke-Native "copie de la cle" { ssh $pi $add } }
}

# --------------------------------------------------------------- seedbox
if ($doSeedbox) {
    Step "Envoi des fichiers sur la seedbox ($SeedboxHost)"
    Invoke-Native "preparation du dossier distant" { ssh $SeedboxHost "rm -rf ~/seedbox-v3 && mkdir -p ~/seedbox-v3" }
    Invoke-Native "copie scp" {
        scp -r (Join-Path $here "seedbox") (Join-Path $here "web") (Join-Path $here "install-seedbox.sh") "${SeedboxHost}:seedbox-v3/"
    }

    Step "Installation sur la seedbox"
    Invoke-Native "install-seedbox.sh" { ssh -t $SeedboxHost "sh ~/seedbox-v3/install-seedbox.sh" }
}

# ----------------------------------------------------------- Raspberry Pi
if ($doPi) {
    Step "Envoi des fichiers sur la Raspberry Pi ($pi)"
    Invoke-Native "preparation du dossier distant" { ssh $pi "rm -rf /storage/seedbox-v3 && mkdir -p /storage/seedbox-v3" }
    Invoke-Native "copie scp" {
        scp -r (Join-Path $here "pi") (Join-Path $here "install-kodi.sh") "${pi}:/storage/seedbox-v3/"
    }

    Step "Installation sur la Raspberry Pi"
    Invoke-Native "install-kodi.sh" { ssh -t $pi "sh /storage/seedbox-v3/install-kodi.sh" }
}

Write-Host "`nDeploiement termine." -ForegroundColor Green
