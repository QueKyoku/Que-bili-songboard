#requires -Version 5.1
# 读取 Windows 系统媒体会话（GSMTC），输出固定格式给 Python 解析。
#
# 输出格式（每个会话三行）：
#   APP    : <来源应用>
#   STATE  : Playing|Paused POS: <秒> DUR: <秒>
#   TITLE  : <曲名> ARTIST: <艺人>
#
# 注意：Windows PowerShell 5.1 默认按系统代码页输出，中文会乱码，
# 所以在最前面就把控制台输出编码切成 UTF-8。
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

try {
    [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime] | Out-Null
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

    function Await($op, $type) {
        $t = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
        $t.Wait(8000) | Out-Null
        return $t.Result
    }

    $mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) `
                 ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
    foreach ($s in $mgr.GetSessions()) {
        $pb = $s.GetPlaybackInfo()
        $tl = $s.GetTimelineProperties()
        $title = ''
        $artist = ''
        try {
            $mp = $s.TryGetMediaPropertiesAsync()
            $mpType = $mp.GetType().GenericTypeArguments[0]
            $props = Await $mp $mpType
            if ($props) {
                $title = [string]$props.Title
                $artist = [string]$props.Artist
            }
        } catch { }

        $pos = [int][math]::Round($tl.Position.TotalSeconds)
        $dur = [int][math]::Round($tl.EndTime.TotalSeconds)

        Write-Output ("APP    : {0}" -f $s.SourceAppUserModelId)
        Write-Output ("STATE  : {0} POS: {1} DUR: {2}" -f $pb.PlaybackStatus, $pos, $dur)
        Write-Output ("TITLE  : {0} ARTIST: {1}" -f $title, $artist)
    }
} catch {
    # 读不到就当没有媒体会话，不要影响调用方
    exit 0
}
