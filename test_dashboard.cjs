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
    append(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    getContext: () => context2d,
    getBoundingClientRect: () => ({ left: 0, width: 800 }) });
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  get('port').value = 'all'; get('range').value = '0';
  const sandbox = vm.createContext({ document: { getElementById: get, createElement: element },
    window: { devicePixelRatio: 1 }, location: { protocol: 'http:' },
    Option: function(text, value) { return { text, value }; },
    ResizeObserver: class { observe() {} }, setInterval() {}, AbortSignal,
    fetch: async () => ({ ok: true, text: async () => '' }) });
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

test('failed source changes preserve data and source, and read error survives filtering', async () => {
  const {get, run} = await dashboard();
  run(`importedFile = { name:'history.txt', text: async () => ${JSON.stringify(JSON.stringify(rows[0]))} };`);
  await run("load('file', importedFile)");
  run("fetch = async () => { throw new Error('HTTP unavailable'); }");
  assert.equal(await run("load('server', importedFile, true)"), false);
  assert.equal(run('source'), 'file');
  assert.equal(get('auto').disabled, true);
  assert.equal(get('auto').checked, false);
  get('port').value = '10001'; run('render()');
  assert.equal(get('badge').textContent, '读取失败');
  assert.match(get('scope').textContent, /history.txt/);
  assert.equal(get('current').textContent, '7');
  run(`fetch = async () => ({ok:true, text:async () => ${JSON.stringify(JSON.stringify(rows[0]))}});`);
  assert.equal(await run("load('server', importedFile, true)"), true);
  assert.equal(get('auto').checked, true);
  assert.equal(get('auto').disabled, false);
  assert.equal(get('port').value, '10001');
  get('auto').checked = false; get('auto').listeners.change();
  assert.match(get('message').textContent, /自动刷新已暂停/);
});

test('legacy totals and failed samples remain missing; empty ranges show an explicit row', async () => {
  const {get, run} = await dashboard();
  run(`records = ${JSON.stringify([ {...rows[0], port_counts:undefined}, {...rows[0], timestamp:1794207060, count:null, port_counts:{10001:null,40002:null}, error:'ss_timeout'} ])}; updatePorts();`);
  get('port').value = '10001'; run('render()');
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
  run("resolveOld({ ok:true, text:async () => '' })"); await old;
  assert.equal(run('source'), 'file');
  assert.equal(run('records.length'), 1);
});
