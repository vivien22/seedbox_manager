<#
.SYNOPSIS
  Deploiement et gestion de Seedbox Manager depuis le PC.

.DESCRIPTION
  Sans option : met a jour la seedbox puis la Raspberry Pi.

  Deploiement
    .\deploy.ps1                     seedbox puis Raspberry Pi
    .\deploy.ps1 -SeedboxOnly        seedbox seulement
    .\deploy.ps1 -PiOnly             Raspberry Pi seulement
    .\deploy.ps1 -SetupKeys          installe ta cle SSH (plus de mot de passe ensuite)
    .\deploy.ps1 -PiHost 192.168.1.x autre adresse pour la Pi (defaut : 192.168.1.100)

  Appareils autorises sur le site
    .\deploy.ps1 -PairPhone          code pour autoriser le telephone
    .\deploy.ps1 -PairDevice PC      code pour autoriser un autre appareil (nom libre)
    .\deploy.ps1 -ListDevices        liste des appareils autorises

  Suivi
    .\deploy.ps1 -PiStatus           transferts en cours sur la Pi, vitesse en direct

  Aide
    .\deploy.ps1 -h                  affiche cette aide
#>
[CmdletBinding()]
param(
    [string]$PiHost = "192.168.1.100",
    [string]$PiUser = "root",
    [string]$SeedboxHost = "bibien@clymene.feralhosting.com",
    [switch]$SeedboxOnly,
    [switch]$PiOnly,
    [switch]$SetupKeys,
    [switch]$PiStatus,
    [switch]$PairPhone,
    [string]$PairDevice = "",
    [switch]$ListDevices,
    [Alias("h")]
    [switch]$Help
)

if ($Help) {
    $text = Get-Content $PSCommandPath -Raw
    $begin = $text.IndexOf(".DESCRIPTION") + ".DESCRIPTION".Length
    $finish = $text.IndexOf("#>")
    Write-Host ("`nSeedbox Manager - deploy.ps1`n" + $text.Substring($begin, $finish - $begin).TrimEnd())
    Write-Host ""
    exit 0
}

$ErrorActionPreference = "Stop"

# Retire les fins de ligne Windows (CRLF) des fichiers copies, au cas ou git
# les aurait converties sur le PC (core.autocrlf).
function FixEol($dir) { return "find $dir -type f | while read f; do tr -d \\r < `$f > `$f.eol && cat `$f.eol > `$f && rm -f `$f.eol; done" }
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

# ------------------------------------------- autorisation du telephone
$pairScript = "/media/sdt1/bibien/private/seedbox-manager/pair-device.sh"
if ($PairPhone -and -not $PairDevice) { $PairDevice = "Telephone" }
if ($PairDevice) {
    if ($PairDevice -notmatch '^[A-Za-z0-9._-]{1,30}$') { Fail "Nom d'appareil : lettres, chiffres, . _ - (30 caracteres max, sans espace ni accent)" }
    Invoke-Native "pair-device.sh" { ssh $SeedboxHost "sh $pairScript $PairDevice" }
    exit 0
}
if ($ListDevices) {
    Invoke-Native "pair-device.sh" { ssh $SeedboxHost "sh $pairScript --list" }
    exit 0
}

# --------------------------------------------- suivi des transferts sur la Pi
if ($PiStatus) {
    $pi = "$PiUser@$PiHost"
    Invoke-Native "copie de seedbox-status.py" { scp -q (Join-Path $here "pi\seedbox-status.py") "${pi}:/storage/.config/seedbox-status.py" }
    ssh -t $pi "python3 /storage/.config/seedbox-status.py --watch"
    exit 0
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
    Invoke-Native "install-seedbox.sh" { ssh -t $SeedboxHost ((FixEol "~/seedbox-v3") + " && sh ~/seedbox-v3/install-seedbox.sh") }
}

# ----------------------------------------------------------- Raspberry Pi
if ($doPi) {
    Step "Envoi des fichiers sur la Raspberry Pi ($pi)"
    Invoke-Native "preparation du dossier distant" { ssh $pi "rm -rf /storage/seedbox-v3 && mkdir -p /storage/seedbox-v3" }
    Invoke-Native "copie scp" {
        scp -r (Join-Path $here "pi") (Join-Path $here "install-kodi.sh") "${pi}:/storage/seedbox-v3/"
    }

    Step "Installation sur la Raspberry Pi"
    Invoke-Native "install-kodi.sh" { ssh -t $pi ((FixEol "/storage/seedbox-v3") + " && sh /storage/seedbox-v3/install-kodi.sh") }
}

Write-Host "`nDeploiement termine." -ForegroundColor Green
