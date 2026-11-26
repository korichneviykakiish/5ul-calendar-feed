const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../public/admin.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function page(replies, confirmResult = true) {
  const elements = Object.fromEntries(['form', 'secret', 'confirmText', 'resumeButton', 'resetButton', 'status']
    .map(id => [id, {value: '', textContent: '', disabled: false, focus() {}}]));
  elements.secret.value = '  synthetic-admin-key  ';
  const calls = [];
  let confirms = 0;
  const context = vm.createContext({
    document: {getElementById: id => elements[id]},
    confirm() { confirms++; return confirmResult; },
    fetch: async (url, options) => {
      calls.push({url, ...options});
      const next = replies.shift();
      if (next instanceof Error) throw next;
      if (!next) throw new Error('Unexpected request');
      const response = typeof next === 'function' ? await next() : next;
      return {status: response.code || 200, ok: !response.code || response.code < 400,
        json: async () => response.body};
    }
  });
  vm.runInContext(script, context);
  return {elements, calls, confirms: () => confirms,
    actions: () => calls.map(call => call.headers['X-Playtime-Action']),
    resume: () => elements.resumeButton.onclick(),
    reset: () => elements.form.onsubmit({preventDefault() {}})};
}
const ok = {body: {status: 'ok', sampled: true}};
const scheduled = {body: {status: 'schedule_status', job_id: 123, enabled: true}};
const ready = {body: {status: 'schedule_ready', enabled: true, job_id: 123}};

test('resume validates even an enabled schedule without reset confirmation and clears key', async () => {
  const p = page([ok, scheduled, ready]);
  await p.resume();
  assert.deepEqual(p.actions(), ['resume', 'schedule-status', 'schedule']);
  assert.equal(p.confirms(), 0);
  for (const request of p.calls) {
    assert.equal(request.url, '/api/playtime');
    assert.equal(request.method, 'POST');
    assert.equal(request.headers.Authorization, 'Bearer synthetic-admin-key');
    assert.equal(request.cache, 'no-store');
  }
  assert.equal(p.elements.secret.value, '');
  assert.match(p.elements.status.textContent, /Подсчёт возобновлён/);
});

test('resume enables only the known disabled schedule after successful login', async () => {
  const p = page([ok, {body: {...scheduled.body, enabled: false}},
    {body: {status: 'schedule_ready', enabled: true, job_id: 123}}]);
  await p.resume();
  assert.deepEqual(p.actions(), ['resume', 'schedule-status', 'schedule']);
  assert.match(p.elements.status.textContent, /Подсчёт возобновлён/);
});

test('schedule failure or missing schedule is not reported as resumed and never retries login', async () => {
  for (const response of [{code: 503, body: {status: 'check_failed'}},
    {body: {status: 'not_configured', enabled: false}}, new Error('offline')]) {
    const p = page([ok, response]);
    await p.resume();
    assert.deepEqual(p.actions(), ['resume', 'schedule-status']);
    assert.doesNotMatch(p.elements.status.textContent, /Подсчёт возобновлён/);
    assert.equal(p.elements.secret.value, '');
  }
});

test('an enabled schedule is not reported as restored when settings validation fails', async () => {
  const p = page([ok, scheduled, {code: 503, body: {status: 'check_failed'}}]);
  await p.resume();
  assert.deepEqual(p.actions(), ['resume', 'schedule-status', 'schedule']);
  assert.doesNotMatch(p.elements.status.textContent, /Подсчёт возобновлён/);
});

test('a changed schedule identity is not reported as restored', async () => {
  const p = page([ok, {body: {...scheduled.body, enabled: false}},
    {body: {status: 'schedule_ready', enabled: true, job_id: 456}}]);
  await p.resume();
  assert.doesNotMatch(p.elements.status.textContent, /Подсчёт возобновлён/);
});

test('login errors, cooldown and duplicate minute do not trigger retries or schedule mutation', async () => {
  for (const response of [{code: 401, body: {status: 'unauthorized'}},
    {code: 503, body: {status: 'login_required'}},
    {code: 503, body: {status: 'rate_limited', retry_at: Math.floor(Date.now()/1000)+3600}},
    {body: {status: 'already_checked'}}, new Error('offline')]) {
    const p = page([response]);
    await p.resume();
    assert.deepEqual(p.actions(), ['resume']);
    assert.doesNotMatch(p.elements.status.textContent, /Подсчёт возобновлён|synthetic-admin-key/);
    assert.equal(p.elements.secret.value, '');
    assert.equal(p.elements.resumeButton.disabled, false);
  }
});

test('reset requires both typed confirmation and dialog and never requests resume', async () => {
  const p = page([{body: {status: 'reset'}}]);
  await p.reset();
  assert.deepEqual(p.actions(), []);
  p.elements.confirmText.value = 'СБРОС';
  await p.reset();
  assert.deepEqual(p.actions(), ['reset-confirmed']);
  assert.equal(p.confirms(), 1);
  const cancelled = page([], false);
  cancelled.elements.confirmText.value = 'СБРОС';
  await cancelled.reset();
  assert.deepEqual(cancelled.actions(), []);
});

test('pending operation blocks both repeated clicks and reset', async () => {
  let release;
  const p = page([() => new Promise(resolve => {release = () => resolve(ok);}), scheduled, ready]);
  const first = p.resume();
  assert.equal(p.elements.resumeButton.disabled, true);
  assert.equal(p.elements.resetButton.disabled, true);
  await p.resume();
  p.elements.confirmText.value = 'СБРОС';
  await p.reset();
  release();
  await first;
  assert.deepEqual(p.actions(), ['resume', 'schedule-status', 'schedule']);
});

test('empty administrator key never sends requests', async () => {
  const p = page([]);
  p.elements.secret.value = ' ';
  await p.resume();
  assert.deepEqual(p.actions(), []);
});
