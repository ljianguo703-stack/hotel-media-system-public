const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function fixture() {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../guest-requests.js'), 'utf8'), context);
  const app = vm.runInContext('guestRequestsUi()', context);
  app.data = {revision: 12};
  return app;
}

const settings = {enabled: true, public_url: 'https://wish.example.test', prompt: '扫码留言',
  first_delay_seconds: 180, interval_seconds: 1800, visible_seconds: 20};

test('entry field accepts bare domains and IP addresses through the save workflow', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../management.html'), 'utf8');
  const field = html.match(/<input\b[^>]*id="guest-public-url"[^>]*>/)[0];
  assert.match(field, /type="text"/);
  assert.match(field, /inputmode="url"/);
  for (const address of ['www.test.com', '203.0.113.10:8080', 'http://203.0.113.10:8080']) {
    const app = fixture();
    app.guestLoaded({settings});
    app.guestDraft.public_url = address;
    let payload;
    app.action = async (_route, body) => {payload = body; return {saved: true};};
    app.loadOperations = async () => {};
    await app.guestSettingsSave();
    assert.equal(payload.settings.public_url, address);
  }
});

test('configured entry starts collapsed and an unfinished draft survives reload', () => {
  const app = fixture();
  app.guestLoaded({settings});
  assert.equal(app.guestSettingsOpen, false);
  assert.equal(app.guestRevision, 12);
  assert.equal(app.guestDraft.first_minutes, 3);
  app.guestDraft.prompt = '编辑中的文字';
  app.guestDirty = true;
  app.guestLoaded({settings});
  assert.equal(app.guestDraft.prompt, '编辑中的文字');
});

test('unconfigured entry opens its setup and preserves explicit fold changes', () => {
  const app = fixture();
  app.guestLoaded({settings: {...settings, enabled: false, public_url: ''}});
  assert.equal(app.guestSettingsOpen, true);
  app.guestSettingsOpen = false;
  app.guestLoaded({settings});
  assert.equal(app.guestSettingsOpen, false);
});

test('save converts minutes and uses the draft revision before closing setup', async () => {
  const app = fixture();
  app.guestLoaded({settings});
  app.guestDirty = true;
  app.guestSettingsOpen = true;
  let payload;
  app.action = async (_route, body) => {payload = body; return {saved: true};};
  app.loadOperations = async () => {};
  await app.guestSettingsSave();
  assert.equal(payload._revision, 12);
  assert.equal(payload.settings.first_delay_seconds, 180);
  assert.equal(payload.settings.interval_seconds, 1800);
  assert.equal(app.guestDirty, false);
  assert.equal(app.guestSettingsOpen, false);
});
