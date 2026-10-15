#!/usr/bin/env node
/* check_js_globals.js — szuka nazw używanych w JS, które nigdzie nie są
 * zadeklarowane i nie są globalnymi przeglądarki. To jest `no-undef`, tylko
 * bez całego ESLinta.
 *
 * Powstało 15.10.2026, po tym jak na żywej stronie wyszło `i18n is not defined`
 * w dwóch panelach (obiekt nazywa się `I18N`). `node --check` tego nie widzi,
 * bo składnia jest poprawna — błąd wychodzi dopiero przy wykonaniu funkcji
 * renderującej. Dwie próby złapania tego wyrażeniami regularnymi dały 11 i 15
 * fałszywych alarmów, bo regex nie odróżnia kodu od tekstu w stringach
 * i komentarzach. Acorn parsuje JS na drzewo, więc wie dokładnie, co jest
 * nazwą, a co znakami w cudzysłowie — i daje zero fałszywych trafień.
 *
 * Użycie:  node check_js_globals.js plik.html [plik2.html ...]
 * Kod wyjścia: 0 = czysto, 1 = są nieistniejące nazwy, 2 = brak zależności.
 */
'use strict';
const fs = require('fs');

let globals;
try {
  globals = require('acorn-globals');
} catch (e) {
  console.error('   brak acorn-globals — zainstaluj: npm install acorn acorn-globals');
  process.exit(2);
}

// Globalne przeglądarki i te, które terminal dociąga z CDN.
// Lista jest jawna i krótka z rozmysłem: jeśli coś nowego będzie potrzebne,
// lepiej dopisać tutaj świadomie niż poluzować całe sprawdzenie.
const ALLOWED = new Set([
  // ECMAScript
  'globalThis', 'undefined', 'NaN', 'Infinity', 'Object', 'Function', 'Boolean', 'Symbol',
  'Error', 'EvalError', 'RangeError', 'ReferenceError', 'SyntaxError', 'TypeError', 'URIError',
  'AggregateError', 'Number', 'BigInt', 'Math', 'Date', 'String', 'RegExp', 'Array',
  'Int8Array', 'Uint8Array', 'Uint8ClampedArray', 'Int16Array', 'Uint16Array', 'Int32Array',
  'Uint32Array', 'Float32Array', 'Float64Array', 'BigInt64Array', 'BigUint64Array',
  'Map', 'Set', 'WeakMap', 'WeakSet', 'WeakRef', 'ArrayBuffer', 'SharedArrayBuffer',
  'DataView', 'JSON', 'Promise', 'Reflect', 'Proxy', 'Intl', 'parseInt', 'parseFloat',
  'isNaN', 'isFinite', 'decodeURI', 'decodeURIComponent', 'encodeURI', 'encodeURIComponent',
  'escape', 'unescape', 'eval', 'structuredClone', 'queueMicrotask', 'reportError',
  // DOM i okno
  'window', 'self', 'document', 'navigator', 'location', 'history', 'screen', 'frames',
  'parent', 'top', 'opener', 'console', 'alert', 'confirm', 'prompt', 'close', 'open',
  'print', 'focus', 'blur', 'scroll', 'scrollTo', 'scrollBy', 'getComputedStyle',
  'matchMedia', 'devicePixelRatio', 'innerWidth', 'innerHeight', 'outerWidth', 'outerHeight',
  'localStorage', 'sessionStorage', 'indexedDB', 'caches', 'crypto', 'performance',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
  'requestAnimationFrame', 'cancelAnimationFrame', 'requestIdleCallback', 'cancelIdleCallback',
  'fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'Worker', 'SharedWorker',
  'AbortController', 'AbortSignal', 'Headers', 'Request', 'Response', 'FormData',
  'URL', 'URLSearchParams', 'Blob', 'File', 'FileReader', 'TextEncoder', 'TextDecoder',
  'Image', 'Audio', 'Option', 'Notification', 'MutationObserver', 'ResizeObserver',
  'IntersectionObserver', 'Event', 'CustomEvent', 'MessageChannel', 'BroadcastChannel',
  'Element', 'HTMLElement', 'Node', 'NodeList', 'DOMParser', 'XMLSerializer',
  'getSelection', 'atob', 'btoa', 'postMessage', 'addEventListener', 'removeEventListener',
  'dispatchEvent', 'speechSynthesis', 'SpeechSynthesisUtterance', 'AudioContext',
  'webkitAudioContext', 'ClipboardItem', 'Path2D', 'OffscreenCanvas', 'createImageBitmap',
  // z CDN
  'Chart',
]);

const files = process.argv.slice(2);
if (!files.length) {
  console.error('   użycie: node check_js_globals.js plik.html [...]');
  process.exit(2);
}

let bad = 0;
let checked = 0;

for (const file of files) {
  if (!fs.existsSync(file)) continue;
  const html = fs.readFileSync(file, 'utf8');

  // Tylko skrypty wbudowane w stronę (bez src=), bo tylko one są w tym pliku.
  const chunks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map(m => m[1]);
  if (!chunks.length) continue;
  const src = chunks.join('\n;\n');
  checked++;

  let free;
  try {
    free = globals(src);
  } catch (e) {
    console.log(`   ❌ ${file}: nie dało się sparsować — ${e.message}`);
    bad++;
    continue;
  }

  const missing = free
    .map(n => n.name)
    .filter(n => !ALLOWED.has(n))
    .filter((n, i, a) => a.indexOf(n) === i)
    .sort();

  if (missing.length) {
    for (const n of missing) {
      // Pokaż pierwsze użycie, żeby było gdzie szukać.
      const re = new RegExp('\\b' + n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b');
      const lineNo = src.split('\n').findIndex(l => re.test(l)) + 1;
      console.log(`   ${n}  — nigdzie nie zadeklarowane (pierwsze użycie: linia ${lineNo} wyciągniętego JS)`);
    }
    console.log(`   ❌ ${file}: ${missing.length} nazw, których nie ma — to ReferenceError przy wykonaniu`);
    bad++;
  } else {
    console.log(`   ✅ ${file} — brak nieistniejących nazw (${free.length} globalnych, wszystkie znane)`);
  }
}

if (!checked) {
  console.log('   (brak plików z wbudowanym JS — pomijam)');
}
process.exit(bad ? 1 : 0);
