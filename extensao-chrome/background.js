// Ao clicar no ícone da extensão, abre o painel lateral (não fecha sozinho).
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((e) => console.warn("sidePanel:", e));
