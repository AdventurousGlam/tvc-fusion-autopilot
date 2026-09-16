<?php
/**
 * CORS proxy for Farside ETF flow data.
 * Upload to Hostinger: /terminal/etf-proxy.php
 * Usage: /terminal/etf-proxy.php?asset=btc  (or eth)
 */
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET');
header('Content-Type: text/html; charset=UTF-8');
header('Cache-Control: public, max-age=1800');

$asset = strtolower($_GET['asset'] ?? 'btc');
$urls = [
    'btc' => 'https://farside.co.uk/bitcoin-etf-flow-all-data/',
    'eth' => 'https://farside.co.uk/ethereum-etf-flow-all-data/',
];
if (!isset($urls[$asset])) { http_response_code(400); echo 'bad asset'; exit; }

$ch = curl_init($urls[$asset]);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_FOLLOWLOCATION => true,
    CURLOPT_TIMEOUT        => 25,
    CURLOPT_CONNECTTIMEOUT => 10,
    CURLOPT_SSL_VERIFYPEER => true,
    CURLOPT_ENCODING       => '',          // accept gzip/deflate
    CURLOPT_HTTPHEADER     => [
        'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language: en-US,en;q=0.9',
        'Referer: https://farside.co.uk/',
        'DNT: 1',
    ],
]);
$html = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$err  = curl_error($ch);
curl_close($ch);

if ($html === false || $code >= 400) {
    http_response_code(502);
    echo "upstream error: HTTP $code / $err";
    exit;
}
echo $html;
