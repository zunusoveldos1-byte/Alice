$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$alicePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $alicePython)) {
    throw 'Сначала создайте .venv и установите зависимости по README.md.'
}
& $alicePython -m alice_voice
