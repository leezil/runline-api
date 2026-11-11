param([switch]$EnableTunnel)

Set-Location -LiteralPath $PSScriptRoot
if ($EnableTunnel) {
    python api_server.py --port 8082 --enable-tunnel --tunnel-port 8083
} else {
    python api_server.py --port 8082
}
