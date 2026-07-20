/* Agent Orchestra — 한국 대기업 사무실 게임 뷰.
 * 캐릭터/사무실 타일: 자체 제작 픽셀 아트. 16px 타일, 12열 시트.
 * SSE 이벤트가 아바타들의 행동(이동/말풍선/회의/작업)을 구동한다.
 */

"use strict";

// ---------- 상수 ----------
const TILE = 16, SCALE = 3, TS = TILE * SCALE;      // 화면 타일 48px
const COLS = 20, ROWS = 11;                          // 960 x 528
const SHEET_COLS = 12;

const SPRITES = {
  orchestrator: 84,   // 총괄 (백발 + 빨간 타이)
  scribe: 111,        // 서기 (안경)
  verifier: 96,       // QA (안경 + 금색 타이)
  accountant: 110,    // 경리 (올림머리) — LLM 호출/예산 감시
  architect: 103,     // 수석 아키텍트 (도면 든 백발)
  trendbot: 101,      // 트렌드봇 (로봇)
  guest: 102,         // 초청 전문가 (서류가방)
};

// 태스크 role -> 직군 스프라이트/이름. 전원 시니어.
const ROLES = {
  backend:  { sprites: [85, 88, 100], name: "백엔드 시니어" },
  frontend: { sprites: [98, 87, 97],  name: "프론트 시니어" },
  design:   { sprites: [104, 105],    name: "디자이너" },
  test:     { sprites: [112],         name: "테스트 시니어" },
  docs:     { sprites: [86],          name: "테크라이터" },
  devops:   { sprites: [109],         name: "DevOps 시니어" },
};
const DEFAULT_ROLE = "backend";

// 타일 인덱스 (사무실)
const T_FLOOR = [48, 48, 48, 48, 49, 50, 51];        // 카펫(가중 랜덤)
const T_WALL = 57, T_WINDOW = 80, T_BOARD = 79, T_CLOCK = 125, T_DOOR = 45;
const T_DESK = 72, T_CHAIR = 73, T_MEET = 76, T_SHELF = 63;
const T_RACK = 74, T_CABINET = 89, T_PLANT = 77, T_COFFEE = 78;

// 회의 테이블(그리드 좌표)
const MEET = { x: 8, y: 4, w: 3, h: 2 };
const MEET_SEATS = [[7, 4], [7, 5], [11, 4], [11, 5], [8, 3], [9, 3],
                    [10, 3], [8, 6], [9, 6], [10, 6]];
const SCRIBE_POST = [17, 3], VERIFIER_POST = [17, 6], ORCH_POST = [6, 3];
const ACCOUNTANT_POST = [15, 2], ARCHITECT_POST = [13, 4], TRENDBOT_POST = [4, 2];

function deskPos(i) { return [2 + (i % 6) * 3, 8 + Math.floor(i / 6) * 2]; }
const px = (gx) => gx * TS + TS / 2;   // 그리드 -> 픽셀 중심

// ---------- 상태 ----------
const $ = (id) => document.getElementById(id);
let runId = null, models = {}, llmCalls = 0;
let currentWorkdir = null, es = null;
let tasks = {};            // task_id -> {task_id, role, target_file, status, ...}
let taskWorker = {};       // task_id -> actor
let actors = [];
let guests = [];           // 초청 전문가 아바타
let implementedCount = 0, totalTasks = 0;
let pendingDecisions = null, decisionIdx = 0, decisionChoices = {};

const canvas = $("game"), ctx = canvas.getContext("2d");
ctx.imageSmoothingEnabled = false;
const sheet = new Image();
sheet.src = "/static/assets/tilemap_packed.png?v=" + Date.now();

// ---------- 아바타 ----------
function makeActor(sprite, name, gx, gy) {
  const a = {
    sprite, name, x: px(gx), y: px(gy), tx: px(gx), ty: px(gy),
    speed: 130, flip: false, bubble: null, bubbleUntil: 0,
    typing: false, working: null, nextStatusAt: 0, subtitle: "",
    patrol: null, patrolIdx: 0, home: [gx, gy], visible: true,
  };
  actors.push(a);
  return a;
}
function walkTo(a, gx, gy) { a.tx = px(gx); a.ty = px(gy); }
function say(a, text, ms = 3500) { a.bubble = text; a.bubbleUntil = performance.now() + ms; }
function goHome(a) { walkTo(a, a.home[0], a.home[1]); }
function startWork(a, label) {
  a.typing = true;
  a.working = { label, since: performance.now() };
  a.nextStatusAt = performance.now() + 4000 + Math.random() * 3000;
}
function stopWork(a) { a.typing = false; a.working = null; }

let orch = null, scribe = null, verifier = null, accountant = null,
    architect = null, trendbot = null;

function resetScene() {
  actors = []; guests = []; tasks = {}; taskWorker = {};
  implementedCount = 0; totalTasks = 0; llmCalls = 0; updateStats();
  orch = makeActor(SPRITES.orchestrator, "총괄", ORCH_POST[0], ORCH_POST[1]);
  scribe = makeActor(SPRITES.scribe, "서기", SCRIBE_POST[0], SCRIBE_POST[1]);
  verifier = makeActor(SPRITES.verifier, "QA", VERIFIER_POST[0], VERIFIER_POST[1]);
  accountant = makeActor(SPRITES.accountant, "경리",
                         ACCOUNTANT_POST[0], ACCOUNTANT_POST[1]);
  architect = makeActor(SPRITES.architect, "수석 아키텍트",
                        ARCHITECT_POST[0], ARCHITECT_POST[1]);
  trendbot = makeActor(SPRITES.trendbot, "트렌드봇",
                       TRENDBOT_POST[0], TRENDBOT_POST[1]);
  renderTaskChips();
}

// ---------- 맵 (오프스크린에 한 번 렌더) ----------
const bgCanvas = document.createElement("canvas");
bgCanvas.width = COLS * TS; bgCanvas.height = ROWS * TS;

function drawTileOn(c, idx, gx, gy) {
  c.drawImage(sheet, (idx % SHEET_COLS) * TILE, Math.floor(idx / SHEET_COLS) * TILE,
              TILE, TILE, gx * TS, gy * TS, TS, TS);
}
function buildMap() {
  const c = bgCanvas.getContext("2d");
  c.imageSmoothingEnabled = false;
  let seed = 7;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  for (let y = 1; y < ROWS; y++)
    for (let x = 0; x < COLS; x++)
      drawTileOn(c, T_FLOOR[Math.floor(rnd() * T_FLOOR.length)], x, y);
  // 벽: 패널 + 창문(도시 뷰) + 화이트보드(회의실 옆) + 시계
  for (let x = 0; x < COLS; x++) drawTileOn(c, T_WALL, x, 0);
  [2, 3, 6, 15, 16].forEach((x) => drawTileOn(c, T_WINDOW, x, 0));
  drawTileOn(c, T_BOARD, 9, 0); drawTileOn(c, T_BOARD, 10, 0);
  drawTileOn(c, T_CLOCK, 12, 0);
  drawTileOn(c, T_DOOR, 0, 1);
  // 회의 테이블 + 의자
  for (let dx = 0; dx < MEET.w; dx++)
    for (let dy = 0; dy < MEET.h; dy++)
      drawTileOn(c, T_MEET, MEET.x + dx, MEET.y + dy);
  MEET_SEATS.forEach(([x, y]) => drawTileOn(c, T_CHAIR, x, y));
  // 서기 코너(책장+캐비닛), 경리(서류 캐비닛), QA(서버랙), 편의시설
  drawTileOn(c, T_SHELF, 17, 1); drawTileOn(c, T_SHELF, 18, 1);
  drawTileOn(c, T_CABINET, 15, 1);
  drawTileOn(c, T_RACK, 18, 6);
  drawTileOn(c, T_PLANT, 1, 1); drawTileOn(c, T_PLANT, 19, 1);
  drawTileOn(c, T_PLANT, 19, 10);
  drawTileOn(c, T_COFFEE, 13, 1);
  // 개인 자리: 모니터 책상 12개 + 의자
  for (let i = 0; i < 12; i++) {
    const [x, y] = deskPos(i);
    drawTileOn(c, T_DESK, x, y);
    drawTileOn(c, T_CHAIR, x, y - 1);
  }
}
sheet.onload = () => { buildMap(); requestAnimationFrame(loop); };

// ---------- 렌더 루프 ----------
let lastT = 0;
function loop(t) {
  const dt = Math.min((t - lastT) / 1000, 0.05); lastT = t;
  const now = performance.now();
  for (const a of actors) {
    const dx = a.tx - a.x, dy = a.ty - a.y, d = Math.hypot(dx, dy);
    if (d > 2) {
      a.x += (dx / d) * a.speed * dt; a.y += (dy / d) * a.speed * dt;
      if (Math.abs(dx) > 4) a.flip = dx < 0;
    }
    if (a.patrol && d <= 2) {
      a.patrolIdx = (a.patrolIdx + 1) % a.patrol.length;
      const [gx, gy] = a.patrol[a.patrolIdx];
      walkTo(a, gx, gy);
    }
    // 개인 자리 업무 상태 말풍선: "지금 ~ 하는 중 (경과, 예상)"
    if (a.working && now >= a.nextStatusAt) {
      const sec = Math.floor((now - a.working.since) / 1000);
      const mm = Math.floor(sec / 60), ss = sec % 60;
      say(a, `지금 ${a.working.label} 작업 중 (경과 ${mm}분 ${ss}초 · 예상 1~3분)`, 5000);
      a.nextStatusAt = now + 9000 + Math.random() * 4000;
    }
  }
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bgCanvas, 0, 0);
  for (const a of [...actors].sort((p, q) => p.y - q.y)) {
    if (!a.visible) continue;
    const moving = Math.hypot(a.tx - a.x, a.ty - a.y) > 2;
    const bob = (moving || a.typing) ? Math.sin(now / 90) * 3 : 0;
    const sx = (a.sprite % SHEET_COLS) * TILE,
          sy = Math.floor(a.sprite / SHEET_COLS) * TILE;
    ctx.save();
    ctx.translate(a.x, a.y + bob);
    if (a.flip) ctx.scale(-1, 1);
    ctx.drawImage(sheet, sx, sy, TILE, TILE, -TS / 2, -TS + 10, TS, TS);
    ctx.restore();
    // 이름표
    ctx.font = "11px 'Malgun Gothic', sans-serif";
    const nw = ctx.measureText(a.name).width + 8;
    ctx.fillStyle = "rgba(0,0,0,.55)";
    ctx.fillRect(a.x - nw / 2, a.y + 12, nw, 15);
    ctx.fillStyle = "#fff"; ctx.textAlign = "center";
    ctx.fillText(a.name, a.x, a.y + 23);
    if (a.subtitle) {   // 종료 후 각자 수행한 역할 표시
      ctx.font = "10px 'Malgun Gothic', sans-serif";
      const sw = ctx.measureText(a.subtitle).width + 8;
      ctx.fillStyle = "rgba(0,0,0,.45)";
      ctx.fillRect(a.x - sw / 2, a.y + 28, sw, 13);
      ctx.fillStyle = "#ffd98a";
      ctx.fillText(a.subtitle, a.x, a.y + 38);
    }
  }
  for (const a of actors)
    if (a.visible && a.bubble && now < a.bubbleUntil) drawBubble(a);
  requestAnimationFrame(loop);
}

function drawBubble(a) {
  ctx.font = "12px 'Malgun Gothic', sans-serif";
  const lines = wrap(a.bubble, 18);
  const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 16;
  const h = lines.length * 15 + 10;
  let bx = a.x - w / 2, by = a.y - TS - h - 4;
  bx = Math.max(4, Math.min(bx, canvas.width - w - 4));
  by = Math.max(4, by);
  ctx.fillStyle = "#fffef2"; ctx.strokeStyle = "#3b2f2f"; ctx.lineWidth = 2;
  roundRect(bx, by, w, h, 6); ctx.fill(); ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(a.x - 5, by + h); ctx.lineTo(a.x + 5, by + h);
  ctx.lineTo(a.x, by + h + 7); ctx.closePath();
  ctx.fillStyle = "#fffef2"; ctx.fill();
  ctx.fillStyle = "#222"; ctx.textAlign = "left";
  lines.forEach((l, i) => ctx.fillText(l, bx + 8, by + 17 + i * 15));
}
function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}
function wrap(text, n) {
  const out = [];
  for (let s = text; s.length; s = s.slice(n)) out.push(s.slice(0, n));
  return out.slice(0, 3);
}

// ---------- 서기의 기록 ----------
function record(text, cls = "") {
  const d = document.createElement("div");
  d.className = "entry " + cls;
  const t = new Date().toLocaleTimeString("ko-KR", { hour12: false });
  d.innerHTML = `<span class="t">${t}</span>${text}`;
  $("chronicle").prepend(d);
}
const esc = (s) => (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const fold = (title, body) =>
  `<details><summary>${title}</summary><pre>${esc(body)}</pre></details>`;

// ---------- 태스크 칩 ----------
const DOT = { impl: "#c084fc", ready: "#6ea8fe", pass: "#4ade80", fail: "#f87171" };
function renderTaskChips() {
  $("taskChips").innerHTML = Object.values(tasks).map((t) =>
    `<span class="chip" data-id="${t.task_id}">
       <i style="background:${DOT[t.status] || "#666"}"></i>${esc(t.target_file)}
       ${t.retry_count ? `<em>↻${t.retry_count}</em>` : ""}</span>`).join("");
  document.querySelectorAll("#taskChips .chip").forEach((el) => {
    el.onclick = () => showTaskModal(el.dataset.id);
  });
}
function showTaskModal(id) {
  const t = tasks[id];
  $("modalTitle").textContent = `${t.target_file} — ${(ROLES[t.role] || {}).name || t.role}`;
  $("modalBody").innerHTML =
    `<p>${esc(t.description || "")}</p>` +
    (t.last_error ? `<h4>실패 로그</h4><pre>${esc(t.last_error)}</pre>` : "") +
    (t.code ? `<h4>생성 코드</h4><pre>${esc(t.code)}</pre>` : "");
  $("modal").style.display = "flex";
}
$("modalClose").onclick = () => ($("modal").style.display = "none");

function updateStats() { $("llmCalls").textContent = llmCalls; }

// ---------- RPG 대화창 ----------
function openDialog(html) { $("dialog").innerHTML = html; $("dialog").style.display = "block"; }
function closeDialog() { $("dialog").style.display = "none"; }

function showDecisionDialog() {
  const d = pendingDecisions.decisions[decisionIdx];
  const total = pendingDecisions.decisions.length;
  openDialog(`
    <div class="dlg-head">📜 회의 안건 ${decisionIdx + 1} / ${total}</div>
    <div class="dlg-q">${esc(d.question)}</div>
    <div class="dlg-why">${esc(d.why_important)}</div>
    <div class="dlg-opts">${d.options.map((o, i) => `
      <div class="dlg-opt ${o.name === d.recommended ? "rec" : ""}" data-i="${i}">
        ▶ ${esc(o.name)} ${o.name === d.recommended ? "★추천" : ""}
        <div class="dlg-detail">장점: ${esc(o.pros)}<br>단점: ${esc(o.cons)}<br>적합: ${esc(o.fit)}</div>
      </div>`).join("")}</div>
    <div class="dlg-reason">💡 ${esc(d.reason)}</div>`);
  document.querySelectorAll(".dlg-opt").forEach((el) => {
    el.onclick = () => {
      const opt = d.options[+el.dataset.i];
      decisionChoices[d.decision_id] = opt.name;
      record(`🗳 회의 결과: <b>${esc(d.question)}</b> → <b>${esc(opt.name)}</b>. "그럼 그렇게 진행하지!"`, "meet");
      say(scribe, `✍ ${opt.name}... 기록했다.`);
      decisionIdx++;
      if (decisionIdx < pendingDecisions.decisions.length) showDecisionDialog();
      else {
        closeDialog();
        resume(decisionChoices);
        [orch, scribe, verifier, accountant, architect].forEach(goHome);
        say(orch, "결정 끝! 설계에 들어간다.");
      }
    };
  });
}

function showEscalationDialog(payload) {
  openDialog(`
    <div class="dlg-head">🚨 긴급 회의 — 자동 재작업 한도 초과</div>
    <div class="dlg-q">${esc(payload.message)}</div>
    <pre class="dlg-log">${esc((payload.failing_tasks || [])
      .map((f) => `[${f.task_id}] ${(f.error || "").slice(0, 400)}`).join("\n\n"))}</pre>
    <div class="dlg-opts">
      <div class="dlg-opt" id="escRetry">▶ 카운터 리셋 후 재시도</div>
      <div class="dlg-opt" id="escStop">▶ 여기서 중단</div>
    </div>`);
  $("escRetry").onclick = () => { closeDialog(); resume("retry");
    record("🗳 회의 결과: 재시도한다. 개발자들이 다시 책상으로!", "meet"); };
  $("escStop").onclick = () => { closeDialog(); resume("stop");
    record("🗳 회의 결과: 여기서 중단한다.", "meet"); };
}

async function resume(value) {
  await fetch(`/runs/${runId}/resume`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
}

// ---------- 이벤트 -> 연출 ----------
function gatherMeeting() {
  [orch, scribe, verifier, accountant, architect].forEach((a, i) => {
    const [x, y] = MEET_SEATS[i];
    walkTo(a, x, y);
  });
  Object.values(taskWorker).forEach((w, i) => {
    const seat = MEET_SEATS[(i + 5) % MEET_SEATS.length];
    walkTo(w, seat[0], seat[1]);
  });
}

function roleName(role, counters) {
  const cfg = ROLES[role] || ROLES[DEFAULT_ROLE];
  counters[role] = (counters[role] || 0) + 1;
  return { name: `${cfg.name}${counters[role]}`,
           sprite: cfg.sprites[(counters[role] - 1) % cfg.sprites.length] };
}

function handle(ev) {
  if (ev.llm_calls_delta) {
    llmCalls += ev.llm_calls_delta; updateStats();
    if (llmCalls >= 45) {
      say(accountant, `⚠ LLM 호출 ${llmCalls}/60! 예산이 아슬아슬합니다`, 4500);
      if (llmCalls >= 45 && llmCalls - ev.llm_calls_delta < 45)
        record(`💰 경리: 운영 비용 경고 — LLM 호출 ${llmCalls}/60.`, "fail");
    } else if (llmCalls % 5 === 0) {
      say(accountant, `장부 기록 완료. LLM 호출 ${llmCalls}회째.`, 3000);
    }
  }
  switch (ev.type) {
    case "started":
      models = ev.models;
      $("modelInfo").textContent =
        `총괄·설계 ${models.orchestrator} · 리뷰 ${models.reviewer} · 워커 ${models.worker} · 유틸 ${models.utility}`;
      resetScene();
      walkTo(trendbot, 3, 3);
      say(trendbot, "삐빅. 최신 트렌드 조사 시작.", 4000);
      record(`📜 <b>새 의뢰 접수</b> — "${esc(ev.request)}"`, "meet");
      if (ev.skills && ev.skills.length)
        record(`🛠 수석 아키텍트 리뷰 스킬 장착: ` +
               ev.skills.map((s) => `<code>${esc(s)}</code>`).join(", ") +
               (ev.ponytail_level ? ` (강도: <b>${esc(ev.ponytail_level)}</b>)` : ""));
      else if (ev.ponytail_level === "off")
        record(`🛠 포니테일 렌즈 미장착 (off) — 이번 실행은 단순화 리뷰 없이 진행.`);
      break;

    case "node": handleNode(ev); break;

    case "interrupt":
      if (ev.payload.type === "decision_required") {
        gatherMeeting();
        say(orch, "전원 회의 소집! 당신의 결정이 필요하다.", 6000);
        record(`🔔 <b>회의 소집</b> — 결정 안건 ${ev.payload.decisions.length}건. 당신의 선택을 기다린다.`, "meet");
        pendingDecisions = ev.payload; decisionIdx = 0; decisionChoices = {};
        setTimeout(showDecisionDialog, 900);
      } else {
        gatherMeeting();
        say(verifier, "한도 초과다. 사람의 판단이 필요하다!", 6000);
        record("🚨 <b>긴급 회의</b> — 자동 재작업 한도 초과.", "fail");
        setTimeout(() => showEscalationDialog(ev.payload), 900);
      }
      break;

    case "resumed":
      closeDialog();   // 히스토리 재생 시 이미 답한 회의 대화창이 다시 열리는 것 방지
      break;

    case "done": {
      record(`🏁 <b>실행 종료</b><pre>${esc(ev.final_summary)}</pre>`, "meet");
      // 다들 자기 자리로 돌아가 앉고, 이름표 아래에 맡았던 역할을 남긴다.
      const ROLE_DONE = {
        "총괄": "기획·설계 총괄", "서기": "전 과정 기록", "QA": "샌드박스 검증",
        "경리": `LLM 호출 ${ev.llm_calls_total ?? llmCalls}회 결산`,
        "수석 아키텍트": "설계·코드 리뷰", "트렌드봇": "기술 동향 조사",
      };
      actors.forEach((a) => {
        if (!a.visible) return;
        stopWork(a);
        goHome(a);
        if (ROLE_DONE[a.name]) a.subtitle = ROLE_DONE[a.name];
        say(a, "🎉 수고했다!", 4000);
      });
      Object.entries(taskWorker).forEach(([tid, w]) => {
        const t = tasks[tid];
        if (t) w.subtitle = `${t.target_file} ${t.verified ? "✓" : ""}`;
      });
      // 실행 종료 — 연결을 닫지 않으면 EventSource가 자동 재접속해서
      // 히스토리가 무한 리플레이된다 (렉처럼 보이는 원인).
      if (es) { es.close(); es = null; }
      break;
    }

    case "error":
      record(`❌ <b>오류</b> — ${esc(ev.message)}`, "fail");
      say(verifier, "문제가 생겼다! 기록을 확인하라.", 6000);
      if (es) { es.close(); es = null; }   // 재접속 무한 리플레이 방지
      break;
  }
}

function handleNode(ev) {
  switch (ev.node) {
    case "trend_research":
      goHome(trendbot);
      say(trendbot, "트렌드 조사 완료. 보고서 전달!", 4500);
      record(`🤖 <b>트렌드봇 보고</b> (${ev.model})` +
             fold("조사 내용 보기", ev.trend_report), "meet");
      break;

    case "collect_decisions": {
      say(orch, `결정 안건 ${ev.decision_count}건을 정리했다.`);
      record(`총괄(${ev.model})이 기획 체계에 따라 중대 결정 ${ev.decision_count}건을 안건으로 올렸다.`);
      const sps = ev.specialists || [];
      if (sps.length) {
        record(`📞 총괄이 전담 전문가 초청 요청: ` +
               sps.map((s) => `<b>${esc(s.role)}</b>(${esc(s.reason)})`).join(", "), "meet");
        say(orch, "이 건은 전문가를 모셔야겠다.", 4000);
      }
      break;
    }

    case "consult": {
      (ev.specialists || []).forEach((sp, i) => {
        const g = makeActor(SPRITES.guest, sp.role, -1, 2 + i);
        guests.push(g);
        walkTo(g, 12 + i, 5);
        setTimeout(() => say(g, "자문 드리겠습니다.", 3500), 1200 + i * 400);
        record(`👔 <b>${esc(sp.role)}</b> 초청 자문` +
               fold("자문 내용 보기", sp.notes), "meet");
      });
      break;
    }

    case "decompose": {
      if (ev.workdir) {
        currentWorkdir = ev.workdir;
        $("projName").textContent = ev.project_name;
        $("projectRow").classList.remove("hidden");
        record(`📁 <b>프로젝트 폴더 생성</b> — <code>${esc(ev.project_name)}</code>`, "meet");
      }
      totalTasks = ev.tasks.length; implementedCount = 0;
      // 재설계인 경우 기존 워커 제거 후 다시 배치
      Object.values(taskWorker).forEach((w) => { w.visible = false; });
      taskWorker = {}; tasks = {};
      say(orch, `설계 확정. 태스크 ${totalTasks}개, 각 직군에 배정한다!`, 5000);
      record(`📋 <b>설계 체계 확정</b> — 태스크 ${totalTasks}개:<br>` +
        ev.tasks.map((t) => `· [${(ROLES[t.role] || {}).name || t.role}] <code>${esc(t.target_file)}</code>`).join("<br>") +
        fold("아키텍처 설계서", ev.architecture) +
        fold("개발 컨벤션", ev.conventions) +
        fold("검증 계획", ev.verification_plan));
      const counters = {};
      ev.tasks.forEach((t, i) => {
        tasks[t.task_id] = { ...t, status: "impl", retry_count: 0 };
        const { name, sprite } = roleName(t.role || DEFAULT_ROLE, counters);
        const w = makeActor(sprite, name, -1, 1 + (i % 8));
        taskWorker[t.task_id] = w;
        const [dx, dy] = deskPos(i);
        w.home = [dx, dy - 1];
        walkTo(w, dx, dy - 1);
        setTimeout(() => {
          say(w, `${t.target_file} 맡았습니다!`, 2500);
          startWork(w, t.target_file);
        }, 600 + i * 250);
      });
      goHome(orch);
      renderTaskChips();
      break;
    }

    case "design_review":
      walkTo(architect, ORCH_POST[0] + 1, ORCH_POST[1]);
      if (ev.approved) {
        say(architect, "설계 리뷰 통과. 구현 들어가자!", 5000);
        record(`✅ <b>설계 리뷰 승인</b> — 수석 아키텍트(${ev.model})가 QA 렌즈 검토를 마쳤다.`, "meet");
        setTimeout(() => goHome(architect), 2500);
      } else {
        say(architect, "이 설계로는 안 된다. 반려!", 5000);
        say(orch, "피드백 반영해서 다시 설계한다...", 5000);
        record(`⛔ <b>설계 반려</b> — 수석 아키텍트(${ev.model}):` +
               fold("반려 사유 보기", ev.feedback), "fail");
      }
      break;

    case "implement": {
      const r = ev.results[0];
      Object.assign(tasks[r.task_id], r, { status: "ready" });
      const w = taskWorker[r.task_id];
      if (w) { stopWork(w); say(w, `✅ 완료!`); }
      record(`<b>${w ? w.name : r.task_id}</b>(${ev.model})가 <code>${esc(tasks[r.task_id].target_file)}</code> 구현을 마쳤다.`);
      implementedCount++;
      renderTaskChips();
      if (implementedCount >= totalTasks) {
        say(verifier, "전원 제출! 서버랙에서 검증 들어간다.", 4000);
        record(`🛡 <b>검증 시작</b> — QA가 Docker 샌드박스에서 컴파일 + pytest 실행 중.`);
        verifier.patrol = [[2, 7], [8, 7], [14, 7], [17, 6]];
      }
      break;
    }

    case "verify": {
      verifier.patrol = null; goHome(verifier);
      const allPass = ev.results.every((r) => r.verified);
      ev.results.forEach((r) => Object.assign(tasks[r.task_id], r,
        { status: r.verified ? "pass" : (r.last_error ? "fail" : "ready") }));
      renderTaskChips();
      if (allPass) {
        say(verifier, "전부 통과다! 🎉", 5000);
        record(`✅ <b>검증 통과</b> — 모든 파일이 컴파일과 테스트를 통과했다.`, "meet");
        guests.forEach((g) => { walkTo(g, 0, 1); setTimeout(() => g.visible = false, 4000); });
      } else {
        const failed = ev.results.filter((r) => r.last_error);
        say(verifier, `${failed.length}개 파일에서 문제 발견!`, 5000);
        failed.forEach((r) => {
          const w = taskWorker[r.task_id];
          if (w) { startWork(w, tasks[r.task_id].target_file + " 수정"); say(w, "🔧 바로 고치겠습니다.", 4000); }
        });
        record(`⚠ <b>검증 실패</b> — 원인 특정: ${failed.map((r) =>
          `<code>${esc(tasks[r.task_id].target_file)}</code>`).join(", ")}. 담당 시니어만 재작업(보강 체계).`, "fail");
      }
      break;
    }

    case "code_review":
      walkTo(architect, 10, 7);   // 개발팀 자리 쪽으로 걸어가 코드 리뷰
      say(architect, "검증은 통과. 이제 군더더기를 보자.", 4500);
      record(`🎀 <b>포니테일 코드 리뷰</b> — 수석 아키텍트(${ev.model})` +
             fold("리뷰 결과 보기", ev.report), "meet");
      setTimeout(() => goHome(architect), 3500);
      break;

    case "rework":
      ev.results.forEach((r) => {
        Object.assign(tasks[r.task_id], r, { status: "ready" });
        const w = taskWorker[r.task_id];
        if (w) { stopWork(w); say(w, "재구현 완료! 다시 봐 주세요."); }
      });
      record(`🔧 태스크 ${ev.results.length}개 재구현 완료 — 재검증에 들어간다.`);
      verifier.patrol = [[2, 7], [8, 7], [14, 7], [17, 6]];
      renderTaskChips();
      break;

    case "finalize":
      record(`📦 <b>서기의 최종 기록</b><pre>${esc(ev.final_summary || "")}</pre>`, "meet");
      say(scribe, "모든 기록을 남겼다. ✍", 5000);
      break;
  }
}

// ---------- 모델 셀렉트 ----------
async function loadModels() {
  const res = await fetch("/models");
  const cfg = await res.json();
  const rows = [];
  const label = { orchestrator: "총괄·설계", reviewer: "설계 리뷰", worker: "워커(개발팀)", utility: "유틸·트렌드" };
  const PROV = { anthropic: "Claude", openai: "GPT", google: "Gemini" };
  for (const [role, sc] of Object.entries(cfg.selectable)) {
    rows.push(`<div class="mrow"><span>${label[role] || role}</span>
      <select data-role="${role}">${sc.options.map((o) =>
        `<option value="${o.id}" ${o.id === sc.default ? "selected" : ""}
                 ${o.available ? "" : "disabled"}>
           ${o.id} · ${PROV[o.provider] || o.provider}${o.available ? "" : " (키 없음)"}
         </option>`).join("")}
      </select></div>`);
  }
  $("modelPanel").innerHTML = rows.join("");
}

function selectedModels() {
  const out = {};
  document.querySelectorAll("#modelPanel select[data-role]").forEach((el) => {
    out[el.dataset.role] = el.value;
  });
  return out;
}

// ---------- 시작 ----------
$("startBtn").onclick = async () => {
  const user_request = $("reqInput").value.trim();
  if (!user_request) return;
  $("startBtn").disabled = true;
  const res = await fetch("/runs", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_request, workdir: $("workdirInput").value,
                           models: selectedModels(),
                           ponytail_level: $("ponytailLevel").value }),
  });
  attachRun((await res.json()).run_id);
};

// ---------- 실행 연결 (새로고침/다른 클라이언트 시작 실행에도 붙는다) ----------
function attachRun(id) {
  if (es) es.close();
  runId = id;
  llmCalls = 0; tasks = {}; currentWorkdir = null;
  $("projectRow").classList.add("hidden");
  es = new EventSource(`/runs/${id}/events`);   // 서버가 히스토리 전체를 재생해줌
  es.onmessage = (e) => handle(JSON.parse(e.data));
}

async function attachLatest() {
  try {
    const runs = await (await fetch("/runs")).json();
    if (runs.length && runs[0].run_id !== runId) attachRun(runs[0].run_id);
  } catch (e) { /* 서버 재시작 중이면 다음 폴링에서 재시도 */ }
}
setInterval(attachLatest, 8000);

// ---------- 포니테일 도구 버튼 ----------
const wd = () => encodeURIComponent(currentWorkdir || $("workdirInput").value);

$("btnRename").onclick = async () => {
  const name = prompt("새 폴더명:", $("projName").textContent);
  if (!name || !currentWorkdir) return;
  const r = await fetch("/projects/rename", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workdir: currentWorkdir, new_name: name }),
  });
  if (!r.ok) { record(`❌ 폴더명 변경 실패: ${esc((await r.json()).detail)}`, "fail"); return; }
  const d = await r.json();
  currentWorkdir = d.workdir;
  $("projName").textContent = d.project_name;
  record(`📁 폴더명 변경 → <code>${esc(d.project_name)}</code>`, "meet");
};

$("btnAudit").onclick = async () => {
  record("🎀 /ponytail-audit 실행 중... (Fable 5)");
  say(architect, "레포 전체 감사 들어간다.", 4000);
  try {
    const r = await fetch(`/ponytail/audit?workdir=${wd()}&level=${$("ponytailLevel").value}`);
    if (!r.ok) throw new Error((await r.json()).detail);
    const d = await r.json();
    record(`🎀 <b>전체 감사 완료</b> — 파일 ${d.files_scanned}개 스캔` +
           fold("감사 보고서 보기", d.report), "meet");
  } catch (e) { record(`❌ 감사 실패: ${esc(e.message)}`, "fail"); }
};

$("btnDebt").onclick = async () => {
  try {
    const r = await fetch(`/ponytail/debt?workdir=${wd()}`);
    const d = await r.json();
    const body = d.items.map((i) => `${i.file}:${i.line}  ${i.text}`).join("\n")
                 || "미뤄둔 작업 없음 — 깨끗하다.";
    record(`🎀 <b>부채 추적</b> — TODO/FIXME ${d.count}건` +
           fold("목록 보기", body), d.count ? "fail" : "meet");
  } catch (e) { record(`❌ 부채 추적 실패: ${esc(e.message)}`, "fail"); }
};

$("btnGain").onclick = async () => {
  try {
    const r = await fetch(`/ponytail/gain?workdir=${wd()}`);
    const d = await r.json();
    const body = `파일 ${d.files}개 · 총 ${d.total_lines}줄 · 의존성 ${d.dependencies}개\n\n` +
      "큰 파일 TOP5:\n" + d.largest.map((s) => `${s.lines}줄  ${s.file}`).join("\n");
    record(`🎀 <b>규모 지표</b> — 총 ${d.total_lines}줄` + fold("상세 보기", body), "meet");
  } catch (e) { record(`❌ 지표 조회 실패: ${esc(e.message)}`, "fail"); }
};

// ---------- API 키 설정 ----------
const KEY_FIELDS = { anthropic: "Anthropic", openai: "Openai", google: "Google" };

async function loadKeyStatus() {
  const st = await (await fetch("/settings/keys")).json();
  const auth = await (await fetch("/settings/claude-auth")).json();
  $("claudeAuth").value = auth.mode;
  for (const [provider, suffix] of Object.entries(KEY_FIELDS)) {
    const el = $("st" + suffix);
    if (provider === "anthropic" && auth.mode === "subscription") {
      el.textContent = "구독 확인 중...";
      fetch("/settings/claude-auth/status").then((r) => r.json()).then((s) => {
        const who = s.email || s.org || "claude.ai 계정";
        const tier = s.subscription ? ` (${s.subscription})` : "";
        el.textContent = s.logged_in
          ? `구독 연동: ${who}${tier}`
          : "⚠ 로그인 필요 — 터미널에서 claude 실행 후 /login";
        el.style.color = s.logged_in ? "var(--ok)" : "var(--bad)";
      });
      continue;
    }
    el.textContent = st[provider].set ? `연동됨 ${st[provider].hint}` : "미설정";
    el.style.color = st[provider].set ? "var(--ok)" : "var(--dim)";
  }
}

$("claudeAuth").onchange = async () => {
  await fetch("/settings/claude-auth", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: $("claudeAuth").value }),
  });
  await loadKeyStatus();
  await loadModels();
  record(`⚙ Claude 인증 방식 변경: <b>${$("claudeAuth").value === "subscription"
    ? "구독 (Claude Code)" : "API 키"}</b>`);
};

$("btnSaveKeys").onclick = async () => {
  const body = {};
  for (const [provider, suffix] of Object.entries(KEY_FIELDS)) {
    const v = $("key" + suffix).value.trim();
    if (v) body[provider] = v;
  }
  if (!Object.keys(body).length) return;
  await fetch("/settings/keys", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  Object.values(KEY_FIELDS).forEach((s) => ($("key" + s).value = ""));
  await loadKeyStatus();
  await loadModels();   // 키 반영 즉시 모델 선택지 활성화 갱신
  record("⚙ API 키 저장됨 — 모델 선택지가 갱신됐습니다.");
};

resetScene();
loadModels();
loadKeyStatus();
attachLatest();   // 새로고침 후에도 진행 중/최근 실행에 자동 연결
