"use strict";

// 数式（$…$）を KaTeX で表示する。KaTeX はアプリに同梱しているので、インターネットにつながらなくても動く
function renderMath(root) {
  if (!window.renderMathInElement) return;
  renderMathInElement(root || document.body, {
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "$", right: "$", display: false },
      { left: "\\(", right: "\\)", display: false },
      { left: "\\[", right: "\\]", display: true },
    ],
    throwOnError: false,
    ignoredClasses: ["nomath"],  // 英語など、$ を数式に使わない科目（「$20」を数式にしない）
  });
}

// 作成役と検証役が同じモデルなら注意を出す（作る画面のモデルの欄と、状態の科目ごとの設定）
function setupSameModelWarning() {
  document.querySelectorAll(".models, .subject-models").forEach((box) => {
    const warn = box.querySelector(".same-model");
    const gen = box.querySelector("select[name$=gen_model], select[name=gen]");
    const verify = box.querySelector("select[name$=verify_model], select[name=verify]");
    if (!warn || !gen || !verify) return;
    const check = () => {
      const g = gen.value || gen.dataset.inherit || "";
      const v = verify.value || verify.dataset.inherit || "";
      warn.hidden = !g || g !== v;
    };
    gen.addEventListener("change", check);
    verify.addEventListener("change", check);
  });
}

// 本文のある設問：スマホでは「本文」と「設問」をタブで切り替える（広い画面では CSS で左右に並べる）。
// 同じ本文の最初の設問では本文から、2 問目からは設問から見せる
function setupReading() {
  const box = document.querySelector(".reading");
  if (!box) return;
  box.classList.add("js");
  function show(name, scroll) {
    box.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.dataset.paneName === name));
    box.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", t.dataset.pane === name ? "true" : "false"));
    if (scroll) box.scrollIntoView({ block: "start" });
  }
  box.querySelectorAll(".tab, .tab-go").forEach((b) => b.addEventListener("click", () => show(b.dataset.pane, true)));
  const id = box.dataset.passage;
  show(storageGet("quiz.passage") === id ? "question" : "passage", false);
  storageSet("quiz.passage", id);
}

function storageGet(key) {
  try { return window.localStorage.getItem(key); } catch (e) { return null; }
}
function storageSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (e) { /* 保存できなくても動作は続ける */ }
}

// --- 解き始め・作成依頼：科目ごとの範囲（単元・小単元のチェック）とレベル、ストックの数の表示 ---
function setupScopeForm(form, mode) {
  const subjectSel = form.querySelector("#subject");
  const dataEl = document.getElementById("home-data");
  const hint = form.querySelector("#stock-hint");
  if (!subjectSel || !dataEl) return;
  const data = JSON.parse(dataEl.textContent);
  const pickers = form.querySelectorAll(".scope-picker");
  const devices = { phone: ["choice"], keyboard: ["input", "choice"] };
  const key = "quiz." + mode;

  function picker() { return form.querySelector(`.scope-picker[data-subject="${subjectSel.value}"]`); }
  function formats() {
    if (mode === "start") {
      const d = form.querySelector("input[name=device]:checked");
      return devices[d ? d.value : "phone"];
    }
    const f = form.querySelector("input[name=format]:checked");
    return [f ? f.value : "choice"];
  }
  function levels() { return [...form.querySelectorAll("input[name=levels]:checked")].map((c) => c.value); }
  function checkedScope() {
    const p = picker();
    return p ? [...p.querySelectorAll("input[name=scope]:checked")].map((c) => c.value) : [];
  }
  function update() {
    pickers.forEach((p) => {
      const on = p.dataset.subject === subjectSel.value;
      p.hidden = !on;
      p.querySelectorAll("input").forEach((i) => { i.disabled = !on; });
    });
    // 作る画面のモデルの欄（科目ごと）。選んだ科目のものだけを見せて送る
    form.querySelectorAll(".models[data-subject]").forEach((box) => {
      const on = box.dataset.subject === subjectSel.value;
      box.hidden = !on;
      box.querySelectorAll("input, select").forEach((i) => { i.disabled = !on; });
    });
    const subject = data[subjectSel.value];
    const p = picker();
    if (!subject || !p) return;
    const lv = levels();
    const scope = checkedScope();
    const chosen = new Set();
    let subjectTotal = 0;
    let chosenTotal = 0;
    subject.units.forEach((u) => {
      let unitTotal = 0;
      u.subunits.forEach((su) => {
        let n = 0;
        formats().forEach((f) => { n += subject.counts[`${su.id}|${f}`] || 0; });
        const inLevel = !lv.length || lv.includes(su.level);
        const row = p.querySelector(`[data-tmpl-count="${su.id}"]`);
        if (row) {
          row.textContent = `${n} 問`;
          row.parentElement.classList.toggle("dim", !inLevel);
        }
        if (!inLevel) return;
        unitTotal += n;
        const picked = !scope.length || scope.includes(`unit:${u.id}`) || scope.includes(`tmpl:${su.id}`);
        if (picked) { chosenTotal += n; }
        if (scope.includes(`unit:${u.id}`) || scope.includes(`tmpl:${su.id}`)) chosen.add(u.label);
      });
      subjectTotal += unitTotal;
      const el = p.querySelector(`[data-unit-count="${u.id}"]`);
      if (el) el.textContent = `${unitTotal} 問`;
    });
    const total = p.querySelector("[data-subject-total]");
    if (total) total.textContent = `ストック ${subjectTotal} 問`;
    const summary = p.querySelector(".scope-summary");
    if (summary) summary.textContent = chosen.size ? [...chosen].join("、") : `${subject.name}（全体）`;
    if (hint) {
      hint.textContent = mode === "start"
        ? `選んだ範囲でまだ解いていない問題：${chosenTotal} 問`
        : `選んだ範囲・形式のストック（まだ解いていない問題）：${chosenTotal} 問`;
    }
    storageSet(key, JSON.stringify({ subject: subjectSel.value, scope, levels: lv }));
  }
  // 単元にチェック → その小単元を全部チェック。小単元を外す → 単元のチェックも外す
  form.addEventListener("change", (ev) => {
    const t = ev.target;
    if (t.name === "scope" && t.dataset.unit) {
      form.querySelectorAll(`input[data-parent="${t.dataset.unit}"]`).forEach((c) => { c.checked = t.checked; });
    } else if (t.name === "scope" && t.dataset.parent) {
      const kids = [...form.querySelectorAll(`input[data-parent="${t.dataset.parent}"]`)];
      const unit = form.querySelector(`input[data-unit="${t.dataset.parent}"]`);
      if (unit) unit.checked = kids.every((c) => c.checked);
    }
    update();
  });
  let last = null;
  try { last = JSON.parse(storageGet(key) || "null"); } catch (e) { last = null; }
  if (last && data[last.subject]) {
    subjectSel.value = last.subject;
    const p = picker();
    (last.scope || []).forEach((v) => {
      const box = p && [...p.querySelectorAll("input[name=scope]")].find((c) => c.value === v);
      if (box) box.checked = true;
    });
    (last.levels || []).forEach((v) => {
      const box = form.querySelector(`input[name=levels][value="${v}"]`);
      if (box) box.checked = true;
    });
  }
  update();
  form.addEventListener("submit", () => {
    const button = form.querySelector("button[type=submit]");
    if (button) setTimeout(() => { button.disabled = true; }, 0);
  });
}

// --- 解く画面：ヒントを開いたかを記録する、キーボード入力のプレビュー ------------
function setupPlay() {
  const hint = document.getElementById("hint");
  const flag = document.getElementById("hint_viewed");
  if (hint && flag) hint.addEventListener("toggle", () => { if (hint.open) flag.value = "1"; });

  const form = document.getElementById("answer-form");
  // 「戻る」で表示し直したときや送信に失敗したときに、ボタンが押せないまま残らないようにする
  window.addEventListener("pageshow", () => {
    document.querySelectorAll("button[disabled]").forEach((b) => { b.disabled = false; });
  });
  if (form) {
    // 2 回押しで二重に送らないようにする（押したボタンの値は送る）
    form.addEventListener("submit", () => {
      setTimeout(() => form.querySelectorAll("button").forEach((b) => { b.disabled = true; }), 0);
    });
  }

  const input = document.getElementById("response");
  const preview = document.getElementById("preview");
  if (!input || !preview) return;
  input.focus();
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const text = input.value.trim();
      if (!text) { preview.textContent = ""; return; }
      try {
        const res = await fetch("/api/preview", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ problem_id: input.dataset.problem, text }),
        });
        const data = await res.json();
        if (data.ok && window.katex) {
          preview.textContent = "";
          const span = document.createElement("span");
          preview.append("読み取り: ", span);
          katex.render(data.latex, span, { throwOnError: false });
        } else {
          preview.textContent = data.error || "";
        }
      } catch (e) { preview.textContent = ""; }
    }, 400);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  renderMath();
  setupReading();
  setupSameModelWarning();
  const start = document.getElementById("start-form");
  if (start) setupScopeForm(start, "start");
  const gen = document.getElementById("generate-form");
  if (gen) setupScopeForm(gen, "generate");
  setupPlay();
});
