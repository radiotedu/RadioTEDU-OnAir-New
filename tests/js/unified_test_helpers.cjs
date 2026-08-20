const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..', '..');
const html = fs.readFileSync(path.join(root, 'app', 'static', 'onair', 'index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(root, 'app', 'static', 'onair', 'app.js'), 'utf8');
const guestRoomJs = fs.readFileSync(path.join(root, 'app', 'static', 'onair', 'guest-room.js'), 'utf8');

function section(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  if (start < 0) throw new Error(`Missing start marker: ${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  if (end < 0) throw new Error(`Missing end marker after ${startMarker}: ${endMarker}`);
  return source.slice(start, end);
}

function documentIds() {
  return new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]));
}

module.exports = { root, html, appJs, guestRoomJs, section, documentIds };
