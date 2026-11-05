// Use installed Chrome when available, or Playwright's browser on other hosts.
const fs = require('fs');
const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const executablePath = process.env.HOTEL_BROWSER_EXECUTABLE || process.env.BG_BROWSER_EXECUTABLE || (fs.existsSync(chrome) ? chrome : '');
module.exports = {headless: true, ...(executablePath ? {executablePath} : {})};
