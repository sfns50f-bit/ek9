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
  });
}

function storageGet(key) {
  try { return window.localStorage.getItem(key); } catch (e) { return null; }
}
function storageSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (e) { /* 保存できなくても動作は続ける */ }
}

// --- 解き始め・作成依頼：科目ごとの範囲の切り替えと、残りの問題数の表示 -----------
function setupScopeForm(form, mode) {
  const subjectSel = form.querySelector("#subject");
  const dataEl = document.getElementById("home-data");
  const hint = form.querySelector("#stock-hint");
  if (!subjectSel || !dataEl) return;
  const data = JSON.parse(dataEl.textContent);
  const scopes = form.querySelectorAll("select.scope");
  const devices = { phone: ["choice"], keyboard: ["input", "choice"] };
  const key = "quiz." + mode;

  function currentScope() {
    const sel = form.querySelector(`select.scope[data-subject="${subjectSel.value}"]`);
    return sel ? sel.value : "";
  }
  function formats() {
    if (mode === "start") {
      const d = form.querySelector("input[name=device]:checked");
      return devices[d ? d.value : "phone"];
    }
    const f = form.querySelector("input[name=format]:checked");
    return [f ? f.value : "choice"];
  }
  function templatesOf(subject, scope) {
    const out = [];
    subject.units.forEach((u) => u.subunits.forEach((su) => {
      if (scope.startsWith("unit:") && u.id !== scope.slice(5)) return;
      if (scope.startsWith("tmpl:") && su.id !== scope.slice(5)) return;
      out.push(su.id);
    }));
    return out;
  }
  function update() {
    scopes.forEach((sel) => {
      const on = sel.dataset.subject === subjectSel.value;
      sel.hidden = !on;
      sel.disabled = !on;
    });
    const subject = data[subjectSel.value];
    if (subject && hint) {
      let n = 0;
      templatesOf(subject, currentScope()).forEach((t) => formats().forEach((f) => { n += subject.counts[`${t}|${f}`] || 0; }));
      hint.textContent = mode === "start"
        ? `この範囲でまだ解いていない問題：${n} 問`
        : `この範囲・形式のストック（まだ解いていない問題）：${n} 問`;
    }
    storageSet(key, JSON.stringify({ subject: subjectSel.value, scope: currentScope() }));
  }
  let last = null;
  try { last = JSON.parse(storageGet(key) || "null"); } catch (e) { last = null; }
  if (last && data[last.subject]) {
    subjectSel.value = last.subject;
    const sel = form.querySelector(`select.scope[data-subject="${last.subject}"]`);
    if (sel && [...sel.options].some((o) => o.value === last.scope)) sel.value = last.scope;
  }
  form.addEventListener("change", update);
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
  const start = document.getElementById("start-form");
  if (start) setupScopeForm(start, "start");
  const gen = document.getElementById("generate-form");
  if (gen) setupScopeForm(gen, "generate");
  setupPlay();
});
