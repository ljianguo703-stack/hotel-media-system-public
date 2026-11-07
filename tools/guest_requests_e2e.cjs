/* Real HTTP against the disposable local fixture. */
const {chromium} = require('playwright-core');
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const info = JSON.parse(fs.readFileSync(path.join(root, 'artifacts/management/AD08/dev.json')));
const output = path.join(root, 'local/evidence/RQ02');
assert.equal(new URL(info.url).hostname, '127.0.0.1');
assert.equal(new URL(info.guest_url).hostname, '127.0.0.1');

(async () => {
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch(require('./browser_options.cjs'));
  const context = await browser.newContext({viewport: {width: 1440, height: 900}, reducedMotion: 'reduce'});
  const admin = await context.newPage();
  const errors = [];
  admin.on('pageerror', error => errors.push(error.message));
  const ui = code => admin.evaluate(code => Function('app', code)(Alpine.$data(document.body)), code);
  const settled = () => admin.waitForFunction(() => {
    const app = Alpine.$data(document.body);
    return app.guestDraft && !app.busy && !app.opsLoading;
  });
  try {
    await admin.goto(info.url + '/?view=operations&tab=guest_requests');
    await admin.locator('#username').fill('admin');
    await admin.locator('#password').fill('ad08-local-test');
    await admin.getByRole('button', {name: '登录后台', exact: true}).click();
    await admin.getByRole('tab', {name: '观众留言', exact: true}).click();
    await settled();
    assert.equal(await ui('return app.guestDraft.enabled'), false);
    await admin.locator('#guest-public-url').fill('https://wish.example.test');
    await admin.getByLabel('启用正片右上角间歇扫码提示').check();
    await admin.getByRole('button', {name: '保存留言设置', exact: true}).click();
    await admin.waitForFunction(() => {
      const app = Alpine.$data(document.body);
      return !app.busy && !app.opsLoading && !app.guestDirty && app.opsData.settings?.enabled;
    });
    const settings = await ui('return app.opsData.settings');
    const entry = new URL(settings.entry_url);
    const localEntry = info.guest_url + entry.pathname + entry.search;
    const catalog = await context.request.get(info.tv_url + '/api/v1/catalog', {headers: {'X-TV-Token': 'ad08-test-only'}});
    const prompt = (await catalog.json()).home.guest_request;
    assert.equal(prompt.enabled, true);
    assert.equal(prompt.first_delay_seconds, 180);
    assert.equal(prompt.interval_seconds, 1800);
    const qr = await context.request.get(info.tv_url + prompt.qr, {headers: {'X-TV-Token': 'ad08-test-only'}});
    assert(qr.ok());
    fs.writeFileSync(path.join(output, 'generated-qr.png'), await qr.body());

    const phone = await context.newPage();
    phone.on('pageerror', error => errors.push(error.message));
    await phone.setViewportSize({width: 390, height: 844});
    await phone.goto(localEntry);
    const message = '想看《星际穿越》 🎬\n<script>window.unexpectedGuestScript=true</script>';
    await phone.getByLabel('想看的影片或留言').fill(message);
    await phone.route('**/request', route => route.abort());
    await phone.getByRole('button', {name: '提交观影心愿', exact: true}).click();
    await phone.waitForFunction(() => document.getElementById('feedback').textContent.length > 0);
    assert.equal(await phone.getByLabel('想看的影片或留言').inputValue(), message);
    await phone.unroute('**/request');
    await phone.getByRole('button', {name: '提交观影心愿', exact: true}).click();
    await phone.getByRole('heading', {name: '留言已收到', exact: true}).waitFor();
    const receipt = await phone.locator('#receipt-id').innerText();
    assert(receipt.startsWith('gr-'));
    await ui('return app.loadOperations()');
    await settled();
    await admin.getByText(message, {exact: true}).waitFor();
    assert.equal(await admin.evaluate(() => window.unexpectedGuestScript), undefined);
    await admin.getByLabel('处理留言 ' + receipt, {exact: true}).selectOption('processing');
    await admin.waitForFunction(() => {
      const app = Alpine.$data(document.body);
      return !app.busy && !app.opsLoading && app.opsData.rows[0]?.status === 'processing';
    });
    await admin.reload();
    await settled();
    assert.equal(await ui('return app.opsData.rows[0].status'), 'processing');

    const native = await browser.newContext({javaScriptEnabled: false, viewport: {width: 390, height: 844}, reducedMotion: 'reduce'});
    const nativePage = await native.newPage();
    await nativePage.goto(localEntry);
    await nativePage.getByLabel('想看的影片或留言').fill('希望增加一部自然纪录片');
    await nativePage.getByRole('button', {name: '提交观影心愿', exact: true}).click();
    await nativePage.getByRole('heading', {name: '留言已收到', exact: true}).waitFor();
    await native.close();

    await admin.getByLabel('搜索观众留言').fill('自然纪录片');
    await admin.waitForFunction(() => {
      const app = Alpine.$data(document.body);
      return !app.opsLoading && app.opsData.total === 1 && app.opsData.rows[0]?.message.includes('自然纪录片');
    });
    await admin.getByLabel('搜索观众留言').fill('');
    await admin.waitForFunction(() => !Alpine.$data(document.body).opsLoading && Alpine.$data(document.body).opsData.total === 2);
    for (const page of [admin, phone]) {
      for (const [width, height] of [[390, 844], [1280, 720], [1440, 900]]) {
        await page.setViewportSize({width, height});
        assert(!await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), 'horizontal overflow');
      }
    }
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'e2e-results.json'), JSON.stringify({
      passed: true, receipt, localEntry, encodedQrUrl: settings.entry_url, checks: ['settings-save-reload', 'TV-public-config-and-QR',
        'network-failure-retains-input', 'real-submit-and-admin-list', 'plain-text-rendering',
        'status-update-reload', 'native-form-without-JavaScript', 'search', 'three-viewport-no-overflow'], errors
    }, null, 2));
    console.log('PASS guest requests real HTTP / settings / retry / queue / status / native form / three viewports');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
