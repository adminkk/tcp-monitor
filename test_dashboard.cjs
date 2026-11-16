// Run with: node --test test_dashboard.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const script = fs.readFileSync(`${__dirname}/index.html`, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

async function dashboard() {
  const elements = new Map();
  const context2d = new Proxy({}, { get: () => () => {} });
  const element = () => ({ value: '', textContent: '', checked: true, style: {}, children: [],
    clientWidth: 800, clientHeight: 350, offsetWidth: 240, listeners: {},
    captured: new Set(),
    setPointerCapture(id) { this.captured.add(id); },
    hasPointerCapture(id) { return this.captured.has(id); },
    releasePointerCapture(id) { this.captured.delete(id); },
    append(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    getContext: () => context2d,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 350 }) });
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  get('port').value = 'all'; get('range').value = '0';
  const sandbox = vm.createContext({ document: { getElementById: get, createElement: element,
      addEventListener: (name, fn) => get('__document').addEventListener(name, fn) },
    window: { devicePixelRatio: 1 }, location: { protocol: 'http:' },
    Option: function(text, value) { return { text, value }; },
    ResizeObserver: class { observe() {} }, setInterval() {}, AbortSignal,
    fetch: async () => ({ ok: true, text: async () => JSON.stringify({records:[], latest:null, ports:[]}) }) });
  vm.runInContext(script, sandbox);
  await new Promise(resolve => setImmediate(resolve));
  return { get, run: code => vm.runInContext(code, sandbox) };
}
const rows = [
  {timestamp: 1794207000, state:'established', ports:[10001,40002], count:8, port_counts:{10001:7,40002:1}},
  {timestamp: 1794207060, state:'established', ports:[40002], count:2, port_counts:{40002:2}},
];

test('single-port numbers, table and tooltip agree; unmonitored ports remain gaps', async () => {
  const {get, run} = await dashboard();
  run(`records = parseData(${JSON.stringify(JSON.stringify(rows[0])+'\n'+JSON.stringify(rows[1]))}).records; updatePorts();`);
  get('port').value = '10001'; run('render()');
  assert.equal(get('rows').children[1].children[1].textContent, '7');
  assert.equal(get('rows').children[1].children[3].textContent, '10001');
  assert.equal(get('rows').children[0].children[3].textContent, '10001（该次未监控）');
  assert.equal(get('current').textContent, '—');
  assert.equal(get('average').textContent, '7.0');
  get('chart').listeners.mousemove({clientX:58});
  assert.match(get('tooltip').textContent, /连接数：7\n统计端口：10001\n该次采集范围：10001, 40002/);
  run('render()'); assert.equal(get('tooltip').style.display, 'none');
  get('port').value = 'all'; run('render()');
  assert.equal(get('rows').children[1].children[3].textContent, '10001, 40002');
  assert.equal(run('visible[1].breakBefore'), true);
});

const brushRows = Array.from({length:61}, (_, i) => ({timestamp:1800000000 + i * 60,
  state:'established', ports:[40002], count:i, port_counts:{40002:i}}));
const pointer = (clientX, extras = {}) => ({clientX, clientY:150, button:0, pointerId:1, isPrimary:true, ...extras});
async function offlineBrush() {
  const app = await dashboard();
  await app.run(`load('file', {name:'brush.txt', text:async () => ${JSON.stringify(brushRows.map(r => JSON.stringify(r)).join('\n'))}})`);
  return app;
}

test('drag selects an offline interval, updates inputs and statistics, and restores the original view after nested reverse drags', async () => {
  const {get, run} = await offlineBrush(), chart = get('chart');
  chart.listeners.pointerdown(pointer(239));
  chart.listeners.pointermove(pointer(601));
  assert.equal(get('brush').style.display, 'block');
  assert.equal(get('brush').style.width, '362px');
  assert.match(get('chart-help').textContent, /松开应用/);
  chart.listeners.mousemove(pointer(400));
  assert.equal(get('tooltip').style.display, 'none');
  await chart.listeners.pointerup(pointer(601));
  assert.equal(get('range').value, 'custom');
  assert.equal(run('customBounds[0]'), brushRows[15].timestamp);
  assert.equal(run('customBounds[1]'), brushRows[45].timestamp);
  assert.equal(get('start').value, run('localInput(customBounds[0])'));
  assert.equal(get('end').value, run('localInput(customBounds[1])'));
  assert.equal(get('samples').textContent, '0 / 31');
  assert.equal(get('average').textContent, '30.0');
  assert.equal(get('brush').style.display, 'none');
  assert.equal(chart.hasPointerCapture(1), false);
  assert.equal(get('zoom-reset').hidden, false);
  chart.listeners.pointerdown(pointer(601));
  await chart.listeners.pointerup(pointer(239));
  assert.ok(run('customBounds[0]') > brushRows[15].timestamp);
  await get('zoom-reset').listeners.click();
  assert.equal(get('range').value, '0');
  assert.equal(get('samples').textContent, '0 / 61');
  assert.equal(get('zoom-reset').hidden, true);
});

test('clicks, outside starts, secondary buttons, cancellation and redraw never apply a selection', async () => {
  const {get, run} = await offlineBrush(), chart = get('chart');
  for (const event of [pointer(20), pointer(200, {clientY:10}), pointer(200, {button:2}), pointer(200, {isPrimary:false})]) {
    chart.listeners.pointerdown(event);
    assert.equal(run('selection'), null);
  }
  chart.listeners.pointerdown(pointer(239)); await chart.listeners.pointerup(pointer(242));
  assert.equal(get('range').value, '0');
  for (const cancel of [() => get('__document').listeners.keydown({key:'Escape'}),
    () => chart.listeners.pointercancel(pointer(300)), () => chart.listeners.lostpointercapture(pointer(300)),
    () => run('draw()')]) {
    chart.listeners.pointerdown(pointer(239)); chart.listeners.pointermove(pointer(601)); cancel();
    await chart.listeners.pointerup(pointer(601));
    assert.equal(run('selection'), null);
    assert.equal(get('brush').style.display, 'none');
    assert.equal(get('range').value, '0');
  }
  // Captured pointers may leave the canvas; the selected interval stays within its axes.
  chart.listeners.pointerdown(pointer(601)); await chart.listeners.pointerup(pointer(-200));
  assert.equal(run('customBounds[0]'), brushRows[0].timestamp);
});

test('online selection requests the chosen interval and restores a prior custom range', async () => {
  const {get, run} = await dashboard();
  run(`let requested=[]; fetch=async url => {requested.push(url); return {ok:true,
    text:async () => ${JSON.stringify(JSON.stringify({records:brushRows, latest:brushRows[60], ports:[40002]}))}}; };`);
  await run(`load()`);
  run(`customBounds=[${brushRows[0].timestamp},${brushRows[60].timestamp}]; $('range').value='custom';`);
  await run('reloadRange()');
  const chart = get('chart');
  chart.listeners.pointerdown(pointer(601)); await chart.listeners.pointerup(pointer(239));
  const url = new URL(run('requested[2]'), 'http://localhost');
  assert.equal(Number(url.searchParams.get('start')), brushRows[15].timestamp);
  assert.equal(Number(url.searchParams.get('end')), brushRows[45].timestamp);
  assert.equal(get('samples').textContent, '0 / 31');
  await get('zoom-reset').listeners.click();
  assert.equal(get('range').value, 'custom');
  assert.equal(run('customBounds[0]'), brushRows[0].timestamp);
  assert.equal(run('customBounds[1]'), brushRows[60].timestamp);
  assert.equal(get('start').value, run('localInput(customBounds[0])'));
  assert.equal(get('samples').textContent, '0 / 61');
});

test('failed source changes preserve data and source, and read error survives filtering', async () => {
  const {get, run} = await dashboard();
  run(`importedFile = { name:'history.txt', text: async () => ${JSON.stringify(JSON.stringify(rows[0]))} };`);
  await run("load('file', importedFile)");
  run("fetch = async () => { throw new Error('HTTP unavailable'); }");
  assert.equal(await run("load('server', importedFile, true)"), false);
  assert.equal(run('source'), 'file');
  assert.equal(get('auto').disabled, true);
  assert.equal(get('auto').checked, false);
  assert.equal(get('online').hidden, false);
  get('port').value = '10001'; run('render()');
  assert.equal(get('badge').textContent, '读取失败');
  assert.match(get('scope').textContent, /history.txt/);
  assert.equal(get('current').textContent, '7');
  run(`fetch = async () => ({ok:true, text:async () => ${JSON.stringify(JSON.stringify({records:[rows[0]], latest:rows[0], ports:rows[0].ports}))}});`);
  assert.equal(await run("load('server', importedFile, true)"), true);
  assert.equal(get('auto').checked, true);
  assert.equal(get('auto').disabled, false);
  assert.equal(get('online').hidden, true);
  assert.equal(get('port').value, '10001');
  get('auto').checked = false; get('auto').listeners.change();
  assert.match(get('message').textContent, /自动刷新已暂停/);
});

test('legacy totals and failed samples remain missing; empty ranges show an explicit row', async () => {
  const {get, run} = await dashboard();
  run(`records = ${JSON.stringify([ {...rows[0], port_counts:undefined}, {...rows[0], timestamp:1794207060, count:null, port_counts:{10001:null,40002:null}, error:'ss_timeout'} ])}; updatePorts();`);
  get('port').value = '10001'; run("source='file'; importedFile={name:'legacy.txt'}; render()");
  assert.equal(get('samples').textContent, '2 / 2');
  assert.match(get('legacy').textContent, /无法拆分/);
  get('chart').listeners.mousemove({clientX:782});
  assert.match(get('tooltip').textContent, /ss_timeout/);
  run("customBounds=[1,2]; $('range').value='custom'; render()");
  assert.equal(get('rows').children[0].children[0].textContent, '当前范围暂无记录');
  assert.equal(get('tooltip').style.display, 'none');
});

test('a late request cannot replace a newer import', async () => {
  const {run} = await dashboard();
  run('let resolveOld; fetch = () => new Promise(resolve => { resolveOld = resolve; });');
  const old = run("load('server')");
  await run(`load('file', { name:'new.txt', text:async () => ${JSON.stringify(JSON.stringify(rows[0]))}})`);
  run("resolveOld({ ok:true, text:async () => JSON.stringify({records:[], latest:null, ports:[]}) })"); await old;
  assert.equal(run('source'), 'file');
  assert.equal(run('records.length'), 1);
});

test('online ranges query only the requested interval; global latest and ports survive an empty interval', async () => {
  const {get, run} = await dashboard();
  const latest = {...rows[0], timestamp: Math.floor(Date.now() / 1000)};
  run(`let requested=[]; fetch=async url => { requested.push(url); return {ok:true,
    text:async () => ${JSON.stringify(JSON.stringify({records:[], latest, ports:[10001,40002]}))}}; };`);
  get('range').value = '1';
  await get('range').listeners.change();
  const url = new URL(run('requested[0]'), 'http://localhost/');
  assert.equal(url.pathname, '/api/history');
  assert.equal(Number(url.searchParams.get('end')) - Number(url.searchParams.get('start')), 3600);
  assert.equal(get('badge').textContent, '正在采集');
  assert.equal(get('rows').children[0].children[0].textContent, '当前范围暂无记录');
  assert.deepEqual(Array.from(get('port').children, p => p.value), ['all','10001','40002']);
  get('port').value = '10001'; get('port').listeners.change();
  assert.equal(run('requested.length'), 1);
  get('start').value = '2026-11-01T01:00:00'; get('end').value = '2026-11-01T02:00:00';
  get('apply').listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  const custom = new URL(run('requested[1]'), 'http://localhost/');
  assert.equal(Number(custom.searchParams.get('start')), new Date(get('start').value).getTime() / 1000);
  get('range').value = '0'; await get('range').listeners.change();
  assert.equal(run('requested[2]'), './api/history');
});

test('a late range response cannot overwrite a newer range; invalid responses keep prior data', async () => {
  const {get, run} = await dashboard();
  run('let pending=[]; fetch=() => new Promise(resolve => pending.push(resolve));');
  get('range').value = '1'; const old = get('range').listeners.change();
  get('range').value = '0'; const current = get('range').listeners.change();
  run(`pending[1]({ok:true, text:async () => ${JSON.stringify(JSON.stringify({records:rows, latest:rows[1], ports:[10001,40002]}))}})`);
  await current;
  run('pending[0]({ok:true, text:async () => JSON.stringify({records:[],latest:null,ports:[]})})'); await old;
  assert.equal(run('records.length'), 2);
  run("fetch=async () => ({ok:true, text:async () => 'broken'})");
  assert.equal(await run('load()'), false);
  assert.equal(run('records.length'), 2);
  assert.equal(get('badge').textContent, '读取失败');
});

test('SQLite imports upload a binary snapshot, show its history, and can return to the server', async () => {
  const {get, run} = await dashboard();
  assert.equal(get('online').hidden, true);
  run(`let requests=[]; let databaseFile={name:'snapshot.sqlite3',size:4096};
    fetch=async (url, options) => {requests.push({url,options}); return {ok:true,
    text:async () => JSON.stringify(url === './api/import' ? {records:${JSON.stringify(rows)}} :
      {records:[],latest:null,ports:[]})}; };`);
  assert.equal(await run("load('file', databaseFile)"), true);
  assert.equal(run('requests[0].url'), './api/import');
  assert.equal(run('requests[0].options.method'), 'POST');
  assert.equal(run("requests[0].options.headers['Content-Type']"), 'application/vnd.sqlite3');
  assert.equal(run('requests[0].options.body === databaseFile'), true);
  assert.equal(run('source'), 'file');
  assert.equal(get('auto').disabled, true);
  assert.equal(get('auto').checked, false);
  assert.equal(get('online').hidden, false);
  assert.equal(get('samples').textContent, '0 / 2');
  assert.match(get('scope').textContent, /snapshot.sqlite3/);
  assert.match(get('message').textContent, /不覆盖服务器数据/);
  await get('online').listeners.click();
  assert.equal(run('source'), 'server');
  assert.equal(get('auto').disabled, false);
  assert.equal(get('auto').checked, true);
  assert.equal(get('online').hidden, true);
});

test('invalid SQLite imports preserve existing data and explain file and size restrictions', async () => {
  const {get, run} = await offlineBrush();
  run("fetch=async () => ({ok:false,status:400,text:async () => JSON.stringify({error:'数据库格式不兼容'})})");
  assert.equal(await run("load('file', {name:'bad.db',size:40})"), false);
  assert.equal(run('records.length'), 61);
  assert.equal(run('importedFile.name'), 'brush.txt');
  assert.match(get('message').textContent, /数据库格式不兼容/);
  run("fetch=async () => ({ok:true,text:async () => JSON.stringify({records:[{}]})})");
  assert.equal(await run("load('file', {name:'invalid.sqlite',size:4096})"), false);
  assert.match(get('message').textContent, /无效的采样记录/);
  assert.equal(await run("load('file', {name:'huge.sqlite3',size:257*1024*1024})"), false);
  assert.match(get('message').textContent, /256 MB/);
  run("location.protocol='file:'");
  assert.equal(await run("load('file', {name:'snapshot.sqlite3',size:4096})"), false);
  assert.match(get('message').textContent, /通过监控服务网页/);
  assert.equal(run('records.length'), 61);
  // Legacy text still works when index.html is opened directly from disk.
  assert.equal(await run('load()'), true);
  assert.equal(run('records.length'), 61);
  assert.equal(get('online').hidden, true);
});

test('a delayed SQLite upload cannot replace a newer import', async () => {
  const {run} = await dashboard();
  run('let finishUpload; fetch=() => new Promise(resolve => {finishUpload=resolve});');
  const pending = run("load('file',{name:'old.sqlite3',size:4096})");
  await run(`load('file',{name:'new.txt',text:async () => ${JSON.stringify(JSON.stringify(rows[0]))}})`);
  run(`finishUpload({ok:true,text:async () => JSON.stringify({records:${JSON.stringify(rows)}})})`);
  await pending;
  assert.equal(run('importedFile.name'), 'new.txt');
  assert.equal(run('records.length'), 1);
});
