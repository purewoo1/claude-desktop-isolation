import { expect, mock, test } from 'claude-code/testing';
import { PANE, quotaRows, windowRow, meter } from '../hooks/format.js';

const NOW = Date.parse('2026-11-01T06:00:00Z');
const RESET = '2026-11-01T08:30:00Z';

test('missing readings are unknown, not zero', () => {
  const rows = quotaRows(undefined, NOW);
  expect(rows.length).toBe(2);
  expect(rows[0].text).toBe('暂未获取');
  expect(rows[0].fraction).toBe(null);
});

test('zero is a genuine reading, unlike absent data', () => {
  expect(windowRow('five_hour', { percentUsed: 0, resetsAt: RESET }, NOW).text).toBe('已用 0%');
});

test('percent is already a percentage; do not multiply by 100', () => {
  const row = windowRow('five_hour', { percentUsed: 23.5, resetsAt: RESET }, NOW);
  expect(row.text).toBe('已用 23.5%');
  expect(row.resetText).toBe('2小时 30分后重置');
});

test('expired data is labeled stale, never reset locally to zero', () => {
  const row = windowRow('five_hour', { percentUsed: 92, resetsAt: RESET }, Date.parse(RESET));
  expect(row.text).toBe('上次记录 92%');
  expect(row.color).toBe('gray');
});

test('invalid percentages are unknown and exceedance stays visible', () => {
  for (const percentUsed of [NaN, Infinity, -1, null, '7']) {
    expect(windowRow('five_hour', { percentUsed }, NOW).text).toBe('暂未获取');
  }
  const row = windowRow('spend_limit', { percentUsed: 105 }, NOW);
  expect(row.text).toBe('已用 105%');
  expect(row.fraction).toBe(1);
});

test('invalid reset timestamps do not invent a countdown', () => {
  expect(windowRow('seven_day', { percentUsed: 8, resetsAt: 'bad' }, NOW).resetText).toBe('重置时间暂未提供');
});

test('invalid current time never produces a bare reset suffix', () => {
  for (const now of [undefined, NaN, Infinity, {}, Promise.resolve(NOW)]) {
    const row = windowRow('five_hour', { percentUsed: 62, resetsAt: RESET }, now);
    expect(row.text).toBe('已用 62%');
    expect(row.resetText).toBe('当前时间暂不可用');
  }
});

test('additional server-reported model limits are retained', () => {
  const rows = quotaRows({ rateLimits: [{ kind: 'seven_day_opus', percentUsed: 80 }] }, NOW);
  expect(rows.length).toBe(3);
  expect(rows[2].label).toBe('Opus 每周额度');
  expect(rows[2].color).toBe('yellow');
  expect(meter(0.5, 8)).toBe('━━━━────');
});

const pane = {
  plugin: 'private-usage-panel', component: 'Pane', requestId: PANE,
  viewport: { columns: 150, rows: 45 },
  props: {
    title: 'Usage · 账号用量', isFocused: false, bodyColumns: 38,
    placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {},
  },
} as const;

test('Desktop and terminal panes draw actual quota values', async ($, on) => {
  mock.clock(on);
  on('session.usage', () => ({ value: {
    context: { tokens: 230600, window: 1000000, percent: 23 },
    rateLimits: [{ kind: 'five_hour', percentUsed: 61, resetsAt: RESET }, { kind: 'seven_day', percentUsed: 42 }],
  } }));
  for (const surface of ['desktop', 'terminal'] as const) {
    const ui = await $.ui.mount({ ...pane, surface });
    expect(await ui.find({ type: 'Text', text: '已用 61%' })).toBeDefined();
    expect(await ui.find({ type: 'Text', text: '已用 42%' })).toBeDefined();
    expect(await ui.find({ type: 'Text', text: '已用 23%' })).toBeUndefined();
    await ui.unmount();
  }
});

test('unavailable readings still render a safe unknown state', async ($, on) => {
  mock.clock(on);
  on('session.usage', () => ({ value: { context: {}, rateLimits: [] } }));
  const ui = await $.ui.mount({ ...pane, surface: 'desktop', props: { ...pane.props, bodyColumns: 12, placement: 'inline' } });
  expect(await ui.find({ type: 'Text', text: '暂未获取' })).toBeDefined();
  await ui.unmount();
});

test('the command toggles only this pane and never steals focus', async ($, on) => {
  let open: any;
  let closed: any;
  let panes: any[] = [];
  on('ui.panes', () => ({ value: panes }));
  on('ui.open', ($, e) => { open = e; return { value: undefined }; });
  on('ui.close', ($, e) => { closed = e; return { value: undefined }; });
  await $.command.run({ command: 'quota-panel', args: '' });
  expect(open.id).toBe(PANE);
  expect(open.focus).toBeUndefined();
  panes = [{ id: PANE }];
  await $.command.run({ command: 'quota-panel', args: '' });
  expect(closed.id).toBe(PANE);
});

test('session start closes only the legacy pane and never opens a dock', async ($, on) => {
  const clock = mock.clock(on);
  const registered: string[] = [];
  const closed: string[] = [];
  let opened = false;
  let redraws = 0;
  on('command.register', ($, e) => { registered.push(e.name); return { value: undefined }; });
  on('ui.open', () => { opened = true; return { value: undefined }; });
  on('ui.close', ($, e) => { closed.push(e.id); return { value: undefined }; });
  on('ui.invalidate', () => { redraws++; return { value: undefined }; });
  on('session.start', () => ({ cwd: '/offline-test' }));
  on('session.end', () => ({ sessionId: 'offline-test' }));
  await $.session.start({ cwd: '/offline-test' });
  expect(registered).toEqual(['quota-panel', 'quota-bar']);
  expect(closed).toEqual([PANE]);
  expect(opened).toBe(false);
  await clock.advance(60000);
  expect(redraws).toBe(1);
  await $.session.end({ reason: 'other' });
  await clock.advance(120000);
  expect(redraws).toBe(1);
});

test('a failed usage read does not break Desktop rendering', async ($, on) => {
  mock.clock(on);
  on('session.usage', () => ({ deny: 'offline test: unavailable' }));
  const ui = await $.ui.mount({ ...pane, surface: 'desktop' });
  expect(await ui.find({ type: 'Text', text: '读取暂不可用；下次更新时会重试。' })).toBeDefined();
  expect(await ui.find({ type: 'Text', text: '暂未获取' })).toBeDefined();
  await ui.unmount();
});

const band = {
  plugin: 'private-usage-panel', component: 'AbovePrompt', requestId: 'above-prompt',
  viewport: { columns: 150, rows: 45 },
  props: {
    hasSurvey: false, isWorking: false, maxRows: 8, bodyColumns: 90,
    scroll: { offset: 0, bodyRows: 8 }, view: {},
  },
} as const;

test('compact Desktop usage band renders without opening any pane', async ($, on) => {
  mock.clock(on);
  on('ui.render', () => ({ type: 'Box', props: { flexDirection: 'column' }, children: [] }));
  let opened = false;
  on('ui.open', () => { opened = true; return { value: undefined }; });
  on('session.usage', () => ({ value: {
    context: { percent: 23 },
    rateLimits: [{ kind: 'five_hour', percentUsed: 61 }, { kind: 'seven_day', percentUsed: 42 }],
  } }));
  const ui = await $.ui.mount({ ...band, surface: 'desktop' });
  expect(await ui.find({ type: 'Text', text: /5 小时额度 · 已用 61%/ })).toBeDefined();
  expect(await ui.find({ type: 'Text', text: /每周额度 · 已用 42%/ })).toBeDefined();
  expect(opened).toBe(false);
  await ui.unmount();
});

test('the band preserves existing UI and can be hidden independently', async ($, on) => {
  mock.clock(on);
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['existing band'] }));
  on('session.usage', () => ({ value: { context: {}, rateLimits: [] } }));
  const ui = await $.ui.mount({ ...band, surface: 'desktop' });
  expect(await ui.find({ type: 'Text', text: 'existing band' })).toBeDefined();
  expect(await ui.find({ type: 'Text', text: /5 小时额度/ })).toBeDefined();
  await $.command.run({ command: 'quota-bar', args: '' });
  expect(await ui.find({ type: 'Text', text: 'existing band' })).toBeDefined();
  expect(await ui.find({ type: 'Text', text: /5 小时额度/ })).toBeUndefined();
  await ui.unmount();
});

test('Desktop band awaits the real async clock API and updates the countdown', async ($, on) => {
  const clock = mock.clock(on, { now: NOW });
  on('ui.render', () => ({ type: 'Box', props: { flexDirection: 'column' }, children: [] }));
  on('command.register', () => ({ value: undefined }));
  on('ui.close', () => ({ value: undefined }));
  on('session.start', () => ({ cwd: '/offline-test' }));
  on('session.end', () => ({ sessionId: 'offline-test' }));
  on('session.usage', () => ({ value: {
    context: {}, rateLimits: [{ kind: 'five_hour', percentUsed: 62, resetsAt: RESET }],
  } }));
  await $.session.start({ cwd: '/offline-test' });
  const ui = await $.ui.mount({ ...band, surface: 'desktop' });
  expect(await ui.find({ type: 'Text', text: '5 小时额度 · 已用 62% · 2小时 30分后重置' })).toBeDefined();
  await clock.advance(60000);
  expect(await ui.find({ type: 'Text', text: '5 小时额度 · 已用 62% · 2小时 29分后重置' })).toBeDefined();
  await ui.unmount();
  await $.session.end({ reason: 'other' });
});

test('Desktop detail pane also awaits the async clock API', async ($, on) => {
  mock.clock(on, { now: NOW });
  on('session.usage', () => ({ value: {
    context: {}, rateLimits: [{ kind: 'five_hour', percentUsed: 62, resetsAt: RESET }],
  } }));
  const ui = await $.ui.mount({ ...pane, surface: 'desktop' });
  expect(await ui.find({ type: 'Text', text: '2小时 30分后重置' })).toBeDefined();
  await ui.unmount();
});
