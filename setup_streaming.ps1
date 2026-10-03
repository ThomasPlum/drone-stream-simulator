# 只將 RTSP 工具下載到本工作資料夾，不修改系統或防火牆。
[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$workspacePath = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd([char[]]'\/')
$workspacePrefix = $workspacePath + [IO.Path]::DirectorySeparatorChar

function Assert-WorkspacePath {
    param([string]$Path)
    $fullPath = [IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "路徑不在工作資料夾內：$fullPath"
    }
    return $fullPath
}

function Get-PlainWorkspaceDirectory {
    param([string]$Path)
    $fullPath = Assert-WorkspacePath $Path
    if (-not (Test-Path -LiteralPath $fullPath)) {
        [void](New-Item -ItemType Directory -Path $fullPath)
    }
    $directoryInfo = Get-Item -LiteralPath $fullPath
    if (-not $directoryInfo.PSIsContainer -or ($directoryInfo.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "工具資料夾必須是一般資料夾，不能是連結：$fullPath"
    }
    return $fullPath
}

function Receive-SetupFile {
    param([string]$Uri, [string]$Destination)
    $safePath = Assert-WorkspacePath $Destination
    Write-Host "下載：$Uri"
    Invoke-WebRequest -Uri $Uri -OutFile $safePath -UseBasicParsing -TimeoutSec 300 -Headers @{ 'User-Agent' = 'TMC-RTSP-Simulator-Setup' }
}

function Assert-Sha256 {
    param([string]$Path, [string]$Expected)
    if ($Expected -notmatch '^[0-9a-fA-F]{64}$') {
        throw "SHA-256 格式錯誤：$Path"
    }
    $actualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $Expected.ToLowerInvariant()) {
        throw "下載檔案 SHA-256 不符合來源的校驗碼：$Path"
    }
    return $actualHash
}

function Export-ZipExecutable {
    param([string]$ArchivePath, [string]$Name, [string]$Destination)
    $safeDestination = Assert-WorkspacePath $Destination
    $zipArchive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $entries = @($zipArchive.Entries | Where-Object { $_.Name -ceq $Name })
        if ($entries.Count -ne 1) {
            throw "ZIP 中找不到唯一的 $Name"
        }
        # 只解出指定檔案；不使用 ZIP 內的路徑建立任何目錄。
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entries[0], $safeDestination, $true)
    }
    finally {
        $zipArchive.Dispose()
    }
}

$toolsDirectory = Get-PlainWorkspaceDirectory (Join-Path $workspacePath 'tools')
$executableNames = @('ffmpeg.exe', 'ffprobe.exe', 'mediamtx.exe')
$toolsPresent = $true
foreach ($executableName in $executableNames) {
    if (-not (Test-Path -LiteralPath (Join-Path $toolsDirectory $executableName) -PathType Leaf)) {
        $toolsPresent = $false
    }
}
if ($toolsPresent -and -not $Force) {
    Write-Host "RTSP 工具已存在：$toolsDirectory"
    Write-Host '如需重新下載，請執行 setup_streaming.ps1 -Force。'
    exit 0
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$downloadRoot = Get-PlainWorkspaceDirectory (Join-Path $workspacePath '.setup-downloads')
$downloadDirectory = Get-PlainWorkspaceDirectory (Join-Path $downloadRoot ('streaming-' + [Guid]::NewGuid().ToString('N')))
$ownedFiles = @()

try {
    $ffmpegUrl = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
    $ffmpegChecksumUrl = $ffmpegUrl + '.sha256'
    $ffmpegVersionUrl = $ffmpegUrl + '.ver'
    $ffmpegArchive = Join-Path $downloadDirectory 'ffmpeg-essentials.zip'
    $ffmpegChecksumFile = Join-Path $downloadDirectory 'ffmpeg.sha256'
    $ffmpegVersionFile = Join-Path $downloadDirectory 'ffmpeg.ver'
    $ownedFiles += @($ffmpegArchive, $ffmpegChecksumFile, $ffmpegVersionFile)
    Receive-SetupFile -Uri $ffmpegChecksumUrl -Destination $ffmpegChecksumFile
    Receive-SetupFile -Uri $ffmpegVersionUrl -Destination $ffmpegVersionFile
    $ffmpegExpected = [IO.File]::ReadAllText($ffmpegChecksumFile).Trim()
    $ffmpegVersion = [IO.File]::ReadAllText($ffmpegVersionFile).Trim()
    $ffmpegActualUrl = $ffmpegUrl
    try {
        # Gyan 官方下載頁亦提供其 GitHub mirror；大檔優先由 mirror 下載。
        $ffmpegReleaseApi = 'https://api.github.com/repos/GyanD/codexffmpeg/releases/tags/' + [Uri]::EscapeDataString($ffmpegVersion)
        $ffmpegRelease = Invoke-RestMethod -Uri $ffmpegReleaseApi -TimeoutSec 30 -Headers @{ 'User-Agent' = 'TMC-RTSP-Simulator-Setup'; 'Accept' = 'application/vnd.github+json' }
        $ffmpegAssetName = 'ffmpeg-' + $ffmpegVersion + '-essentials_build.zip'
        $ffmpegMirrorAssets = @($ffmpegRelease.assets | Where-Object { $_.name -ceq $ffmpegAssetName })
        if ($ffmpegMirrorAssets.Count -ne 1) {
            throw "官方 mirror 找不到唯一的 $ffmpegAssetName"
        }
        $ffmpegActualUrl = $ffmpegMirrorAssets[0].browser_download_url
    }
    catch {
        Write-Warning ('無法取得 Gyan 官方 GitHub mirror，改用 Gyan 原始下載網址：' + $_.Exception.Message)
    }
    Receive-SetupFile -Uri $ffmpegActualUrl -Destination $ffmpegArchive
    $ffmpegArchiveHash = Assert-Sha256 -Path $ffmpegArchive -Expected $ffmpegExpected

    $releaseApi = 'https://api.github.com/repos/bluenviron/mediamtx/releases/latest'
    $release = Invoke-RestMethod -Uri $releaseApi -Headers @{ 'User-Agent' = 'TMC-RTSP-Simulator-Setup'; 'Accept' = 'application/vnd.github+json' }
    $mediaAssets = @($release.assets | Where-Object { $_.name -match '^mediamtx_.+_windows_amd64\.zip$' })
    $checksumAssets = @($release.assets | Where-Object { $_.name -ceq 'checksums.sha256' })
    if ($mediaAssets.Count -ne 1 -or $checksumAssets.Count -ne 1) {
        throw 'MediaMTX 官方發行頁找不到唯一的 Windows amd64 ZIP 或 checksums.sha256。'
    }
    $mediaAsset = $mediaAssets[0]
    $mediaArchive = Join-Path $downloadDirectory 'mediamtx.zip'
    $mediaChecksumFile = Join-Path $downloadDirectory 'mediamtx-checksums.sha256'
    $ownedFiles += @($mediaArchive, $mediaChecksumFile)
    Receive-SetupFile -Uri $checksumAssets[0].browser_download_url -Destination $mediaChecksumFile
    Receive-SetupFile -Uri $mediaAsset.browser_download_url -Destination $mediaArchive
    $mediaExpected = $null
    foreach ($checksumLine in [IO.File]::ReadAllLines($mediaChecksumFile)) {
        if ($checksumLine -match '^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$' -and $Matches[2] -ceq $mediaAsset.name) {
            $mediaExpected = $Matches[1]
            break
        }
    }
    if (-not $mediaExpected) {
        throw "校驗碼清單找不到 $($mediaAsset.name)"
    }
    $mediaArchiveHash = Assert-Sha256 -Path $mediaArchive -Expected $mediaExpected

    foreach ($executableName in $executableNames) {
        $stagedExecutable = Join-Path $downloadDirectory $executableName
        $ownedFiles += $stagedExecutable
        $sourceArchive = if ($executableName -eq 'mediamtx.exe') { $mediaArchive } else { $ffmpegArchive }
        Export-ZipExecutable -ArchivePath $sourceArchive -Name $executableName -Destination $stagedExecutable
    }

    $installedHashes = [ordered]@{}
    foreach ($executableName in $executableNames) {
        $stagedExecutable = Join-Path $downloadDirectory $executableName
        $destinationExecutable = Assert-WorkspacePath (Join-Path $toolsDirectory $executableName)
        Copy-Item -LiteralPath $stagedExecutable -Destination $destinationExecutable -Force
        $installedHashes[$executableName] = (Get-FileHash -LiteralPath $destinationExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $versions = [ordered]@{
        setup_utc = [DateTime]::UtcNow.ToString('o')
        ffmpeg = [ordered]@{ version = $ffmpegVersion; url = $ffmpegActualUrl; upstream_url = $ffmpegUrl; checksum_url = $ffmpegChecksumUrl; archive_sha256 = $ffmpegArchiveHash }
        mediamtx = [ordered]@{ version = $release.tag_name; url = $mediaAsset.browser_download_url; checksum_url = $checksumAssets[0].browser_download_url; archive_sha256 = $mediaArchiveHash }
        executable_sha256 = $installedHashes
    }
    $versionsPath = Assert-WorkspacePath (Join-Path $toolsDirectory 'versions.json')
    [IO.File]::WriteAllText($versionsPath, ($versions | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
    Write-Host "RTSP 工具準備完成：$toolsDirectory"
    Write-Host "FFmpeg $ffmpegVersion / MediaMTX $($release.tag_name)；ZIP SHA-256 已驗證。"
}
finally {
    # 僅刪除本次建立的已知檔案；不遞迴刪除工作資料夾。
    foreach ($ownedFile in $ownedFiles) {
        $safeFile = Assert-WorkspacePath $ownedFile
        if (-not $safeFile.StartsWith($downloadDirectory + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "清理路徑不屬於本次下載：$safeFile"
        }
        if (Test-Path -LiteralPath $safeFile -PathType Leaf) {
            Remove-Item -LiteralPath $safeFile -Force
        }
    }
    $safeDownloadDirectory = Assert-WorkspacePath $downloadDirectory
    # 沒有 -Recurse；只有目錄為空時才會移除。
    if ((Test-Path -LiteralPath $safeDownloadDirectory -PathType Container) -and
        @(Get-ChildItem -LiteralPath $safeDownloadDirectory -Force).Count -eq 0) {
        Remove-Item -LiteralPath $safeDownloadDirectory
    }
}
