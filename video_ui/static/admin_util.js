// Чистые функции админки (форматирование и разбор строк журнала).
// Вынесены из admin.html, чтобы их можно было проверить node-тестами без
// браузера — как answer_render.js / vad.js / stream_client.js.
"use strict";

// «450 с» читается хуже, чем «7 мин»: оператор смотрит на статус мельком.
function ago(sec) {
  if (sec === null || sec === undefined) return "—";
  if (sec < 90) return Math.round(sec) + " с";
  if (sec < 5400) return Math.round(sec / 60) + " мин";
  if (sec < 172800) return Math.round(sec / 3600) + " ч";
  return Math.round(sec / 86400) + " сут";
}

// Задержки: до секунды показываем миллисекунды, дальше секунды с десятой —
// «4980 мс» глазами не сравнить, «5.0 с» сравнивается сразу.
function fmtMs(ms) {
  if (ms === null || ms === undefined || ms === "") return "—";
  if (ms < 1000) return Math.round(ms) + " мс";
  return (ms / 1000).toFixed(1).replace(".", ",") + " с";
}

function pct(part, whole) {
  if (!whole) return "—";
  return (100 * part / whole).toFixed(1).replace(".", ",") + "%";
}

function truncate(s, n) {
  s = s || "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// «2026-07-29T14:23:41Z» -> «29.07 14:23». Год не показываем: ретеншен 30 суток,
// и он только съедал бы ширину колонки.
function fmtTs(iso) {
  if (!iso || iso.length < 16) return "—";
  const d = iso.slice(0, 10).split("-");
  return d[2] + "." + d[1] + " " + iso.slice(11, 16);
}

// Статус обращения одним словом. Порядок важен: ошибка перекрывает всё
// остальное (при сбое STT вопроса могло и не быть), и только потом смотрим,
// нашла ли база ответ.
function statusOf(row) {
  if (row.error === "disabled") return { label: "точка отключена", cls: "s-off" };
  if (row.error === "gate") return { label: "не пропущен", cls: "s-off" };
  // Шум и пустая запись — не сбой и не пробел в базе: базу мы не спрашивали.
  if (row.error === "noise") return { label: "не расслышал (шум)", cls: "s-off" };
  if (row.error === "empty") return { label: "не расслышал (тишина)", cls: "s-off" };
  if (row.error) return { label: "сбой: " + row.error, cls: "s-err" };
  if (row.answer_found === false) return { label: "нет в базе", cls: "s-gap" };
  return { label: "ответ найден", cls: "s-ok" };
}

const LANG_SHORT = { russian: "рус", kazakh: "каз" };
function langName(code) { return LANG_SHORT[code] || code || "—"; }

// Светофор плиток: без него семь одинаково серых квадратов, и глазу негде
// зацепиться — проблему приходится вычислять, а не видеть. Пороги грубые и
// намеренно простые: их задача — покрасить, а не поставить диагноз.
const TONES = {
  // доля вопросов, на которые база не ответила
  fallback: [0.20, 0.40],
  // доля записей с шумом/тишиной: много — микрофон или место шумное
  not_heard: [0.10, 0.25],
  // ЛЮБОЙ сбой сервиса — уже плохо, поэтому первый порог нулевой
  failures: [0.0001, 0.05],
  // ожидание ответа, миллисекунды
  wait: [10000, 20000],
};

function tone(kind, value) {
  const t = TONES[kind];
  if (!t || value === null || value === undefined) return "";
  if (value < t[0]) return "good";
  return value < t[1] ? "warn" : "bad";
}

// Строка запроса из объекта: пустые и нулевые значения не тащим, иначе в адресе
// копится мусор вида &kiosk=&only=.
function qs(params) {
  const parts = [];
  Object.keys(params).forEach(function (k) {
    const v = params[k];
    if (v === null || v === undefined || v === "" || v === false) return;
    parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
  });
  return parts.join("&");
}

/* для node-тестов (в браузере module нет) */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { ago: ago, fmtMs: fmtMs, pct: pct, truncate: truncate,
                     fmtTs: fmtTs, statusOf: statusOf, qs: qs,
                     langName: langName, tone: tone, TONES: TONES };
}
