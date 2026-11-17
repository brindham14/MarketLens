// Fails if any inline <script> in index.html has a syntax error, or the build label is missing.
const fs = require('fs');
const html = fs.readFileSync('index.html', 'utf8');
const re = /<script(?![^>]*src)[^>]*>([\s\S]*?)<\/script>/g;
let m, n = 0, bad = 0;
while ((m = re.exec(html))) {
  n++;
  try { new Function(m[1]); } catch (e) { bad++; console.error('Script #' + n + ' has a syntax error: ' + e.message); }
}
const label = /id="mlVer"[^>]*>build (\d+)</.exec(html);
if (!label) { bad++; console.error('Build label not found'); }
console.log(n + ' inline scripts checked, build ' + (label ? label[1] : '?') + ', problems: ' + bad);
process.exit(bad ? 1 : 0);
