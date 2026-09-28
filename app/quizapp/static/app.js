"use strict";

function renderMath(root) {
  root = root || document.body;
  if (!window.katex) return;
  root.querySelectorAll(".tex[data-tex]").forEach((el) => {
    try {
      katex.render(el.dataset.tex, el, { throwOnError: false, displayMode: el.classList.contains("display") });
    } catch (e) { /* 元の文字列を表示したままにする */ }
  });
  if (window.renderMathInElement) {
    renderMathInElement(root, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
        { left: "\\[", right: "\\]", display: true },
      ],
      ignoredClasses: ["tex", "katex"],
      throwOnError: false,
    });
  }
}

function storageGet(key) {
  try { return window.localStorage.getItem(key); } catch (e) { return null; }
}

function storageSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (e) { /* 保存できなくても動作は続ける */ }
}

// --- ホーム：科目・単元・難易度の選択 ---------------------------------------
function setupGenerateForm() {
  const form = document.getElementById("generate-form");
  if (!form) return;
  const courses = JSON.parse(document.getElementById("courses-data").textContent);
  const stock = JSON.parse(document.getElementById("stock-data").textContent);
  const courseSel = document.getElementById("course");
  const topicSel = document.getElementById("topic");
  const hint = document.getElementById("stock-hint");

  courses.forEach((c, i) => courseSel.add(new Option(c.course, String(i))));

  function fillTopics() {
    const course = courses[Number(courseSel.value)] || courses[0];
    topicSel.innerHTML = "";
    course.topics.forEach((t) => topicSel.add(new Option(`${t.unit}（${t.level}）`, t.id)));
  }

  function difficulty() {
    const checked = form.querySelector("input[name=difficulty]:checked");
    return checked ? checked.value : "2";
  }

  function updateHint() {
    const n = stock[`${topicSel.value}|${difficulty()}`] || 0;
    hint.textContent = n > 0
      ? `在庫 ${n} 問：すぐに出題できます。`
      : "在庫なし：これから作問して検証します（数分〜十数分）。";
    storageSet("quiz.last", JSON.stringify({ course: courseSel.value, topic: topicSel.value, difficulty: difficulty() }));
  }

  let last = null;
  try { last = JSON.parse(storageGet("quiz.last") || "null"); } catch (e) { last = null; }
  if (last && courses[Number(last.course)]) courseSel.value = last.course;
  fillTopics();
  if (last && last.topic) topicSel.value = last.topic;
  if (!topicSel.value && topicSel.options.length) topicSel.selectedIndex = 0;
  if (last && last.difficulty) {
    const radio = form.querySelector(`input[name=difficulty][value="${last.difficulty}"]`);
    if (radio) radio.checked = true;
  }

  courseSel.addEventListener("change", () => { fillTopics(); updateHint(); });
  topicSel.addEventListener("change", updateHint);
  form.querySelectorAll("input[name=difficulty]").forEach((r) => r.addEventListener("change", updateHint));
  form.addEventListener("submit", () => {
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    button.textContent = "準備中…";
  });
  updateHint();
}

// --- 生成待ち ---------------------------------------------------------------
function setupJobPage() {
  const box = document.getElementById("job");
  if (!box) return;
  const id = box.dataset.jobId;
  const progress = document.getElementById("job-progress");
  const elapsed = document.getElementById("job-elapsed");
  const errorBox = document.getElementById("job-error");
  const started = Date.now();

  async function poll() {
    let data;
    try {
      const res = await fetch(`/api/jobs/${id}`, { cache: "no-store" });
      data = await res.json();
    } catch (e) {
      setTimeout(poll, 5000);
      return;
    }
    const sec = Math.round((Date.now() - started) / 1000);
    elapsed.textContent = `経過 ${Math.floor(sec / 60)}分${sec % 60}秒`;
    if (data.status === "done") {
      window.location.href = `/jobs/${id}`;
      return;
    }
    if (data.status === "failed" || data.status === "cancelled") {
      box.querySelector(".spinner").hidden = true;
      progress.textContent = data.status === "cancelled" ? "キャンセルしました。" : "問題を作れませんでした。";
      errorBox.textContent = data.error || "";
      errorBox.hidden = !data.error;
      document.getElementById("retry-form").hidden = false;
      return;
    }
    if (data.status === "pending") {
      progress.textContent = data.ahead > 0
        ? `順番待ち（前に ${data.ahead} 件）`
        : `順番待ち（ワーカー：${data.worker || "応答待ち"}）`;
    } else {
      progress.textContent = data.progress || "処理中";
    }
    setTimeout(poll, 3000);
  }
  poll();
}

// --- 解答入力：記号ボタンとプレビュー ---------------------------------------
function setupAnswerInput() {
  const input = document.getElementById("answer");
  if (!input) return;
  const preview = document.getElementById("preview");
  let timer = null;
  let seq = 0;

  async function updatePreview() {
    const text = input.value.trim();
    const mine = ++seq;
    if (!text) { preview.innerHTML = ""; return; }
    let data;
    try {
      const res = await fetch("/api/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, kind: input.dataset.kind, var: input.dataset.var }),
      });
      data = await res.json();
    } catch (e) { return; }
    if (mine !== seq) return;
    if (data.ok && window.katex) {
      preview.classList.remove("bad");
      katex.render(data.latex, preview, { throwOnError: false, displayMode: true });
    } else {
      preview.classList.add("bad");
      preview.textContent = data.error ? `読み取れません：${data.error}` : "";
    }
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(updatePreview, 300);
  });

  document.querySelectorAll(".keys button[data-insert]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const text = btn.dataset.insert;
      const start = input.selectionStart ?? input.value.length;
      const end = input.selectionEnd ?? input.value.length;
      input.value = input.value.slice(0, start) + text + input.value.slice(end);
      const pos = start + text.length;
      input.focus();
      input.setSelectionRange(pos, pos);
      input.dispatchEvent(new Event("input"));
    });
  });

  if (input.value) updatePreview();
}

document.addEventListener("DOMContentLoaded", () => {
  renderMath();
  setupGenerateForm();
  setupJobPage();
  setupAnswerInput();
});
