param()
$ErrorActionPreference = 'Stop'
$python = Join-Path $PWD '.venv/Scripts/python.exe'
$stamp = '.venv/rocket3-setup.txt'
$signature = (Get-FileHash $PSCommandPath).Hash + (Get-FileHash requirements.txt).Hash
if ((Test-Path $python) -and (Test-Path $stamp) -and (Get-Content $stamp -Raw).Trim() -eq $signature) { return }
function Invoke-Uv {
    & uv @args
    if ($LASTEXITCODE) { throw "uv failed with exit code $LASTEXITCODE" }
}
if (!(Test-Path $python)) { Invoke-Uv venv --python 3.12 .venv }
Invoke-Uv pip install --python $python --index-url https://download.pytorch.org/whl/cu130 'torch==2.14.1+cu130' 'torchvision==0.29.1+cu130'
Invoke-Uv pip install --python $python -r requirements.txt
# The full MineStudio install targets simulation and RL training. The policy
# imports just its PyTorch modules, gym spaces and gym3 type definitions.
Invoke-Uv pip install --python $python --no-deps minestudio==1.1.6 gym==0.26.2 gym3==0.3.3
Set-Content -LiteralPath $stamp -Value $signature

