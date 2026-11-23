import { PANE, quotaRows, meter } from './format.js';

// Deliberately uses only the engine's existing usage snapshot and UI APIs.
// No HTTP, credentials, shell, files, conversation messages, or model requests.
export function register(on) {
  let timer;
  let bandVisible = true;

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'quota-panel',
      description: '显示／隐藏用量详情（手动打开会占用右侧面板区）',
    });
    await $.command.register({
      name: 'quota-bar',
      description: '显示／隐藏输入框上方用量条，不占用右侧 Context',
    });
    const result = await next(e);
    timer?.cancel();
    timer = $.clock.every(60000, () => $.ui.invalidate('ui.render'));
    // v0.1's dock replaced Desktop's Context rail. On reload, close ONLY our
    // legacy pane and leave the native rail and every other plugin untouched.
    await $.ui.close({ id: PANE });
    return result;
  });

  on('session.end', async ($, e, next) => {
    timer?.cancel();
    timer = undefined;
    return next(e);
  });

  on('session.measure', async ($, e, next) => {
    $.ui.invalidate('ui.render');
    return next(e);
  });

  on('command.run', { command: 'quota-panel' }, async ($) => {
    const panes = await $.ui.panes();
    if (panes.some(pane => pane.id === PANE)) {
      await $.ui.close({ id: PANE });
    } else {
      await $.ui.open({ id: PANE, title: 'Usage · 账号用量', columns: 38, rows: 15 });
    }
    return {};
  });

  on('command.run', { command: 'quota-bar' }, async ($) => {
    bandVisible = !bandVisible;
    $.ui.invalidate('ui.render');
    return {};
  });

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const previous = await next(e);
    if (!bandVisible) return previous;
    const { Box, Text } = $.ui.resolve(e);
    let usage;
    try {
      usage = await $.session.usage();
    } catch {
      // Show unknown rather than interrupting input or inventing a quota.
    }
    const rows = quotaRows(usage, await $.clock.now());
    return Box({
      flexDirection: 'column',
      children: [
        ...(previous ? [previous] : []),
        ...rows.map(row => Text({
          color: row.color,
          children: [`${row.label} · ${row.text} · ${row.resetText}`],
        })),
        Text({ dimColor: true, children: ['账号额度 · 随 Claude 回复更新 · /quota-bar 隐藏'] }),
      ],
    });
  });

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== PANE) return next(e);
    const { Box, Text } = $.ui.resolve(e);
    let usage;
    let unavailable = false;
    try {
      usage = await $.session.usage();
    } catch {
      unavailable = true;
    }
    const rows = quotaRows(usage, await $.clock.now());
    const width = Math.max(8, Math.min(28, (e.props.bodyColumns || 30) - 4));
    return Box({
      flexDirection: 'column',
      padding: 1,
      children: [
        Text({ bold: true, children: ['账号额度 · 非 Context'] }),
        Text({ dimColor: true, children: ['聊天、Code 等共享的订阅额度'] }),
        ...rows.map(row => Box({
          flexDirection: 'column',
          marginTop: 1,
          children: [
            Text({ bold: true, children: [row.label] }),
            Text({ color: row.color, children: [row.text] }),
            Text({ color: row.color, children: [meter(row.fraction, width)] }),
            Text({ dimColor: true, children: [row.resetText] }),
          ],
        })),
        Text({ dimColor: true, children: [
          unavailable ? '读取暂不可用；下次更新时会重试。' : '随 Claude 的额度数据更新；闲置时可能滞后。',
        ] }),
        Text({ dimColor: true, children: ['/quota-panel 显示／隐藏'] }),
      ],
    });
  });
}
