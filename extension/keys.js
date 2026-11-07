// Content script: informa ao background quais teclas (Ctrl/Alt/Shift) estavam
// pressionadas no último clique. O Raio usa isso para "impedir" ou "forçar" a captura.
function mods(e) {
  const m = [];
  if (e.ctrlKey) m.push("Ctrl");
  if (e.altKey) m.push("Alt");
  if (e.shiftKey) m.push("Shift");
  return m.join("+");
}

function report(e) {
  try {
    chrome.runtime.sendMessage({ type: "mods", mods: mods(e) }).catch(() => {});
  } catch (err) {
    // contexto da extensão invalidado (extensão recarregada): ignora
  }
}

// Em todo clique (inclusive sem tecla) para o valor anterior nunca ficar "preso".
addEventListener("mousedown", report, true);
addEventListener("click", report, true);
// Teclado: só quando há modificador (Enter em um link, por exemplo).
addEventListener("keydown", (e) => { if (e.ctrlKey || e.altKey || e.shiftKey) report(e); }, true);
