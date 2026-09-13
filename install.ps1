<#
.SYNOPSIS
    Installe BankExtract sur un poste Windows.

.DESCRIPTION
    Vérifie Python, crée un environnement isolé, installe le logiciel et le
    navigateur nécessaire, puis prépare les fichiers de configuration.

    À lancer dans PowerShell :
        powershell -ExecutionPolicy Bypass -File install.ps1

.NOTES
    Rien n'est installé en dehors du dossier choisi, hormis Python lui-même
    si vous acceptez son installation.
#>

[CmdletBinding()]
param(
    # Dossier d'installation.
    [string]$Dossier = "$env:USERPROFILE\BankExtract",

    # Branche du dépôt à télécharger.
    [string]$Branche = "claude/bank-statement-extraction-5uas3w",

    # Dépôt GitHub source.
    [string]$Depot = "azzouz-dz-hue/Fable5"
)

$ErrorActionPreference = "Stop"
$PSDefaultParameterValues['*:Encoding'] = 'utf8'

function Etape($texte) { Write-Host "`n== $texte" -ForegroundColor Cyan }
function Bon($texte)   { Write-Host "   [ok] $texte" -ForegroundColor Green }
function Souci($texte) { Write-Host "   [!]  $texte" -ForegroundColor Yellow }
function Stopper($texte) { Write-Host "`n[ECHEC] $texte" -ForegroundColor Red; exit 1 }

Write-Host @"

  BankExtract — installation
  Extraction automatisee des releves de comptes bancaires

"@ -ForegroundColor White

# ---------------------------------------------------------------- 1. Python

Etape "Recherche de Python"

$python = $null
foreach ($candidat in @("py -3.12", "py -3.11", "py -3.10", "py -3", "python")) {
    $morceaux = $candidat.Split(" ")
    $exe = $morceaux[0]
    $arguments = if ($morceaux.Count -gt 1) { $morceaux[1..($morceaux.Count - 1)] } else { @() }
    try {
        $version = & $exe @arguments --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -match "Python (\d+)\.(\d+)") {
            if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10) {
                $python = @{ Exe = $exe; Args = $arguments; Version = $version }
                break
            }
        }
    } catch { }
}

if (-not $python) {
    Souci "Python 3.10 ou plus recent est introuvable."
    $reponse = Read-Host "   Installer Python maintenant via winget ? (O/n)"
    if ($reponse -eq "" -or $reponse -match "^[oO]") {
        try {
            winget install --id Python.Python.3.12 --source winget --accept-package-agreements --accept-source-agreements
        } catch {
            Stopper "Installation automatique impossible. Installez Python depuis https://www.python.org/downloads/ (cochez « Add python.exe to PATH »), puis relancez ce script."
        }
        Stopper "Python vient d'etre installe. FERMEZ cette fenetre, rouvrez PowerShell et relancez ce script."
    }
    Stopper "Python est necessaire. Telechargez-le sur https://www.python.org/downloads/"
}
Bon "$($python.Version) detecte"

# ---------------------------------------------------------------- 2. Sources

Etape "Preparation du dossier $Dossier"

$racine = Join-Path $Dossier "app"

# $PSScriptRoot est vide quand le script arrive par un tube (« irm ... | iex ») :
# dans ce cas il n'y a pas de sources locales, on telecharge.
$surPlace = $PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml"))

if ($surPlace) {
    # Le script est lance depuis les sources deja presentes.
    $racine = $PSScriptRoot
    Bon "sources trouvees sur place"
} else {
    New-Item -ItemType Directory -Force -Path $Dossier | Out-Null
    $temporaire = [System.IO.Path]::GetTempPath()
    $archive = Join-Path $temporaire "bankextract-source.zip"
    $url = "https://github.com/$Depot/archive/refs/heads/$Branche.zip"
    Write-Host "   Telechargement depuis $url"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
    } catch {
        Stopper "Telechargement impossible : $($_.Exception.Message)"
    }

    $extraction = Join-Path $temporaire "bankextract-extrait"
    if (Test-Path $extraction) { Remove-Item $extraction -Recurse -Force }
    Expand-Archive -Path $archive -DestinationPath $extraction -Force

    $source = Get-ChildItem $extraction -Directory | Select-Object -First 1
    if (Test-Path $racine) { Remove-Item $racine -Recurse -Force }
    Move-Item $source.FullName $racine
    Remove-Item $archive, $extraction -Recurse -Force -ErrorAction SilentlyContinue
    Bon "sources installees dans $racine"
}

# ---------------------------------------------------------------- 3. Environnement

Etape "Creation de l'environnement Python"

$venv = Join-Path $racine ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    & $python.Exe @($python.Args) -m venv $venv
    if ($LASTEXITCODE -ne 0) { Stopper "Creation de l'environnement impossible." }
}
$pyvenv = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $pyvenv)) {
    Stopper "L'environnement Python n'a pas ete cree ($pyvenv introuvable). Verifiez que Python est complet, puis relancez."
}
Bon "environnement pret"

Etape "Installation du logiciel (quelques minutes)"
& $pyvenv -m pip install --quiet --upgrade pip
& $pyvenv -m pip install --quiet $racine
if ($LASTEXITCODE -ne 0) { Stopper "Installation des composants impossible." }
Bon "logiciel installe"

# ---------------------------------------------------------------- 4. Navigateur

Etape "Installation du navigateur (environ 150 Mo)"
& $pyvenv -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Souci "Le navigateur n'a pas pu etre telecharge. Relancez plus tard : bankextract setup"
} else {
    Bon "navigateur installe"
}

# ---------------------------------------------------------------- 5. Espace de travail

Etape "Preparation de l'espace de travail"

$travail = Join-Path $Dossier "donnees"
New-Item -ItemType Directory -Force -Path (Join-Path $travail "config") | Out-Null

foreach ($fichier in @("banks.yaml", "schedules.yaml")) {
    $cible = Join-Path $travail "config\$fichier"
    $modele = Join-Path $racine "config\$fichier"
    if ((Test-Path $modele) -and -not (Test-Path $cible)) {
        Copy-Item $modele $cible
        Bon "config\$fichier cree"
    } elseif (Test-Path $cible) {
        Souci "config\$fichier existe deja — conserve"
    }
}

$env_cible = Join-Path $travail ".env"
$env_modele = Join-Path $racine ".env.example"
if ((Test-Path $env_modele) -and -not (Test-Path $env_cible)) {
    Copy-Item $env_modele $env_cible
    Bon ".env cree (vos identifiants iront dedans)"
}

# ---------------------------------------------------------------- 6. Lanceur

Etape "Creation du lanceur"

$lanceur = Join-Path $travail "bankextract.cmd"
@"
@echo off
rem Lanceur BankExtract — ouvre une invite de commandes prete a l'emploi.
cd /d "%~dp0"
set "PATH=$venv\Scripts;%PATH%"
if "%~1"=="" (
    echo.
    echo   BankExtract est pret. Commandes utiles :
    echo.
    echo     bankextract setup                 preparer le poste
    echo     bankextract record bna --url ...  enregistrer un parcours
    echo     bankextract replay bna            rejouer le parcours
    echo     bankextract dashboard             tableau de bord
    echo     bankextract --help                aide complete
    echo.
    cmd /k
) else (
    "$venv\Scripts\bankextract.exe" %*
)
"@ | Set-Content -Path $lanceur -Encoding ASCII
Bon "lanceur cree : $lanceur"

$bureau = [Environment]::GetFolderPath("Desktop")
if (Test-Path $bureau) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        $raccourci = $shell.CreateShortcut((Join-Path $bureau "BankExtract.lnk"))
        $raccourci.TargetPath = $lanceur
        $raccourci.WorkingDirectory = $travail
        $raccourci.Description = "Extraction des releves de comptes bancaires"
        $raccourci.Save()
        Bon "raccourci place sur le Bureau"
    } catch {
        Souci "Raccourci non cree (sans consequence)."
    }
}

# ---------------------------------------------------------------- 7. Verification

Etape "Verification"
Push-Location $travail
& (Join-Path $venv "Scripts\bankextract.exe") setup
Pop-Location

Write-Host @"

  Installation terminee.

  Dossier de travail : $travail
  Ouvrez le raccourci BankExtract sur votre Bureau, puis enregistrez
  le parcours de votre banque :

      bankextract record bna --url https://ebanking.bna.dz

  Une fenetre s'ouvrira : connectez-vous, telechargez un releve,
  puis fermez la fenetre. Le logiciel refera cela tout seul ensuite.

"@ -ForegroundColor Green
