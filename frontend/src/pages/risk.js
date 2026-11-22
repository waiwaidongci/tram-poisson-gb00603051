import {feedback, field} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {ALARM, DAY_STATE, WINDOW_STATE} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

const statePill = state => pill(WINDOW_STATE[state] || state, state);
const dayPill = state => pill(DAY_STATE[state] || state, state);
const alarmPill = state => pill(ALARM[state] || state, state);
const pct = number => `${Number(number).toFixed(4)}`;
const windowKey = row => [row.date, row.road, row.intersection, row.shift].join('|');

export async function riskPage(route, refresh) {
  const {spec, runs} = workspace;
  const message = feedback();
  const completeRuns = runs.filter(run => run.status === 'complete');
  const monitors = await workspace.monitors();

  if (!completeRuns.length) {
    return panel(spec.pages[4], empty('还没有计算完成的试算运行，无法建立低估盯盘。', '#run', spec.pages[2]));
  }

  // 路由 id 优先，否则取最近一个有盯盘的运行，再退回最近完成的运行
  const runId = Number(route.id) || monitors[0]?.run_id || completeRuns[0].id;
  const run = await workspace.getRun(runId);
  const monitor = monitors.find(item => item.run_id === runId);
  const detail = monitor ? await workspace.getMonitor(monitor.id) : null;

  const runPicker = el('label', {class:'field run-picker', for:'risk-run-picker'}, el('span', {}, '选择试算运行'),
    el('select', {id:'risk-run-picker', onChange: event => { location.hash = `#risk/${event.target.value}`; }},
      completeRuns.map(item => el('option', {value:item.id, selected:item.id === runId}, item.name))));

  const submitCreate = button('基于该运行建立低估盯盘', {type:'submit', class:'primary', 'data-testid':'create-monitor'});
  const establish = session.canWrite && !monitor && el('form', {class:'fields',
    onSubmit: handler(submitCreate, message, async event => {
      event.preventDefault();
      await workspace.createMonitor(runId);
      location.hash = `#risk/${runId}`;
    })}, submitCreate,
    p('盯盘计划冻结该运行的逐窗预测，之后实绩与更正都在此计划上重放。', 'subtle'));

  if (!monitor) {
    return el('div', {class:'two-col'},
      el('div', {class:'wide-panel'}, panel(spec.pages[4], runPicker,
        empty('该运行尚未建立盯盘。', '#results/' + runId, '先查看试算结果'), establish)),
      el('aside', {class:'sidebar-panel'}, panel('盯盘口径', methodNotes(spec))));
  }

  const snap = detail.snapshot;

  // 指标卡：首个越线日、当前告警、告警额度、阻塞观测日
  const cards = [
    {label:'首个越线日', value:snap.first_crossing_day || '—',
     detail:snap.active_first_crossing_day ? `当前最早在警：${snap.active_first_crossing_day}` : '无在警窗口'},
    {label:'在警 / 已撤回', value:`${snap.active_alarms.length} / ${snap.alarms.length - snap.active_alarms.length}`,
     detail:'撤回也算发过，额度不还'},
    {label:'告警额度', value:`${snap.budget_used} / ${snap.budget_total}`,
     detail:`剩余 ${snap.budget_remaining} 个窗口 · 单窗 α=${pct(snap.alpha_per_window)}`},
    {label:'观测日进度', value:snap.blocked_at ? `等待 ${snap.blocked_at}` : '全部等齐',
     detail:`全线误报上界 ${(snap.family_alpha * 100).toFixed(0)}%（Bonferroni）`},
  ];

  // 观测日等齐进度
  const dayColumns = [
    {key:'date', label:'观测日'},
    {key:'state', label:'状态', render:row => dayPill(row.state)},
    {key:'expected', label:'应有窗口'},
    {key:'reported', label:'已报窗口'},
    {key:'missing', label:'缺窗'},
  ];

  // 窗口明细表
  const windowColumns = [
    {key:'date', label:'观测日'}, {key:'road', label:'道路'}, {key:'intersection', label:'路口'},
    {key:'shift', label:'时段'},
    {key:'predicted', label:'预测期望', render:row => value(Number(row.predicted).toFixed(3))},
    {key:'critical', label:'越线阈值 ≥', render:row => row.critical ?? '—'},
    {key:'actual_count', label:'实绩', render:row => row.actual_count ?? '—'},
    {key:'p_value', label:'上尾概率', render:row => row.p_value == null ? '—' : pct(row.p_value)},
    {key:'status', label:'状态', render:row => statePill(row.status)},
    {label:'操作', render:row => session.canWrite && row.status !== '已撤回'
      ? reportButton(row, monitor.id, message, refresh) : '—'},
  ];

  // 告警与撤回台账
  const alarmColumns = [
    {key:'window_date', label:'观测日'}, {key:'road', label:'道路'}, {key:'intersection', label:'路口'},
    {key:'shift', label:'时段'},
    {key:'fired_count', label:'告警实绩'},
    {key:'critical', label:'阈值 ≥'},
    {key:'fired_on', label:'告警依据日'},
    {key:'state', label:'状态', render:row => alarmPill(row.state)},
    {key:'withdrawn_on', label:'撤回日', render:row => row.withdrawn_on || '—'},
    {key:'withdraw_reason', label:'撤回依据', render:row => row.withdraw_reason || row.basis},
  ];

  // 实绩版本流水
  const reportColumns = [
    {key:'observed_on', label:'观测/改动日'},
    {key:'window_key', label:'窗口'},
    {key:'version', label:'版本', render:row => row.version === 1 ? '首版' : `v${row.version} 更正`},
    {key:'count', label:'实绩次数'},
    {key:'actor', label:'上报人'},
    {label:'时间', render:row => date(row.created_at)},
  ];

  return el('div', {},
    el('div', {class:'result-head'}, p(run.name, 'eyeline'),
      p(`盯盘 #${monitor.id} · 冻结于 ${date(monitor.created_at)} · ${monitor.window_count} 个留出窗口`, 'subtle'),
      el('div', {class:'actions'}, runPicker, link('查看试算结果', `#results/${runId}`, {class:'quiet'}))),
    metrics(cards),
    el('div', {class:'two-col'},
      el('div', {class:'wide-panel'},
        panel('观测日等齐进度', table(dayColumns, snap.days, {caption:'观测日等齐进度', empty:'尚未收到实绩'})),
        panel('窗口盯盘明细', table(windowColumns, snap.windows, {caption:'窗口盯盘明细', pageSize:12})),
        panel('告警与撤回台账', table(alarmColumns, detail.alarms,
          {caption:'告警与撤回台账', empty:'全线尚未出现越线告警。'}))),
      el('aside', {class:'sidebar-panel'},
        panel('实绩上报', p('按观测日顺序处理；当日窗口等齐才判定，缺窗会阻塞后续观测日。更正从改动日起重放。', 'subtle'),
          message),
        panel('实绩版本流水', table(reportColumns, detail.reports.slice().reverse(),
          {caption:'实绩版本流水', empty:'还没有实绩上报。'})),
        panel('盯盘口径', methodNotes(spec)))));
}

function reportButton(row, monitorId, outerMessage, refresh) {
  const btn = button(row.actual_count == null ? '上报实绩' : '晚到更正', {class:'quiet'});
  btn.addEventListener('click', () => {
    const today = localDate();
    const modalMessage = feedback();
    const submit = button('提交', {type:'submit', class:'primary'});
    const form = el('form', {class:'fields modal-form'},
      el('p', {class:'eyeline'}, `${row.date} ${row.road}·${row.intersection}·${row.shift}`),
      p(`预测期望 ${Number(row.predicted).toFixed(3)}，越线阈值 ≥ ${row.critical ?? '—'}`, 'subtle'),
      field({key:'count', label:'确认阻断次数（实绩）', type:'int', min:0, max:10000}, row.actual_count ?? 0),
      field({key:'observed_on', label:row.actual_count == null ? '观测日报出日' : '更正改动日', type:'date'}, today),
      el('div', {class:'actions'}, submit, button('取消', {type:'button', class:'quiet'})), modalMessage);
    const overlay = el('div', {class:'modal-overlay'}, el('div', {class:'modal', role:'dialog', 'aria-modal':'true'}, form));
    form.querySelector('button.quiet').addEventListener('click', () => overlay.remove());
    overlay.addEventListener('click', event => { if (event.target === overlay) overlay.remove(); });
    form.addEventListener('submit', handler(submit, modalMessage, async event => {
      event.preventDefault();
      const data = new FormData(form);
      await workspace.reportActual(monitorId, {
        window_key: windowKey(row),
        count: Number(data.get('count')),
        observed_on: data.get('observed_on'),
      });
      overlay.remove();
      outerMessage.textContent = row.actual_count == null ? '实绩已上报，盯盘已重放。' : '更正已重放，告警状态已更新。';
      await refresh();
    }));
    document.body.append(overlay);
  });
  return btn;
}

function localDate() {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${now.getFullYear()}-${month}-${day}`;
}

function methodNotes(spec) {
  return el('ul', {class:'method-list'}, spec.methodology.slice(-2).map(text => el('li', {}, text)));
}
