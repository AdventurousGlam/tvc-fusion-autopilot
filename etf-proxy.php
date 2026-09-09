<?php
/**
 * Tiny CORS proxy for Farside ETF flow data.
 * Upload to Hostinger: /terminal/etf-proxy.php
 * Usage: /terminal/etf-proxy.php?asset=btc  (or eth)
 */
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET');
header('Content-Type: text/html; charset=UTF-8');
header('Cache-Control: public, max-age=1800'); // 30 min

$asset = strtolower($_GET['asset'] ?? 'btc');
$urls = [
    'btc' => 'https://farside.co.uk/bitcoin-etf-flow-all-data/',
    'eth' => 'https://farside.co.uk/ethereum-etf-flow-all-data/',
];
if (!isset($urls[$asset])) { http_response_code(400); echo 'bad asset'; exit; }

$ctx = stream_context_create(['http' => [
    'timeout' => 20,
    'header' => "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\nAccept: text/html\r\n",
    'ignore_errors' => true,
]]);
$html = @file_get_contents($urls[$asset], false, $ctx);
if ($html === false) { http_response_code(502); echo 'upstream error'; exit; }
echo $html;
