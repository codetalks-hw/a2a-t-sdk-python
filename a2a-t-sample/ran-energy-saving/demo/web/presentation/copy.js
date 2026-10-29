/** Business copy of the presentation page.
 *
 * The page only speaks in business terms: an audience reads the story of one energy-saving
 * request, never a protocol or transport detail. Protocol identifiers (``information``,
 * ``target``, …) stay internal and are mapped to labels and one-line narratives here.
 */

const EN = {
  lang: "en-US",
  title: "RAN energy saving · pre-task negotiation",
  scenarioLabel: "Demo scenario",
  requestTitle: "Request from the network operations engineer",
  requestHint: "Edit the request and start it; the NMS validates and negotiates what it receives.",
  reset: "Restore the scenario request",
  start: "Start the energy-saving task",
  starting: "Starting…",
  running: "A demo run is in progress…",
  waitForRun: "Finish the current demo before switching scenarios.",
  connectionLost: "Connection interrupted, reconnecting…",
  startFailed: "The demo could not be started.",
  emptyNegotiation: "Waiting for the request.",
  emptyNegotiationRunning: "Sending the request to the NMS…",
  acts: { negotiation: "Negotiation", execution: "NMS execution", result: "Result" },
  who: { oss: "OSS", nms: "NMS" },
  actions: {
    propose: "Proposal",
    counter: "Counter-proposal",
    accept: "Accept",
    reject: "Reject",
    abort: "Abort",
    end: "Negotiation over",
  },
  round: (round, maxRounds) =>
    maxRounds ? `Round ${round} of ${maxRounds}` : `Round ${round}`,
  roundShort: (round) => `R${round}`,
  abortLine: "The round limit is reached; the negotiation is aborted.",
  endLine: "The negotiation is over.",
  genericProposal: "The NMS needs to confirm the request before executing it.",
  executionDone: "Completed",
  notCreated: "No task created",
  map: {
    ossRole: "OSS / BSS",
    ossSide: "client side",
    nmsRole: "RAN NMS",
    nmsSide: "execution side",
    cellsRole: "Cells / base stations",
    cellsSide: "execution result",
    idle: "Not started",
    ossNegotiating: "Receiving proposals",
    ossAgreed: "Agreed with the NMS",
    ossReported: "Received the report",
    ossEnded: "Negotiation ended",
    nmsNegotiating: "Validating · negotiating",
    nmsExecuting: "Executing the plan",
    nmsReported: "Report delivered",
    nmsEnded: "Negotiation ended",
    cellsIdle: "Not started",
    cellsApplying: "Applying the plan",
    cellsApplied: "Plan applied",
    negotiationIdle: "Pre-task negotiation",
    negotiationWaiting: "Negotiation · round {round}/{max}",
    negotiationAgreed: "Agreed",
    negotiationEnded: "Negotiation ended",
    applyIdle: "Apply the plan",
    applying: "Applying",
    applied: "Applied",
    report: "Status + report",
  },
  taskCreated: "Task created · on duty",
  steps: {
    evaluate: "Evaluate the plan",
    apply: "Apply the plan",
    report: "Generate the report",
  },
  report: {
    title: "Energy-saving report",
    cell: "Cell",
    window: "Window",
    rate: "Energy saving",
    baseline: "Baseline",
    remaining: "Remaining",
    saved: "Estimated saving",
    onDuty: "The task stays on duty until it is cancelled.",
    noNumbers: "The plan was applied and the task is on duty.",
  },
  ended: {
    title: "Negotiation ended · no task created",
    rejected: "The OSS cannot provide the requested value.",
    aborted: "Counter-proposals did not converge within the round limit.",
    generic: "No task was created.",
  },
  failed: { title: "The demo did not complete" },
  tech: "Technical details",
  scenarios: {
    information: {
      label: "Missing value",
      story:
        "The request has no time-segmented guaranteed rate; the NMS asks for it before executing.",
      proposals: {
        1: "The time-segmented guaranteed rate is missing. Please provide it.",
      },
      decisions: { 1: "Accept: supply the missing guaranteed rate." },
    },
    target: {
      label: "Overlapping windows",
      story:
        "Two guaranteed-rate windows overlap; the NMS proposes a correction before executing.",
      proposals: {
        1: "Two guaranteed-rate windows overlap: 17:30~23:00 and 20:00~00:00.",
        2: "Revised with non-overlapping windows — please confirm.",
        3: "The segments still cannot be applied — please adjust once more.",
      },
      decisions: {
        1: "Counter-proposal: move the windows to 17:30~20:00 and 20:00~00:00.",
        2: "Accept the revised plan.",
      },
    },
    feasibility: {
      label: "Infeasible condition",
      story:
        "A guaranteed rate is requested during a power outage; the NMS offers a feasible alternative.",
      proposals: {
        1: "The rate cannot be guaranteed during the outage. Lower 06:00~18:00 to 2Mbps.",
      },
      decisions: { 1: "Accept: lower 06:00~18:00 to 2Mbps during the outage." },
    },
    reject: {
      label: "OSS rejects",
      story: "The NMS asks for a missing value; the OSS cannot provide it and the negotiation ends.",
      proposals: {
        1: "The time-segmented guaranteed rate is missing. Please provide it.",
      },
      decisions: { 1: "Reject: the guaranteed rate is not available." },
    },
    abort: {
      label: "Round limit",
      story: "Counter-proposals never converge; the NMS aborts at the round limit.",
      proposals: {
        1: "Two guaranteed-rate windows overlap: 17:30~23:00 and 20:00~00:00.",
        2: "Revised with non-overlapping windows — please confirm.",
        3: "The segments still cannot be applied — please adjust once more.",
      },
      decisions: {
        1: "Counter-proposal: keep the 20:00~00:00 window.",
        2: "Counter-proposal: keep both original high-throughput windows.",
        3: "Counter-proposal: keep the original request unchanged.",
      },
    },
  },
};

const ZH = {
  lang: "zh-CN",
  title: "RAN 节能 · 任务前协商",
  scenarioLabel: "演示场景",
  requestTitle: "网络操作工程师的请求",
  requestHint: "可修改请求后发起；网管会按实际收到的内容校验并协商。",
  reset: "恢复默认请求",
  start: "发起节能任务",
  starting: "正在发起…",
  running: "演示进行中…",
  waitForRun: "当前演示结束后再切换场景。",
  connectionLost: "连接中断，正在重连…",
  startFailed: "演示未能启动。",
  emptyNegotiation: "等待发起。",
  emptyNegotiationRunning: "正在把请求发给网管…",
  acts: { negotiation: "协商", execution: "网管执行", result: "结果" },
  who: { oss: "OSS", nms: "网管" },
  actions: {
    propose: "提议",
    counter: "反提议",
    accept: "接受",
    reject: "拒绝",
    abort: "中止",
    end: "协商结束",
  },
  round: (round, maxRounds) =>
    maxRounds ? `第 ${round} / ${maxRounds} 轮` : `第 ${round} 轮`,
  roundShort: (round) => `第${round}轮`,
  abortLine: "已达到轮次上限，协商中止。",
  endLine: "协商结束。",
  genericProposal: "网管需要先与你确认任务内容。",
  executionDone: "已完成",
  notCreated: "未创建任务",
  map: {
    ossRole: "OSS / BSS",
    ossSide: "客户侧",
    nmsRole: "RAN 网管",
    nmsSide: "执行侧",
    cellsRole: "小区 / 基站",
    cellsSide: "执行结果",
    idle: "待发起",
    ossNegotiating: "接收提议中",
    ossAgreed: "已与网管达成一致",
    ossReported: "已收到报告",
    ossEnded: "协商结束",
    nmsNegotiating: "校验 · 协商中",
    nmsExecuting: "执行方案中",
    nmsReported: "已出报告",
    nmsEnded: "协商结束",
    cellsIdle: "待发起",
    cellsApplying: "方案下发中",
    cellsApplied: "方案已下发",
    negotiationIdle: "任务前协商",
    negotiationWaiting: "协商 · 第 {round}/{max} 轮",
    negotiationAgreed: "已达成一致",
    negotiationEnded: "协商结束",
    applyIdle: "方案下发",
    applying: "下发中",
    applied: "已下发",
    report: "状态 + 报告",
  },
  taskCreated: "任务已创建 · 值守中",
  steps: {
    evaluate: "评估方案",
    apply: "下发方案",
    report: "生成报告",
  },
  report: {
    title: "节能报告",
    cell: "小区",
    window: "时段",
    rate: "节能",
    baseline: "基线用电",
    remaining: "剩余用电",
    saved: "预计节省",
    onDuty: "任务持续值守，直到被取消。",
    noNumbers: "方案已下发，任务进入值守。",
  },
  ended: {
    title: "协商结束 · 未创建任务",
    rejected: "OSS 无法提供所需参数。",
    aborted: "多轮反提议未收敛，已达到轮次上限。",
    generic: "本次协商未创建任务。",
  },
  failed: { title: "演示未能完成" },
  tech: "技术详情",
  scenarios: {
    information: {
      label: "缺参数",
      story: "请求缺少分时段保障速率，网管要求补充后再执行。",
      proposals: {
        1: "请求缺少分时段保障速率，请补充后再执行。",
      },
      decisions: { 1: "接受：补齐分时段保障速率。" },
    },
    target: {
      label: "时段重叠",
      story: "两个保障时段重叠，网管先提议修正，达成一致后再执行。",
      proposals: {
        1: "两个保障时段重叠：17:30~23:00 与 20:00~00:00。",
        2: "已按不重叠的时段修订，请确认。",
        3: "仍未满足可执行条件，请再调整一次。",
      },
      decisions: {
        1: "反提议：把时段改为 17:30~20:00 与 20:00~00:00，消除重叠。",
        2: "接受修订后的方案。",
      },
    },
    feasibility: {
      label: "条件不可行",
      story: "停电期间仍要求保障速率，网管给出可行替代方案。",
      proposals: {
        1: "停电期间无法保障速率，建议把 06:00~18:00 下调到 2Mbps。",
      },
      decisions: { 1: "接受：停电期间把 06:00~18:00 下调到 2Mbps。" },
    },
    reject: {
      label: "拒绝",
      story: "网管要求补充参数，OSS 无法提供，协商结束。",
      proposals: {
        1: "请求缺少分时段保障速率，请补充后再执行。",
      },
      decisions: { 1: "拒绝：无法提供该保障速率。" },
    },
    abort: {
      label: "轮次超限",
      story: "多轮反提议仍不收敛，网管在轮次上限时中止。",
      proposals: {
        1: "两个保障时段重叠：17:30~23:00 与 20:00~00:00。",
        2: "已按不重叠的时段修订，请确认。",
        3: "仍未满足可执行条件，请再调整一次。",
      },
      decisions: {
        1: "反提议：坚持保留 20:00~00:00 时段。",
        2: "反提议：保留原有两个高保障时段。",
        3: "反提议：维持原请求不变。",
      },
    },
  },
};

const COPY = { "en-US": EN, "zh-CN": ZH };

/** Return the copy of one process language, falling back to English. */
export function copyFor(language) {
  return COPY[language] || EN;
}

/** Return the display label of one scenario. */
export function scenarioLabel(copy, name) {
  return copy.scenarios[name]?.label || name;
}

/** Return the one-line narrative of one scenario. */
export function scenarioStory(copy, name) {
  return copy.scenarios[name]?.story || "";
}

/** Return the human sentence of one NMS proposal. */
export function proposalLine(copy, scenario, round) {
  const proposals = copy.scenarios[scenario]?.proposals || {};
  return proposals[round] || "";
}

/** Return the human sentence of one OSS decision. */
export function decisionLine(copy, scenario, round) {
  const decisions = copy.scenarios[scenario]?.decisions || {};
  return decisions[round] || "";
}

/** Return the localized label of one negotiation action. */
export function actionLabel(copy, action) {
  return copy.actions[action] || action;
}

/** Fill ``{name}`` placeholders of one template. */
export function format(template, values) {
  return String(template).replace(/\{(\w+)\}/g, (match, key) =>
    Object.hasOwn(values, key) ? String(values[key]) : match,
  );
}
