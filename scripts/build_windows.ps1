param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path $PSScriptRoot -Parent)

# Tcl 初始化失败时停止，防止生成缺少 Tk 界面的成品。
& $Python -c 'import tkinter; print(tkinter.Tcl().eval("info patchlevel"))'
if ($LASTEXITCODE -ne 0) { throw '当前 Python 无法初始化 Tcl/Tk，请检查 Python 安装。' }

$toolPath = Join-Path $PWD 'tools/rust-silk.exe'
$toolHash = '8DF2E8B8879A4DE76729A12B3B49569F7FD9F1FFD7EAB851D8DD05CFCD03CE99'
if (-not (Test-Path -LiteralPath $toolPath)) {
    $downloadDir = Join-Path ([IO.Path]::GetTempPath()) ('wechat-qq-build-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    $zipPath = Join-Path $downloadDir 'rust-silk.zip'
    Invoke-WebRequest -Uri 'https://github.com/Wangnov/rust-silk/releases/download/v0.1.3/rust-silk-x86_64-pc-windows-msvc.zip' -OutFile $zipPath
    if ((Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash -ne '44A01BC0F3EC3EC6F6044B656869B0DD7C298329D12F07C7F7A846BE72A89504') {
        throw 'rust-silk 下载包校验失败。'
    }
    Expand-Archive -LiteralPath $zipPath -DestinationPath (Join-Path $downloadDir 'extracted')
    $tool = @(Get-ChildItem -LiteralPath (Join-Path $downloadDir 'extracted') -Filter 'rust-silk.exe' -Recurse)
    if ($tool.Count -ne 1) { throw '下载包中未找到唯一 rust-silk.exe。' }
    New-Item -ItemType Directory -Path 'tools' -Force | Out-Null
    Copy-Item -LiteralPath $tool[0].FullName -Destination $toolPath
}
if ((Get-FileHash -LiteralPath $toolPath -Algorithm SHA256).Hash -ne $toolHash) { throw 'rust-silk 程序校验失败。' }

$arguments = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', 'WeChat-Chat-Export-for-LLM', '--icon', 'assets/app.ico',
    '--collect-all', 'wechatauto', '--collect-all', 'winsdk',
    '--collect-all', 'uiautomation', '--collect-all', 'comtypes',
    '--collect-all', 'imageio_ffmpeg', '--collect-all', 'sherpa_onnx',
    '--collect-all', 'soundfile', '--hidden-import', 'win32timezone',
    '--add-binary', 'tools/rust-silk.exe;tools', 'app.py'
)
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败。' }
$modules = Get-Content -Raw -LiteralPath 'build/WeChat-Chat-Export-for-LLM/PYZ-00.toc'
$analysis = Get-Content -Raw -LiteralPath 'build/WeChat-Chat-Export-for-LLM/Analysis-00.toc'
if (-not $modules.Contains("'tkinter'") -or -not $analysis.Contains("'_tkinter.pyd'") -or
    -not $analysis.Contains('_tcl_data\\init.tcl') -or -not $analysis.Contains('_tk_data\\tk.tcl')) {
    throw '构建缺少 Tk 界面组件，停止打包。'
}

$version = (& $Python -c 'from exporter_core import APP_VERSION; print(APP_VERSION)').Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+-local\.\d+$') { throw '无法确认版本号。' }
$packageDir = Join-Path $PWD 'dist/package'
New-Item -ItemType Directory -Path $packageDir -Force | Out-Null
Copy-Item -LiteralPath 'dist/WeChat-Chat-Export-for-LLM.exe', 'README.md', 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md' -Destination $packageDir -Force
Copy-Item -LiteralPath 'docs' -Destination $packageDir -Recurse -Force
$packagePath = Join-Path $PWD ("dist/wechat-qq-export-Windows-x64-v$version.zip")
Compress-Archive -Path (Join-Path $packageDir '*') -DestinationPath $packagePath -Force
$hash = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath 'dist/SHA256SUMS.txt' -Value ($hash + '  ' + [IO.Path]::GetFileName($packagePath)) -Encoding ascii
Write-Output "已生成发布包：$packagePath"
