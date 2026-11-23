// Pure formatting only. Missing/stale readings must never look like 0% usage.
export const PANE = 'private-usage-panel';

const labels = {
  five_hour: '5 小时额度',
  seven_day: '每周额度',
  seven_day_opus: 'Opus 每周额度',
  seven_day_sonnet: 'Sonnet 每周额度',
  spend_limit: '消费限额',
};

export function windowRow(kind, reading, now) {
  const clockValid = typeof now === 'number' && Number.isFinite(now);
  const valid = reading && typeof reading.percentUsed === 'number'
    && Number.isFinite(reading.percentUsed) && reading.percentUsed >= 0;
  const reset = typeof reading?.resetsAt === 'string' ? Date.parse(reading.resetsAt) : NaN;
  const stale = valid && clockValid && Number.isFinite(reset) && reset <= now;
  const percent = valid ? Math.round(reading.percentUsed * 10) / 10 : null;
  let resetText = '重置时间暂未提供';
  if (Number.isFinite(reset)) {
    if (!clockValid) {
      resetText = '当前时间暂不可用';
    } else if (reset <= now) {
      resetText = '已到重置时间，等待新的额度数据';
    } else {
      const minutes = Math.ceil((reset - now) / 60000);
      const days = Math.floor(minutes / 1440);
      const hours = Math.floor((minutes % 1440) / 60);
      const mins = minutes % 60;
      const parts = [days ? `${days}天` : '', hours ? `${hours}小时` : '', mins ? `${mins}分` : ''].filter(Boolean);
      resetText = `${parts.join(' ')}后重置`;
    }
  }
  return {
    label: labels[kind] || String(kind).slice(0, 64),
    percent,
    stale,
    text: valid ? `${stale ? '上次记录' : '已用'} ${percent}%` : '暂未获取',
    resetText: valid ? resetText : '等待 Claude 提供额度数据',
    color: !valid || stale ? 'gray' : percent >= 90 ? 'red' : percent >= 75 ? 'yellow' : 'cyan',
    fraction: valid ? Math.max(0, Math.min(1, percent / 100)) : null,
  };
}

export function quotaRows(usage, now) {
  const windows = Array.isArray(usage?.rateLimits) ? usage.rateLimits : [];
  const kinds = ['five_hour', 'seven_day'];
  for (const item of windows) {
    if (typeof item?.kind === 'string' && !kinds.includes(item.kind)) kinds.push(item.kind);
  }
  return kinds.map(kind => windowRow(kind, windows.find(item => item?.kind === kind), now));
}

export function meter(fraction, width = 20) {
  const cells = Math.max(8, Math.min(28, Math.floor(width)));
  if (fraction === null) return '─'.repeat(cells);
  const filled = Math.round(Math.max(0, Math.min(1, fraction)) * cells);
  return '━'.repeat(filled) + '─'.repeat(cells - filled);
}
