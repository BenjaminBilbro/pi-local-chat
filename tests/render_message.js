/**
 * render_message.js
 *
 * Takes a JSON array of assistant messages on stdin, renders each one
 * using the existing history.js functions, and prints the HTML to stdout.
 *
 * Usage:
 *   echo '[{"role":"assistant","content":[...]}]' | node tests/render_message.js
 */

import { JSDOM } from 'jsdom';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Create DOM
const dom = new JSDOM(`<!DOCTYPE html><body><div id="messages"></div></body>`, {
  url: 'http://localhost',
  runScripts: 'dangerously',
  resources: 'usable',
});

const { window } = dom;
const { document } = window;

// Load marked via a script tag in the DOM
const markedSrc = readFileSync(join(__dirname, '../static/marked.min.js'), 'utf-8');
const markedScript = document.createElement('script');
markedScript.textContent = markedSrc;
document.head.appendChild(markedScript);

// marked UMD sets itself on window.marked when module is not available
// But in jsdom it may set module.exports instead. Copy to window.
if (!window.marked && window.module && window.module.exports) {
  window.marked = window.module.exports;
}

window.marked.setOptions({ breaks: true, gfm: true });

// Load history.js — strip 'export' so functions become global
const historySrc = readFileSync(join(__dirname, '../static/history.js'), 'utf-8');
const historyCode = historySrc.replace(/^export function /gm, 'function ');

const historyScript = document.createElement('script');
historyScript.textContent = historyCode;
document.head.appendChild(historyScript);

// Now the functions should be on window
const { renderAssistantMessage } = window;
if (typeof renderAssistantMessage !== 'function') {
  console.error('renderAssistantMessage not found on window after loading history.js');
  process.exit(1);
}

// Read messages from stdin
let stdin = '';
for await (const chunk of process.stdin) {
  stdin += chunk;
}

const messages = JSON.parse(stdin);

for (const msg of messages) {
  if (msg.role !== 'assistant') continue;

  // Create a fresh container for each message
  const tempDiv = document.createElement('div');
  tempDiv.id = 'messages';

  renderAssistantMessage(msg, tempDiv);

  // Output the inner HTML
  let html = tempDiv.innerHTML;
  console.log(`---MESSAGE---`);
  console.log(html);
  console.log(`---END---`);
}
