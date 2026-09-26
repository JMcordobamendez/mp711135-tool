// Syntax-checks the component logic embedded in MP711135.dc.html, which the
// browser otherwise only compiles at runtime (a typo there = blank page).
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'MP711135.dc.html'), 'utf8');
const m = html.match(/<script type="text\/x-dc" data-dc-script[^>]*>([\s\S]*?)<\/script>/);
if (!m) { console.error('component <script data-dc-script> not found'); process.exit(1); }
try {
  new vm.Script('class DCLogic {}\n' + m[1], { filename: 'MP711135.dc.html <script>' });
} catch (e) {
  console.error(e.stack || String(e));
  process.exit(1);
}
console.log('frontend component script: syntax OK');
